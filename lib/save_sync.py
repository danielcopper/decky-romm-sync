import os
import json
import hashlib
import socket
import time
import uuid
import base64
import ssl
import tempfile
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import decky

if TYPE_CHECKING:
    import asyncio
    from typing import Callable, Optional, Protocol

    class _SaveSyncDeps(Protocol):
        settings: dict
        _state: dict
        _save_sync_state: dict
        loop: asyncio.AbstractEventLoop
        def _romm_request(self, path: str) -> Any: ...
        def _romm_download(self, path: str, dest: str, progress_callback: Optional[Callable] = None) -> None: ...
        def _resolve_system(self, platform_slug: str, platform_fs_slug: Optional[str] = None) -> str: ...
        def _save_state(self) -> None: ...
        def _log_debug(self, msg: str) -> None: ...


class SaveSyncMixin:
    """Bidirectional save file sync between local RetroDECK and RomM server."""

    # ── State Management ─────────────────────────────────────────

    def _init_save_sync_state(self):
        """Initialize default save sync state. Called from _main()."""
        self._save_sync_state = {
            "version": 1,
            "device_id": None,
            "device_name": None,
            "saves": {},
            "playtime": {},
            "pending_conflicts": [],
            "offline_queue": [],
            "settings": {
                "save_sync_enabled": False,
                "conflict_mode": "newest_wins",
                "sync_before_launch": True,
                "sync_after_exit": True,
                "clock_skew_tolerance_sec": 60,
            },
        }

    def _load_save_sync_state(self):
        """Load save sync state from disk, merging with defaults."""
        path = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "save_sync_state.json")
        try:
            with open(path, "r") as f:
                saved = json.load(f)
            for key in ("saves", "playtime", "pending_conflicts", "offline_queue"):
                if key in saved:
                    self._save_sync_state[key] = saved[key]
            for key in ("version", "device_id", "device_name"):
                if key in saved:
                    self._save_sync_state[key] = saved[key]
            if "settings" in saved:
                self._save_sync_state["settings"].update(saved["settings"])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_save_sync_state(self):
        """Persist save sync state to disk (atomic write)."""
        state_dir = decky.DECKY_PLUGIN_RUNTIME_DIR
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, "save_sync_state.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._save_sync_state, f, indent=2)
        os.replace(tmp, path)

    def _get_retrodeck_saves_path(self):
        """Read the saves path from RetroDECK's retrodeck.json config.

        Returns the saves base directory. Falls back to ~/retrodeck/saves
        if the config file is unreadable or missing the path.
        Not cached — reads fresh every call to handle config changes.
        """
        fallback = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "saves")
        config_path = os.path.join(
            decky.DECKY_USER_HOME,
            ".var", "app", "net.retrodeck.retrodeck",
            "config", "retrodeck", "retrodeck.json",
        )
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
            saves_path = config.get("paths", {}).get("saves_path", "")
            if saves_path:
                return saves_path
        except (OSError, IOError, json.JSONDecodeError, ValueError):
            self._log_debug("Could not read retrodeck.json, using fallback saves path")
        return fallback

    def _get_rom_save_info(self, rom_id):
        """Get save-related info for an installed ROM.

        Reads the saves path from RetroDECK config (retrodeck.json).
        Save path pattern: <saves_path>/<system>/<rom_name>.srm

        Returns (system, rom_name, saves_dir) or None if not installed.
        """
        rom_id_str = str(int(rom_id))
        installed = self._state["installed_roms"].get(rom_id_str)
        if not installed:
            return None
        system = installed.get("system", "")
        file_path = installed.get("file_path", "")
        if not system or not file_path:
            return None
        rom_name = os.path.splitext(os.path.basename(file_path))[0]

        saves_base = self._get_retrodeck_saves_path()
        saves_dir = os.path.join(saves_base, system)

        return system, rom_name, saves_dir

    # ── Playtime Notes API Helpers ────────────────────────────────

    PLAYTIME_NOTE_TITLE = "romm-sync:playtime"

    def _romm_get_playtime_note(self, rom_id):
        """Fetch the playtime note for a ROM.

        Uses GET /api/roms/{rom_id} and filters all_user_notes by title,
        because GET /api/roms/{rom_id}/notes returns 500 when notes exist (RomM bug).
        Returns note dict (with id, content, etc.) or None if not found.
        """
        rom_detail = self._romm_request(f"/api/roms/{rom_id}")
        if not isinstance(rom_detail, dict):
            return None
        notes = rom_detail.get("all_user_notes", [])
        if not isinstance(notes, list):
            return None
        for note in notes:
            if note.get("title") == self.PLAYTIME_NOTE_TITLE:
                return note
        return None

    def _romm_create_playtime_note(self, rom_id, playtime_data):
        """Create a new playtime note for a ROM.

        Don't send tags — they cause GET /api/roms/{id}/notes to return 500.
        """
        result = self._romm_post_json(f"/api/roms/{rom_id}/notes", {
            "title": self.PLAYTIME_NOTE_TITLE,
            "content": json.dumps(playtime_data),
            "is_public": False,
        })
        # Store note_id in state for future updates
        if isinstance(result, dict) and result.get("id"):
            rom_id_str = str(int(rom_id))
            entry = self._save_sync_state.get("playtime", {}).get(rom_id_str)
            if entry is not None:
                entry["note_id"] = result["id"]
                self._save_save_sync_state()
        return result

    def _romm_update_playtime_note(self, rom_id, note_id, playtime_data):
        """Update an existing playtime note."""
        return self._romm_put_json(f"/api/roms/{rom_id}/notes/{note_id}", {
            "content": json.dumps(playtime_data),
        })

    @staticmethod
    def _parse_playtime_note_content(content):
        """Parse JSON content from a playtime note. Returns dict or None."""
        if not content:
            return None
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    def _sync_playtime_to_romm(self, rom_id, session_duration_sec):
        """Push playtime to RomM via the Notes API after a session.

        Fetches the server note, adds the session delta to the server total,
        and creates/updates the note. Best-effort — errors are logged.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        entry = self._save_sync_state.get("playtime", {}).get(rom_id_str)
        if not entry:
            return

        local_total = entry.get("total_seconds", 0)
        device_name = self._save_sync_state.get("device_name", "")

        try:
            note = self._with_retry(self._romm_get_playtime_note, rom_id)
            server_seconds = 0
            note_id = None

            if note:
                note_id = note.get("id")
                server_data = self._parse_playtime_note_content(
                    note.get("content", "")
                )
                if server_data:
                    server_seconds = int(server_data.get("seconds", 0))

            # Merge: server baseline + this session, or local total, whichever is higher
            new_total = max(local_total, server_seconds + session_duration_sec)

            playtime_data = {
                "seconds": new_total,
                "updated": datetime.now(timezone.utc).isoformat(),
                "device": device_name,
            }

            if note_id:
                self._with_retry(
                    self._romm_update_playtime_note, rom_id, note_id, playtime_data
                )
            else:
                self._with_retry(
                    self._romm_create_playtime_note, rom_id, playtime_data
                )

            # Sync local state to the merged total
            entry["total_seconds"] = new_total
            self._save_save_sync_state()

        except Exception as e:
            self._log_debug(f"Failed to sync playtime to RomM for rom {rom_id}: {e}")

    # ── HTTP Helpers ──────────────────────────────────────────────

    @staticmethod
    def _is_retryable(exc):
        """Check if an exception is a transient network error worth retrying.

        Retries on: timeouts, connection refused/reset, 5xx server errors.
        Does NOT retry on: 4xx client errors (400, 401, 403, 404, 409, etc.).
        """
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code >= 500
        if isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, OSError)):
            return True
        return False

    def _with_retry(self, fn, *args, max_attempts=3, base_delay=1, **kwargs):
        """Call fn(*args, **kwargs) with exponential backoff retry.

        Delays: base_delay * 3^attempt (1s, 3s, 9s for defaults).
        Only retries on transient errors (see _is_retryable).
        """
        last_exc = None
        for attempt in range(max_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts - 1 and self._is_retryable(exc):
                    delay = base_delay * (3 ** attempt)
                    self._log_debug(
                        f"Retry {attempt + 1}/{max_attempts} after {delay}s: {exc}"
                    )
                    time.sleep(delay)
                else:
                    raise
        raise last_exc  # pragma: no cover

    def _romm_json_request(self, path, data, method="POST"):
        """Send a JSON request (POST/PUT) to RomM API, return parsed response."""
        url = self.settings["romm_url"].rstrip("/") + path
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        credentials = base64.b64encode(
            f"{self.settings['romm_user']}:{self.settings['romm_pass']}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {credentials}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def _romm_post_json(self, path, data):
        """POST JSON to RomM API, return parsed response."""
        return self._romm_json_request(path, data, method="POST")

    def _romm_put_json(self, path, data):
        """PUT JSON to RomM API, return parsed response."""
        return self._romm_json_request(path, data, method="PUT")

    def _romm_upload_multipart(self, path, file_path, method="POST"):
        """Upload a file via multipart/form-data to RomM API."""
        boundary = uuid.uuid4().hex
        filename = os.path.basename(file_path)
        safe_filename = filename.replace('"', '\\"')

        with open(file_path, "rb") as f:
            file_data = f.read()

        body = b""
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="saveFile"; filename="{safe_filename}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += file_data
        body += f"\r\n--{boundary}--\r\n".encode()

        url = self.settings["romm_url"].rstrip("/") + path
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        credentials = base64.b64encode(
            f"{self.settings['romm_user']}:{self.settings['romm_pass']}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {credentials}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def _romm_upload_save(self, rom_id, file_path, emulator="retroarch", save_id=None):
        """Upload or update a save file on RomM.

        POST does upsert by filename (same rom_id + filename = update in place).
        If save_id is given, uses PUT for explicit update.
        device_id is NOT sent — ignored on RomM 4.6.1.
        """
        params = f"rom_id={rom_id}&emulator={urllib.parse.quote(emulator)}"

        if save_id:
            return self._romm_upload_multipart(
                f"/api/saves/{save_id}?{params}", file_path, method="PUT"
            )
        return self._romm_upload_multipart(
            f"/api/saves?{params}", file_path, method="POST"
        )

    def _romm_download_save(self, save_id, dest_path):
        """Download a save file binary from RomM.

        GET /api/saves/{id}/content does NOT exist on 4.6.1.
        Instead: fetch metadata → use download_path → URL-encode and fetch binary.
        """
        metadata = self._romm_request(f"/api/saves/{save_id}")
        download_path = metadata.get("download_path", "")
        if not download_path:
            raise ValueError(f"Save {save_id} has no download_path")
        encoded_path = urllib.parse.quote(download_path, safe="/")
        self._romm_download(encoded_path, dest_path)

    def _romm_list_saves(self, rom_id):
        """List saves from RomM for a ROM. Returns plain array."""
        result = self._romm_request(f"/api/saves?rom_id={rom_id}")
        if isinstance(result, list):
            return result
        return []

    # ── File Helpers ──────────────────────────────────────────────

    def _file_md5(self, path):
        """Compute MD5 hash of a file."""
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _find_save_files(self, rom_id):
        """Find local save files (.srm, .rtc) for a ROM.

        Returns list of {"path": str, "filename": str}.
        """
        info = self._get_rom_save_info(rom_id)
        if not info:
            return []
        system, rom_name, saves_dir = info
        if not os.path.isdir(saves_dir):
            return []
        results = []
        for ext in (".srm", ".rtc"):
            save_path = os.path.join(saves_dir, rom_name + ext)
            if os.path.isfile(save_path):
                results.append({"path": save_path, "filename": rom_name + ext})
        return results

    # ── Server Save Hash Helper ──────────────────────────────────

    def _get_server_save_hash(self, server_save):
        """Download a server save to temp and compute its MD5 hash.

        Used for slow-path conflict detection when no content_hash is available.
        Returns hash string or None on error.
        """
        save_id = server_save.get("id")
        if not save_id:
            return None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp")
            os.close(fd)
            self._with_retry(self._romm_download_save, save_id, tmp_path)
            return self._file_md5(tmp_path)
        except Exception as e:
            self._log_debug(f"Failed to hash server save {save_id}: {e}")
            return None
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ── Conflict Detection ────────────────────────────────────────

    def _detect_conflict(self, rom_id, filename, local_hash, server_save):
        """Hybrid conflict detection (no content_hash on RomM 4.6.1).

        - Local change: hash local file vs last_sync_hash
        - Server change FAST PATH: updated_at + file_size_bytes vs stored values
        - Server change SLOW PATH: download to tmp, hash, compare with last_sync_hash

        Returns: "skip", "download", "upload", or "conflict".
        """
        rom_id_str = str(int(rom_id))
        save_state = self._save_sync_state["saves"].get(rom_id_str, {})
        file_state = save_state.get("files", {}).get(filename, {})
        last_sync_hash = file_state.get("last_sync_hash")

        # Never synced before — state recovery
        if not last_sync_hash:
            if local_hash:
                server_hash = self._get_server_save_hash(server_save)
                if server_hash is None:
                    return "conflict"  # Can't verify, ask user
                return "skip" if local_hash == server_hash else "conflict"
            return "download"

        local_changed = local_hash != last_sync_hash

        # Server change detection — fast path
        server_changed = False
        stored_updated_at = file_state.get("last_sync_server_updated_at")
        stored_size = file_state.get("last_sync_server_size")
        server_updated_at = server_save.get("updated_at", "")
        server_size = server_save.get("file_size_bytes")

        if stored_updated_at and server_updated_at == stored_updated_at:
            if stored_size is None or server_size == stored_size:
                server_changed = False  # fast path: unchanged
            else:
                server_changed = True  # size changed
        else:
            # Timestamp changed or no stored timestamp → slow path
            server_hash = self._get_server_save_hash(server_save)
            if server_hash and server_hash != last_sync_hash:
                server_changed = True
            else:
                # False alarm — update stored metadata
                if file_state:
                    file_state["last_sync_server_updated_at"] = server_updated_at
                    if server_size is not None:
                        file_state["last_sync_server_size"] = server_size

        if not local_changed and not server_changed:
            return "skip"
        if not local_changed and server_changed:
            return "download"
        if local_changed and not server_changed:
            return "upload"
        return "conflict"

    def _resolve_conflict_by_mode(self, local_mtime, server_save):
        """Apply configured conflict resolution mode.

        Returns: "upload", "download", or "ask".
        """
        settings = self._save_sync_state.get("settings", {})
        mode = settings.get("conflict_mode", "newest_wins")

        if mode == "always_upload":
            return "upload"
        if mode == "always_download":
            return "download"
        if mode == "ask_me":
            return "ask"

        # newest_wins (default)
        tolerance = settings.get("clock_skew_tolerance_sec", 60)
        server_updated = server_save.get("updated_at", "")
        try:
            server_dt = datetime.fromisoformat(server_updated.replace("Z", "+00:00"))
            local_dt = datetime.fromtimestamp(local_mtime, tz=timezone.utc)
            diff = abs((local_dt - server_dt).total_seconds())
            if diff <= tolerance:
                return "ask"
            return "upload" if local_dt > server_dt else "download"
        except (ValueError, TypeError):
            return "ask"

    def _add_pending_conflict(self, rom_id, filename, local_path, server_save):
        """Add a conflict to the pending queue (no duplicates)."""
        rom_id = int(rom_id)
        for c in self._save_sync_state["pending_conflicts"]:
            if c.get("rom_id") == rom_id and c.get("filename") == filename:
                return

        local_mtime = os.path.getmtime(local_path) if os.path.isfile(local_path) else None
        self._save_sync_state["pending_conflicts"].append({
            "rom_id": rom_id,
            "filename": filename,
            "local_path": local_path,
            "local_hash": self._file_md5(local_path) if os.path.isfile(local_path) else None,
            "local_mtime": (
                datetime.fromtimestamp(local_mtime, tz=timezone.utc).isoformat()
                if local_mtime else None
            ),
            "local_size": os.path.getsize(local_path) if os.path.isfile(local_path) else None,
            "server_save_id": server_save.get("id"),
            "server_updated_at": server_save.get("updated_at", ""),
            "server_size": server_save.get("file_size_bytes"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    def _update_file_sync_state(self, rom_id_str, filename, server_response, local_path, system):
        """Update per-file sync tracking after a successful sync operation."""
        if rom_id_str not in self._save_sync_state["saves"]:
            self._save_sync_state["saves"][rom_id_str] = {
                "files": {},
                "emulator": "retroarch",
                "system": system,
            }
        save_entry = self._save_sync_state["saves"][rom_id_str]
        save_entry.setdefault("files", {})

        now = datetime.now(timezone.utc).isoformat()
        local_hash = self._file_md5(local_path) if os.path.isfile(local_path) else ""
        local_mtime = (
            datetime.fromtimestamp(os.path.getmtime(local_path), tz=timezone.utc).isoformat()
            if os.path.isfile(local_path) else now
        )

        save_entry["files"][filename] = {
            "last_sync_hash": local_hash,
            "last_sync_at": now,
            "last_sync_server_updated_at": server_response.get("updated_at", now),
            "last_sync_server_save_id": server_response.get("id"),
            "last_sync_server_size": server_response.get("file_size_bytes"),
            "local_mtime_at_last_sync": local_mtime,
        }

    # ── Sync Helpers ──────────────────────────────────────────────

    def _do_download_save(self, server_save, saves_dir, filename, rom_id_str, system):
        """Download a save file from server. Backs up existing local file first."""
        local_path = os.path.join(saves_dir, filename)
        os.makedirs(saves_dir, exist_ok=True)
        tmp_path = local_path + ".tmp"

        self._with_retry(self._romm_download_save, server_save["id"], tmp_path)

        # Backup existing local save before overwriting
        if os.path.isfile(local_path):
            backup_dir = os.path.join(saves_dir, ".romm-backup")
            os.makedirs(backup_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            name, ext = os.path.splitext(filename)
            os.replace(local_path, os.path.join(backup_dir, f"{name}_{ts}{ext}"))

        os.replace(tmp_path, local_path)
        self._update_file_sync_state(rom_id_str, filename, server_save, local_path, system)
        self._log_debug(f"Downloaded save: {filename} for rom {rom_id_str}")

    def _do_upload_save(self, rom_id, file_path, filename, rom_id_str, system, server_save=None):
        """Upload a local save file to server."""
        save_id = server_save.get("id") if server_save else None

        result = self._with_retry(
            self._romm_upload_save, int(rom_id), file_path, "retroarch", save_id
        )

        self._update_file_sync_state(rom_id_str, filename, result, file_path, system)
        self._log_debug(f"Uploaded save: {filename} for rom {rom_id_str}")
        return result

    def _sync_rom_saves(self, rom_id, direction="both"):
        """Sync saves for a single ROM.

        direction: "download" (pre-launch), "upload" (post-exit), or "both" (manual).
        Returns (synced_count, errors_list).
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        info = self._get_rom_save_info(rom_id)
        if not info:
            self._log_debug(f"_sync_rom_saves({rom_id}): no save info, skipping")
            return 0, []
        system, rom_name, saves_dir = info

        # Fetch server saves (with retry)
        try:
            server_saves = self._with_retry(self._romm_list_saves, rom_id)
        except Exception as e:
            decky.logger.error(f"_sync_rom_saves({rom_id}): failed to list saves: {e}")
            return 0, [f"Failed to fetch saves: {e}"]

        local_files = self._find_save_files(rom_id)
        local_by_name = {lf["filename"]: lf for lf in local_files}
        self._log_debug(
            f"_sync_rom_saves({rom_id}): system={system}, rom_name={rom_name}, "
            f"local_files={len(local_files)}, server_saves={len(server_saves)}, "
            f"saves_dir={saves_dir}"
        )
        server_by_name = {}
        for ss in server_saves:
            fn = ss.get("file_name", "")
            if fn:
                server_by_name[fn] = ss

        all_filenames = set(local_by_name.keys()) | set(server_by_name.keys())
        synced = 0
        errors = []

        for filename in sorted(all_filenames):
            local = local_by_name.get(filename)
            server = server_by_name.get(filename)

            if local and server:
                local_hash = self._file_md5(local["path"])
                action = self._detect_conflict(rom_id, filename, local_hash, server)
            elif local and not server:
                action = "upload"
            elif server and not local:
                action = "download"
            else:
                continue

            # Filter by sync direction
            if direction == "download" and action == "upload":
                continue
            if direction == "upload" and action == "download":
                continue
            if action == "skip":
                continue

            # Resolve conflicts
            if action == "conflict":
                # Force ask on first sync (no baseline hash) — user must choose
                save_state = self._save_sync_state["saves"].get(rom_id_str, {})
                file_state = save_state.get("files", {}).get(filename, {})
                has_sync_history = bool(file_state.get("last_sync_hash"))

                if not has_sync_history:
                    resolution = "ask"
                else:
                    local_mtime = os.path.getmtime(local["path"]) if local else 0
                    resolution = self._resolve_conflict_by_mode(local_mtime, server)

                if resolution == "ask":
                    if local:
                        self._add_pending_conflict(rom_id, filename, local["path"], server)
                    continue
                action = resolution

            try:
                if action == "download":
                    self._do_download_save(
                        server, saves_dir, filename, rom_id_str, system
                    )
                    synced += 1
                elif action == "upload" and local:
                    self._do_upload_save(
                        rom_id, local["path"], filename, rom_id_str,
                        system, server
                    )
                    synced += 1
            except urllib.error.HTTPError as e:
                if e.code == 409 and local and server:
                    # Server has newer save — queue as conflict
                    self._add_pending_conflict(rom_id, filename, local["path"], server)
                else:
                    errors.append(f"{filename}: HTTP {e.code}")
            except Exception as e:
                errors.append(f"{filename}: {e}")
                tmp = os.path.join(saves_dir, filename + ".tmp")
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        # Persist failed operations to offline queue for manual retry
        for err in errors:
            parts = err.split(":", 1)
            err_filename = parts[0].strip() if len(parts) > 1 else ""
            if err_filename and err_filename in all_filenames:
                self._add_to_offline_queue(rom_id, err_filename, direction, err)

        return synced, errors

    def _add_to_offline_queue(self, rom_id, filename, direction, error_msg):
        """Add a failed sync operation to the offline queue for later retry."""
        rom_id = int(rom_id)
        queue = self._save_sync_state.setdefault("offline_queue", [])
        # Avoid duplicates
        for item in queue:
            if item.get("rom_id") == rom_id and item.get("filename") == filename:
                item["error"] = error_msg
                item["failed_at"] = datetime.now(timezone.utc).isoformat()
                item["retry_count"] = item.get("retry_count", 0) + 1
                return
        queue.append({
            "rom_id": rom_id,
            "filename": filename,
            "direction": direction,
            "error": error_msg,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "retry_count": 1,
        })

    # ── Callables ─────────────────────────────────────────────────

    def _is_save_sync_enabled(self):
        """Check if save sync feature is enabled."""
        return self._save_sync_state.get("settings", {}).get("save_sync_enabled", False)

    async def ensure_device_registered(self):
        """Ensure this device has a unique ID for save sync tracking.

        Generates a local UUID on first use — no server registration needed.
        The device_id is only used locally to identify which machine uploaded a save.
        """
        if not self._is_save_sync_enabled():
            return {"success": False, "device_id": "", "device_name": "", "disabled": True}

        if self._save_sync_state.get("device_id"):
            return {
                "success": True,
                "device_id": self._save_sync_state["device_id"],
                "device_name": self._save_sync_state.get("device_name", ""),
            }

        hostname = socket.gethostname()
        device_id = str(uuid.uuid4())

        self._save_sync_state["device_id"] = device_id
        self._save_sync_state["device_name"] = hostname
        self._save_save_sync_state()
        decky.logger.info(f"Device ID generated: {device_id} ({hostname})")
        return {"success": True, "device_id": device_id, "device_name": hostname}

    async def get_save_status(self, rom_id):
        """Get save sync status for a ROM (local files, server saves, conflict state)."""
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        local_files = self._find_save_files(rom_id)

        server_saves = []
        try:
            server_saves = self._with_retry(self._romm_list_saves, rom_id)
        except Exception as e:
            self._log_debug(f"Failed to fetch saves for rom {rom_id}: {e}")

        server_by_name = {
            ss.get("file_name", ""): ss for ss in server_saves if ss.get("file_name")
        }
        save_state = self._save_sync_state["saves"].get(rom_id_str, {})
        files_state = save_state.get("files", {})

        file_statuses = []
        seen_filenames = set()

        # Local files (may also exist on server)
        for lf in local_files:
            fn = lf["filename"]
            seen_filenames.add(fn)
            local_hash = self._file_md5(lf["path"])
            server = server_by_name.get(fn)

            if server:
                action = self._detect_conflict(rom_id, fn, local_hash, server)
            else:
                action = "upload" if local_hash else "skip"

            file_statuses.append({
                "filename": fn,
                "local_path": lf["path"],
                "local_hash": local_hash,
                "local_mtime": datetime.fromtimestamp(
                    os.path.getmtime(lf["path"]), tz=timezone.utc
                ).isoformat(),
                "local_size": os.path.getsize(lf["path"]),
                "server_save_id": server.get("id") if server else None,
                "server_updated_at": server.get("updated_at", "") if server else None,
                "server_size": server.get("file_size_bytes") if server else None,
                "last_sync_at": files_state.get(fn, {}).get("last_sync_at"),
                "status": action,
            })

        # Server-only saves (not present locally)
        for fn, ss in server_by_name.items():
            if fn not in seen_filenames:
                file_statuses.append({
                    "filename": fn,
                    "local_path": None,
                    "local_hash": None,
                    "local_mtime": None,
                    "local_size": None,
                    "server_save_id": ss.get("id"),
                    "server_updated_at": ss.get("updated_at", ""),
                    "server_size": ss.get("file_size_bytes"),
                    "last_sync_at": None,
                    "status": "download",
                })

        playtime = self._save_sync_state.get("playtime", {}).get(rom_id_str, {})
        return {
            "rom_id": rom_id,
            "files": file_statuses,
            "playtime": playtime,
            "device_id": self._save_sync_state.get("device_id", ""),
        }

    async def pre_launch_sync(self, rom_id):
        """Download newer saves from server before game launch."""
        if not self._is_save_sync_enabled():
            return {"success": True, "message": "Save sync disabled", "synced": 0}

        settings = self._save_sync_state.get("settings", {})
        if not settings.get("sync_before_launch", True):
            return {"success": True, "message": "Pre-launch sync disabled", "synced": 0}

        if not self._save_sync_state.get("device_id"):
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": "Device not registered"}

        synced, errors = self._sync_rom_saves(rom_id, direction="download")
        self._save_save_sync_state()

        msg = f"Downloaded {synced} save(s)"
        if errors:
            msg += f", {len(errors)} error(s)"
        return {
            "success": len(errors) == 0,
            "message": msg,
            "synced": synced,
            "errors": errors,
        }

    async def post_exit_sync(self, rom_id):
        """Upload changed saves after game exit."""
        if not self._is_save_sync_enabled():
            return {"success": True, "message": "Save sync disabled", "synced": 0}

        settings = self._save_sync_state.get("settings", {})
        if not settings.get("sync_after_exit", True):
            return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

        if not self._save_sync_state.get("device_id"):
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": "Device not registered"}

        synced, errors = self._sync_rom_saves(rom_id, direction="upload")
        self._save_save_sync_state()

        msg = f"Uploaded {synced} save(s)"
        if errors:
            msg += f", {len(errors)} error(s)"
        return {
            "success": len(errors) == 0,
            "message": msg,
            "synced": synced,
            "errors": errors,
        }

    async def sync_rom_saves(self, rom_id):
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        if not self._is_save_sync_enabled():
            return {"success": False, "message": "Save sync is disabled", "synced": 0}

        if not self._save_sync_state.get("device_id"):
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": "Device not registered"}

        synced, errors = self._sync_rom_saves(int(rom_id), direction="both")
        self._save_save_sync_state()

        msg = f"Synced {synced} save(s)"
        if errors:
            msg += f", {len(errors)} error(s)"
        return {
            "success": len(errors) == 0,
            "message": msg,
            "synced": synced,
            "errors": errors,
        }

    async def sync_all_saves(self):
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        if not self._is_save_sync_enabled():
            return {"success": False, "message": "Save sync is disabled", "synced": 0, "conflicts": 0}

        if not self._save_sync_state.get("device_id"):
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": "Device not registered"}

        total_synced = 0
        total_errors = []
        rom_count = 0

        # Only iterate installed ROMs — non-installed ROMs have no save files
        rom_ids = set(self._state["installed_roms"].keys())
        self._log_debug(f"sync_all_saves: {len(rom_ids)} ROMs to check")

        for rom_id_str in sorted(rom_ids):
            rom_count += 1
            synced, errors = self._sync_rom_saves(int(rom_id_str), direction="both")
            total_synced += synced
            total_errors.extend(errors)

        self._save_save_sync_state()

        conflicts = len(self._save_sync_state.get("pending_conflicts", []))
        msg = f"Synced {total_synced} save(s) across {rom_count} ROM(s)"
        if total_errors:
            msg += f", {len(total_errors)} error(s)"
        if conflicts:
            msg += f", {conflicts} conflict(s)"
        return {
            "success": len(total_errors) == 0,
            "message": msg,
            "synced": total_synced,
            "conflicts": conflicts,
            "roms_checked": rom_count,
            "errors": total_errors,
        }

    async def resolve_conflict(self, rom_id, filename, resolution):
        """Resolve a pending save conflict. resolution: "upload" or "download"."""
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        if resolution not in ("upload", "download"):
            return {"success": False, "message": f"Invalid resolution: {resolution}"}

        # Find and remove from pending
        conflict = None
        remaining = []
        for c in self._save_sync_state["pending_conflicts"]:
            if c.get("rom_id") == rom_id and c.get("filename") == filename:
                conflict = c
            else:
                remaining.append(c)
        self._save_sync_state["pending_conflicts"] = remaining

        if not conflict:
            return {"success": False, "message": "Conflict not found"}

        info = self._get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "message": "ROM not installed"}
        system, rom_name, saves_dir = info

        try:
            if resolution == "download":
                server_save_id = conflict.get("server_save_id")
                if not server_save_id:
                    return {"success": False, "message": "No server save ID"}
                server_save = self._with_retry(
                    self._romm_request, f"/api/saves/{server_save_id}"
                )
                self._do_download_save(
                    server_save, saves_dir, filename, rom_id_str, system
                )
            else:  # upload
                local_path = conflict.get("local_path")
                if not local_path or not os.path.isfile(local_path):
                    return {"success": False, "message": "Local file not found"}
                server_save = None
                if conflict.get("server_save_id"):
                    try:
                        server_save = self._with_retry(
                            self._romm_request,
                            f"/api/saves/{conflict['server_save_id']}"
                        )
                    except Exception:
                        pass
                self._do_upload_save(
                    rom_id, local_path, filename, rom_id_str,
                    system, server_save
                )

            self._save_save_sync_state()
            return {"success": True, "message": f"Conflict resolved: {resolution}"}
        except Exception as e:
            decky.logger.error(f"Conflict resolution failed: {e}")
            return {"success": False, "message": "Conflict resolution failed"}

    async def get_pending_conflicts(self):
        """Return list of unresolved save conflicts."""
        return {"conflicts": self._save_sync_state.get("pending_conflicts", [])}

    async def record_session_start(self, rom_id):
        """Record the start of a play session for playtime tracking."""
        rom_id_str = str(int(rom_id))
        playtime = self._save_sync_state.setdefault("playtime", {})
        entry = playtime.setdefault(rom_id_str, {
            "total_seconds": 0,
            "session_count": 0,
            "last_session_start": None,
            "last_session_duration_sec": None,
            "offline_deltas": [],
        })
        entry["last_session_start"] = datetime.now(timezone.utc).isoformat()
        self._save_save_sync_state()
        return {"success": True}

    async def record_session_end(self, rom_id):
        """Record end of play session, accumulate playtime delta."""
        rom_id_str = str(int(rom_id))
        playtime = self._save_sync_state.get("playtime", {})
        entry = playtime.get(rom_id_str)

        if not entry or not entry.get("last_session_start"):
            return {"success": False, "message": "No active session"}

        try:
            start = datetime.fromisoformat(entry["last_session_start"])
            now = datetime.now(timezone.utc)
            duration = (now - start).total_seconds()

            # Sanity check: clamp to 0-24h
            duration = max(0, min(duration, 86400))

            entry["total_seconds"] = entry.get("total_seconds", 0) + int(duration)
            entry["session_count"] = entry.get("session_count", 0) + 1
            entry["last_session_duration_sec"] = int(duration)
            entry["last_session_start"] = None

            self._save_save_sync_state()

            # Best-effort sync playtime to RomM server notes
            try:
                self._sync_playtime_to_romm(int(rom_id), int(duration))
            except Exception:
                pass  # Already logged inside _sync_playtime_to_romm

            return {
                "success": True,
                "duration_sec": int(duration),
                "total_seconds": entry["total_seconds"],
                "session_count": entry["session_count"],
            }
        except (ValueError, TypeError) as e:
            return {"success": False, "message": "Failed to calculate session duration"}

    async def get_save_sync_settings(self):
        """Return current save sync settings."""
        return self._save_sync_state.get("settings", {
            "save_sync_enabled": False,
            "conflict_mode": "newest_wins",
            "sync_before_launch": True,
            "sync_after_exit": True,
            "clock_skew_tolerance_sec": 60,
        })

    async def update_save_sync_settings(self, settings):
        """Update save sync settings (conflict_mode, sync toggles, etc.)."""
        allowed_keys = {
            "save_sync_enabled", "conflict_mode", "sync_before_launch",
            "sync_after_exit", "clock_skew_tolerance_sec",
        }
        valid_modes = {"newest_wins", "always_upload", "always_download", "ask_me"}

        current = self._save_sync_state.setdefault("settings", {})

        for key, value in settings.items():
            if key not in allowed_keys:
                continue
            if key == "conflict_mode" and value not in valid_modes:
                continue
            if key == "clock_skew_tolerance_sec":
                value = max(0, int(value))
            if key in ("save_sync_enabled", "sync_before_launch", "sync_after_exit"):
                value = bool(value)
            current[key] = value

        self._save_save_sync_state()
        return {"success": True, "settings": current}

    async def get_server_playtime(self, rom_id):
        """Read playtime from RomM server notes for a ROM.

        Returns local playtime, server playtime, and merged total.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        local_entry = self._save_sync_state.get("playtime", {}).get(rom_id_str, {})
        local_seconds = local_entry.get("total_seconds", 0)

        server_seconds = 0
        try:
            note = self._with_retry(self._romm_get_playtime_note, rom_id)
            if note:
                server_data = self._parse_playtime_note_content(
                    note.get("content", "")
                )
                if server_data:
                    server_seconds = int(server_data.get("seconds", 0))
        except Exception as e:
            self._log_debug(f"Failed to read server playtime for rom {rom_id}: {e}")

        return {
            "rom_id": rom_id,
            "local_seconds": local_seconds,
            "server_seconds": server_seconds,
            "total_seconds": max(local_seconds, server_seconds),
            "session_count": local_entry.get("session_count", 0),
        }

    async def get_all_playtime(self):
        """Return all local playtime entries keyed by rom_id string.

        Used by the frontend at plugin load to write playtime into Steam's UI.
        """
        return {"playtime": self._save_sync_state.get("playtime", {})}

    async def get_offline_queue(self):
        """Return failed sync operations awaiting manual retry."""
        return {"queue": self._save_sync_state.get("offline_queue", [])}

    async def retry_failed_sync(self, rom_id, filename):
        """Retry a specific failed sync operation from the offline queue."""
        rom_id = int(rom_id)
        queue = self._save_sync_state.get("offline_queue", [])

        # Find and remove the item
        item = None
        remaining = []
        for q in queue:
            if q.get("rom_id") == rom_id and q.get("filename") == filename:
                item = q
            else:
                remaining.append(q)
        self._save_sync_state["offline_queue"] = remaining

        if not item:
            return {"success": False, "message": "Item not found in queue"}

        direction = item.get("direction", "both")
        synced, errors = self._sync_rom_saves(rom_id, direction=direction)
        self._save_save_sync_state()

        if errors:
            return {"success": False, "message": errors[0], "synced": synced}
        return {"success": True, "message": f"Synced {synced} save(s)", "synced": synced}

    async def clear_offline_queue(self):
        """Clear all failed sync operations from the offline queue."""
        self._save_sync_state["offline_queue"] = []
        self._save_save_sync_state()
        return {"success": True}
