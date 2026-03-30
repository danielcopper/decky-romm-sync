"""SaveService — save sync business logic.

All RomM communication goes through ``RommApiProtocol``.
No ``import decky`` — error utilities come from ``lib.errors``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from models.saves import SaveConflict

from domain.emulator_tag import build_emulator_tag, detect_core_change
from domain.save_conflicts import (
    build_conflict_dict,
    check_local_changes,
    check_server_changes_fast,
    determine_action,
    resolve_conflict_by_mode,
)
from domain.save_extensions import get_save_extensions
from domain.save_path import resolve_save_dir
from domain.save_sync import determine_sync_action, match_local_to_server_saves
from lib.errors import RommApiError, RommConflictError, classify_error
from services.protocols import (
    CoreResolverFn,
    RetryStrategy,
    RommApiProtocol,
    RomsPathProvider,
    SavesPathProvider,
)

_DEVICE_NOT_REGISTERED = "Device not registered"
_NO_MIGRATION = object()  # sentinel: no slot migration requested

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.save_sync import MatchedSave
    from services.protocols import EventEmitter


class SaveService:
    """Bidirectional save file sync between local RetroDECK and RomM server.

    Parameters
    ----------
    romm_api:
        Protocol adapter for all RomM save/notes HTTP operations.
    retry:
        Retry strategy — provides ``with_retry`` and ``is_retryable``.
    state:
        Live reference to the main plugin state dict (``installed_roms``,
        ``shortcut_registry``).
    save_sync_state:
        Live reference to the save-sync state dict.  Caller should
        pre-populate via :meth:`init_state` / :meth:`load_state`.
    loop:
        The plugin's ``asyncio`` event loop (for ``run_in_executor``).
    logger:
        Standard-library logger (replaces ``decky.logger``).
    runtime_dir:
        Absolute path to the plugin runtime directory (for
        ``save_sync_state.json`` persistence).
    get_saves_path:
        Callable returning the current RetroDECK saves directory.
    get_roms_path:
        Callable returning the current RetroDECK roms directory.
    get_active_core:
        Callable resolving the active RetroArch core for a system/game.
        Returns ``(core_so, label)`` tuple; either may be None if unresolved.
    get_retroarch_save_sorting:
        Callable returning ``(sort_by_content, sort_by_core)`` booleans from retroarch.cfg.
    """

    _LOG_LEVELS: ClassVar[dict[str, int]] = {"debug": 0, "info": 1, "warn": 2, "error": 3}

    def __init__(
        self,
        *,
        romm_api: RommApiProtocol,
        retry: RetryStrategy,
        settings: dict,
        state: dict,
        save_sync_state: dict,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        runtime_dir: str,
        get_saves_path: SavesPathProvider,
        get_roms_path: RomsPathProvider,
        get_active_core: CoreResolverFn,
        plugin_version: str = "0.0.0",
        emit: EventEmitter | None = None,
    ) -> None:
        self._romm_api = romm_api
        self._retry = retry
        self._settings = settings
        self._state = state
        self._save_sync_state = save_sync_state
        self._loop = loop
        self._logger = logger
        self._runtime_dir = runtime_dir
        self._get_saves_path = get_saves_path
        self._get_roms_path = get_roms_path
        self._get_active_core = get_active_core
        self._plugin_version = plugin_version
        self._emit = emit

    # ------------------------------------------------------------------
    # Debug logging helper
    # ------------------------------------------------------------------

    def _log_debug(self, msg: str) -> None:
        configured = self._settings.get("log_level", "warn")
        if self._LOG_LEVELS.get("debug", 0) >= self._LOG_LEVELS.get(configured, 2):
            self._logger.info(msg)

    def _get_server_device_id(self) -> str | None:
        """Return the server device ID if registered, else None."""
        return self._save_sync_state.get("server_device_id")

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------

    @staticmethod
    def make_default_state() -> dict:
        """Return a fresh default save-sync state dict."""
        return {
            "version": 1,
            "device_id": None,
            "device_name": None,
            "server_device_id": None,
            "saves": {},
            "playtime": {},
            "settings": {
                "save_sync_enabled": False,
                "conflict_mode": "ask_me",
                "sync_before_launch": True,
                "sync_after_exit": True,
                "clock_skew_tolerance_sec": 60,
                "default_slot": "default",
                "autocleanup_limit": 10,
            },
        }

    def init_state(self) -> None:
        """Populate ``_save_sync_state`` with defaults (idempotent)."""
        defaults = self.make_default_state()
        for key, value in defaults.items():
            self._save_sync_state.setdefault(key, value)
        self._save_sync_state.setdefault("settings", {})
        for key, value in defaults["settings"].items():
            self._save_sync_state["settings"].setdefault(key, value)
        # Migrate: rename "active_core" → "last_synced_core" in per-game entries
        for _rid, entry in self._save_sync_state.get("saves", {}).items():
            if "active_core" in entry:
                entry["last_synced_core"] = entry.pop("active_core")

    def load_state(self) -> None:
        """Load save sync state from disk, merging with defaults."""
        path = os.path.join(self._runtime_dir, "save_sync_state.json")
        try:
            with open(path) as f:
                saved = json.load(f)
            for key in ("saves", "playtime"):
                if key in saved:
                    self._save_sync_state[key] = saved[key]
            for key in ("version", "device_id", "device_name", "server_device_id"):
                if key in saved:
                    self._save_sync_state[key] = saved[key]
            if "settings" in saved:
                self._save_sync_state["settings"].update(saved["settings"])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save_state(self) -> None:
        """Persist save sync state to disk (atomic write)."""
        os.makedirs(self._runtime_dir, exist_ok=True)
        path = os.path.join(self._runtime_dir, "save_sync_state.json")
        tmp = path + ".tmp"
        lock_fd = os.open(path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._save_sync_state, f, indent=2)
            os.replace(tmp, path)
        finally:
            os.close(lock_fd)

    def prune_orphaned_state(self) -> None:
        """Remove save sync state entries for rom_ids no longer in shortcut registry."""
        registry = self._state.get("shortcut_registry", {})
        changed = False

        for section in ("saves", "playtime"):
            data = self._save_sync_state.get(section, {})
            stale = [rid for rid in data if rid not in registry]
            for rid in stale:
                del data[rid]
                self._logger.info(f"Pruned orphaned save sync state: {section}[{rid}]")
            if stale:
                changed = True

        if changed:
            self.save_state()

    # ------------------------------------------------------------------
    # ROM / path helpers
    # ------------------------------------------------------------------

    def _get_rom_save_info(self, rom_id: int) -> dict | None:
        """Get save-related info for an installed ROM.

        Returns dict with keys: system, rom_name, saves_dir, platform_slug, file_path
        or None if not installed.
        """
        rom_id_str = str(int(rom_id))
        installed = self._state["installed_roms"].get(rom_id_str)
        if not installed:
            return None
        system = installed.get("system", "")
        file_path = installed.get("file_path", "")
        platform_slug = installed.get("platform_slug", "")
        if not system or not file_path:
            return None
        rom_name = os.path.splitext(os.path.basename(file_path))[0]

        # Use domain save path resolution.
        # Read sort settings from state (populated by MigrationService at startup).
        saves_base = self._get_saves_path()
        roms_base = self._get_roms_path()
        sort_state = self._state.get("save_sort_settings")
        if sort_state:
            sort_by_content = sort_state.get("sort_by_content", True)
            sort_by_core = sort_state.get("sort_by_core", False)
        else:
            sort_by_content, sort_by_core = True, False  # RetroDECK defaults
        saves_dir = resolve_save_dir(
            file_path,
            saves_base,
            system,
            roms_base=roms_base,
            sort_by_content=sort_by_content,
            sort_by_core=sort_by_core,
        )

        return {
            "system": system,
            "rom_name": rom_name,
            "saves_dir": saves_dir,
            "platform_slug": platform_slug,
            "file_path": file_path,
        }

    def _is_save_sort_changed(self) -> bool:
        """Check if a save sort migration is pending (detected by MigrationService)."""
        return self._state.get("save_sort_settings_previous") is not None

    # ------------------------------------------------------------------
    # File Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _file_md5(path: str) -> str:
        """Compute MD5 hash of a file."""
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _find_save_files(self, rom_id: int) -> list[dict]:
        """Find local save files for a ROM.

        Returns list of ``{"path": str, "filename": str}``.
        """
        info = self._get_rom_save_info(rom_id)
        if not info:
            return []
        rom_name = info["rom_name"]
        saves_dir = info["saves_dir"]
        platform_slug = info["platform_slug"]
        if not os.path.isdir(saves_dir):
            return []
        results = []
        for ext in get_save_extensions(platform_slug):
            save_path = os.path.join(saves_dir, rom_name + ext)
            if os.path.isfile(save_path):
                results.append({"path": save_path, "filename": rom_name + ext})
        return results

    # ------------------------------------------------------------------
    # Playtime Notes API Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Server Save Hash Helper
    # ------------------------------------------------------------------

    def _get_server_save_hash(self, server_save: dict) -> str | None:
        """Download a server save to temp and compute its MD5 hash.

        Used for slow-path conflict detection when no content_hash is available.
        Returns hash string or None on non-retryable error.
        Raises on retryable errors so the caller can retry.
        """
        save_id = server_save.get("id")
        if not save_id:
            return None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp")
            os.close(fd)
            self._romm_api.download_save(save_id, tmp_path)
            return self._file_md5(tmp_path)
        except Exception as e:
            self._log_debug(f"Failed to hash server save {save_id}: {e}")
            if self._retry.is_retryable(e):
                raise
            return None
        finally:
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)

    # ------------------------------------------------------------------
    # Conflict Detection
    # ------------------------------------------------------------------

    def _check_server_changes(self, file_state: dict, server_save: dict, last_sync_hash: str) -> bool:
        """Compare server metadata/hash against baseline to detect server modifications."""
        fast = check_server_changes_fast(file_state, server_save)
        if fast is not None:
            return fast

        # Slow path: timestamp changed or no stored timestamp — download and hash
        server_updated_at = server_save.get("updated_at", "")
        server_size = server_save.get("file_size_bytes")
        try:
            server_hash = self._retry.with_retry(self._get_server_save_hash, server_save)
        except Exception:
            server_hash = None
        if server_hash and server_hash != last_sync_hash:
            return True

        # False alarm — update stored metadata
        if file_state:
            file_state["last_sync_server_updated_at"] = server_updated_at
            if server_size is not None:
                file_state["last_sync_server_size"] = server_size
        return False

    def _extract_device_sync_info(self, server_save: dict) -> dict | None:
        """Extract this device's sync info from server save response.

        Returns the device_syncs entry for our server_device_id, or None.
        """
        server_device_id = self._get_server_device_id()
        if not server_device_id:
            return None
        device_syncs = server_save.get("device_syncs", [])
        for sync in device_syncs:
            if str(sync.get("device_id")) == server_device_id:
                return sync
        return None

    def _detect_conflict(self, rom_id: int, filename: str, local_hash: str | None, server_save: dict) -> str:
        """Hybrid conflict detection (no content_hash on RomM 4.6.1).

        Returns: ``"skip"``, ``"download"``, ``"upload"``, or ``"conflict"``.
        """
        rom_id_str = str(int(rom_id))
        save_state = self._save_sync_state["saves"].get(rom_id_str, {})
        file_state = save_state.get("files", {}).get(filename, {})
        last_sync_hash = file_state.get("last_sync_hash")

        # Never synced before — state recovery
        if not last_sync_hash:
            if local_hash:
                try:
                    server_hash = self._retry.with_retry(self._get_server_save_hash, server_save)
                except Exception:
                    server_hash = None
                if server_hash is None:
                    return "conflict"  # Can't verify, ask user
                return "skip" if local_hash == server_hash else "conflict"
            return "download"

        local_changed = check_local_changes(local_hash, last_sync_hash)

        # v4.7: try device_syncs from server response
        device_sync_info = self._extract_device_sync_info(server_save)
        if device_sync_info is not None:
            # Use v4.7 path — avoids expensive server hash download
            result = determine_sync_action(local_changed, server_save, device_sync_info, file_state)
            self._log_debug(
                f"_detect_conflict({rom_id}, {filename}): v4.7 path "
                f"local_changed={local_changed} is_current={device_sync_info.get('is_current')} → {result}"
            )
            return result

        # v4.6 fallback: existing slow-path logic
        server_changed = self._check_server_changes(file_state, server_save, last_sync_hash)
        result = determine_action(local_changed, server_changed)

        self._log_debug(
            f"_detect_conflict({rom_id}, {filename}): v4.6 path "
            f"local_hash={local_hash[:8] if local_hash else None}… "
            f"baseline={last_sync_hash[:8] if last_sync_hash else None}… "
            f"local_changed={local_changed} server_changed={server_changed} → {result}"
        )
        return result

    def _resolve_conflict_by_mode(self, local_mtime: float, server_save: dict) -> str:
        """Wrapper: apply configured conflict resolution mode via domain function."""
        settings = self._save_sync_state.get("settings", {})
        mode = settings.get("conflict_mode", "ask_me")
        tolerance = settings.get("clock_skew_tolerance_sec", 60)
        return resolve_conflict_by_mode(mode, local_mtime, server_save, tolerance)

    def _update_file_sync_state(
        self,
        rom_id_str: str,
        filename: str,
        server_response: dict,
        local_path: str,
        system: str,
        *,
        emulator_tag: str | None = None,
        core_so: str | None = None,
    ) -> None:
        """Update per-file sync tracking after a successful sync operation."""
        if rom_id_str not in self._save_sync_state["saves"]:
            self._save_sync_state["saves"][rom_id_str] = {
                "files": {},
                "emulator": emulator_tag or "retroarch",
                "system": system,
                "last_synced_core": core_so,
                "active_slot": self._save_sync_state.get("settings", {}).get("default_slot", "default"),
            }
        save_entry = self._save_sync_state["saves"][rom_id_str]
        save_entry.setdefault("files", {})
        if emulator_tag is not None:
            save_entry["emulator"] = emulator_tag
        if core_so is not None:
            save_entry["last_synced_core"] = core_so

        now = datetime.now(UTC).isoformat()
        local_hash = self._file_md5(local_path) if os.path.isfile(local_path) else ""

        save_entry["files"][filename] = {
            "last_sync_hash": local_hash,
            "last_sync_at": now,
            "last_sync_server_updated_at": server_response.get("updated_at", now),
            "last_sync_server_save_id": server_response.get("id"),
            "last_sync_server_size": server_response.get("file_size_bytes"),
            "last_sync_local_mtime": os.path.getmtime(local_path) if os.path.isfile(local_path) else None,
            "last_sync_local_size": os.path.getsize(local_path) if os.path.isfile(local_path) else None,
            "tracked_save_id": server_response.get("id"),
        }

    # ------------------------------------------------------------------
    # Sync Helpers
    # ------------------------------------------------------------------

    def _do_download_save(self, server_save: dict, saves_dir: str, filename: str, rom_id_str: str, system: str) -> None:
        """Download a save file from server. Backs up existing local file first."""
        local_path = os.path.join(saves_dir, filename)
        os.makedirs(saves_dir, exist_ok=True)
        tmp_path = local_path + ".tmp"

        # Use v4.7 download_save_content with device_id (marks device as synced on server)
        # Falls back to v4.6 download_save if download_save_content is not available
        device_id = self._get_server_device_id()
        if device_id and self._romm_api.supports_device_sync():
            self._retry.with_retry(
                lambda: self._romm_api.download_save_content(
                    server_save["id"],
                    tmp_path,
                    device_id=device_id,
                    optimistic=True,
                ),
            )
        else:
            self._retry.with_retry(lambda: self._romm_api.download_save(server_save["id"], tmp_path))

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

    def _do_upload_save(
        self,
        rom_id: int,
        file_path: str,
        filename: str,
        rom_id_str: str,
        system: str,
        server_save: dict | None = None,
    ) -> dict:
        """Upload a local save file to server."""
        save_id = server_save.get("id") if server_save else None

        # Resolve active core for emulator tag
        installed = self._state["installed_roms"].get(rom_id_str, {})
        rom_filename = os.path.basename(installed.get("file_path", "")) or None
        core_so, _label = self._get_active_core(system, rom_filename)
        emulator = build_emulator_tag(core_so)

        # v4.7: pass device_id and slot
        device_id = self._get_server_device_id()
        game_state = self._save_sync_state.get("saves", {}).get(rom_id_str, {})
        slot = game_state.get("active_slot", "default") if device_id else None

        result = self._retry.with_retry(
            lambda: self._romm_api.upload_save(
                int(rom_id), file_path, emulator, save_id, device_id=device_id, slot=slot
            )
        )

        self._update_file_sync_state(
            rom_id_str, filename, result, file_path, system, emulator_tag=emulator, core_so=core_so
        )

        # Promote local slot to server after successful upload
        if slot:
            slots_dict = self._save_sync_state.get("saves", {}).get(rom_id_str, {}).get("slots", {})
            if slot in slots_dict and slots_dict[slot].get("source") == "local":
                slots_dict[slot]["source"] = "server"
                slots_dict[slot]["count"] = 1

        # Mark device as synced with the uploaded save version.
        # RomM's upload endpoint updates updated_at but NOT last_synced_at in
        # DeviceSaveSync, so is_current would be False on the next list_saves.
        upload_id = result.get("id")
        if device_id and upload_id and self._romm_api.supports_device_sync():
            try:
                self._romm_api.confirm_download(upload_id, device_id)
            except Exception:
                self._log_debug(f"confirm_download after upload failed for save {upload_id} (non-fatal)")

        self._log_debug(f"Uploaded save: {filename} for rom {rom_id_str} (emulator={emulator})")
        return result

    def _sync_single_save_file(
        self,
        rom_id: int,
        filename: str,
        local: dict | None,
        server: dict | None,
    ) -> tuple[str, str]:
        """Determine and resolve the sync action for one save file.

        Returns ``(action, local_hash)`` where action is the *resolved*
        action after conflict-mode processing (may be ``"ask"``).
        """
        local_hash = ""
        if local and server:
            local_hash = self._file_md5(local["path"])
            action = self._detect_conflict(rom_id, filename, local_hash, server)
        elif local:
            action = "upload"
        elif server:
            action = "download"
        else:
            return "none", local_hash

        if action == "skip":
            return "skip", local_hash

        if action == "conflict":
            assert server is not None
            local_mtime = os.path.getmtime(local["path"]) if local else 0
            resolution = self._resolve_conflict_by_mode(local_mtime, server)
            if resolution == "ask":
                return "ask", local_hash
            action = resolution

        return action, local_hash

    def _handle_conflict_error(
        self,
        rom_id: int,
        filename: str,
        local: dict | None,
        server: dict | None,
        local_hash: str,
        errors: list[str],
        conflicts: list[SaveConflict | dict],
    ) -> None:
        """Handle a RommConflictError by recording a conflict or error entry."""
        if local and server:
            local_path = local["path"]
            local_info = {
                "path": local_path,
                "mtime": os.path.getmtime(local_path) if os.path.isfile(local_path) else None,
                "size": os.path.getsize(local_path) if os.path.isfile(local_path) else None,
            }
            conflicts.append(build_conflict_dict(rom_id, filename, local_info, local_hash, server))
        else:
            errors.append(f"{filename}: conflict without matching local+server")

    def _handle_unexpected_error(
        self,
        e: Exception,
        filename: str,
        saves_dir: str,
        errors: list[str],
    ) -> None:
        """Handle an unexpected exception by recording an error and cleaning up temp files."""
        _code, _msg = classify_error(e)
        errors.append(f"{filename}: {_msg}")
        tmp = os.path.join(saves_dir, filename + ".tmp")
        with contextlib.suppress(OSError):
            os.remove(tmp)

    def _execute_sync_action(
        self,
        action: str,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local: dict | None,
        server: dict | None,
        local_hash: str,
        saves_dir: str,
        system: str,
        errors: list[str],
        conflicts: list[SaveConflict | dict],
    ) -> bool:
        """Execute a resolved sync action (download/upload). Returns True if synced."""
        try:
            if action == "download":
                assert server is not None
                self._do_download_save(server, saves_dir, filename, rom_id_str, system)
                return True
            if action == "upload" and local:
                self._do_upload_save(rom_id, local["path"], filename, rom_id_str, system, server)
                return True
        except RommConflictError:
            self._handle_conflict_error(rom_id, filename, local, server, local_hash, errors, conflicts)
        except RommApiError as e:
            _code, _msg = classify_error(e)
            errors.append(f"{filename}: {_msg}")
        except Exception as e:
            self._handle_unexpected_error(e, filename, saves_dir, errors)
        return False

    def _process_single_file_sync(
        self,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local: dict | None,
        server: dict | None,
        saves_dir: str,
        system: str,
        errors: list[str],
        conflicts: list[SaveConflict | dict],
    ) -> bool:
        """Process sync for one save file. Returns True if a file was synced."""
        t_file = time.time()
        action, local_hash = self._sync_single_save_file(rom_id, filename, local, server)

        self._log_debug(
            f"[TIMING] _sync_rom_saves({rom_id}): detect {filename} -> {action} {time.time() - t_file:.3f}s"
        )

        if action in ("skip", "none"):
            return False

        if action == "ask":
            if local and server:
                local_path = local["path"]
                local_info = {
                    "path": local_path,
                    "mtime": os.path.getmtime(local_path) if os.path.isfile(local_path) else None,
                    "size": os.path.getsize(local_path) if os.path.isfile(local_path) else None,
                }
                conflicts.append(build_conflict_dict(rom_id, filename, local_info, local_hash, server))
            return False

        t_action = time.time()
        result = self._execute_sync_action(
            action,
            rom_id,
            rom_id_str,
            filename,
            local,
            server,
            local_hash,
            saves_dir,
            system,
            errors,
            conflicts,
        )
        self._log_debug(f"[TIMING] _sync_rom_saves({rom_id}): {action} {filename} {time.time() - t_action:.3f}s")
        return result

    def _check_newer_in_slot(
        self,
        m: MatchedSave,
        files_state: dict,
        rom_id: int,
        save_state: dict,
        conflicts: list[SaveConflict | dict],
    ) -> bool:
        """Check if a matched save has a newer version in its slot from another device.

        Returns True if the normal sync step should be skipped (conflict appended).
        """
        if not m.newer_save_in_slot:
            return False
        file_state = files_state.get(m.filename, {})
        dismissed_id = file_state.get("dismissed_newer_save_id")
        newer_id = m.newer_save_in_slot.get("id")
        if dismissed_id is None or (newer_id is not None and newer_id > dismissed_id):
            conflicts.append(
                self._build_newer_in_slot_conflict(
                    rom_id,
                    m.filename,
                    m.server_save,
                    m.newer_save_in_slot,
                    save_state.get("active_slot"),
                )
            )
            return True
        return False

    @staticmethod
    def _build_newer_in_slot_conflict(
        rom_id: int,
        filename: str,
        tracked_save: dict | None,
        newer_save: dict,
        slot: str | None,
    ) -> dict:
        """Build a newer-in-slot conflict descriptor for the frontend."""
        return {
            "type": "newer_in_slot",
            "rom_id": rom_id,
            "filename": filename,
            "tracked_save_id": tracked_save.get("id") if tracked_save else None,
            "tracked_updated_at": tracked_save.get("updated_at") if tracked_save else None,
            "newer_save_id": newer_save.get("id"),
            "newer_updated_at": newer_save.get("updated_at"),
            "slot": slot,
        }

    def _sync_rom_saves(self, rom_id: int) -> tuple[int, list[str], list[SaveConflict | dict]]:
        """Sync saves for a single ROM (always bidirectional).

        Returns ``(synced_count, errors_list, conflicts_list)``.
        """
        t_total = time.time()
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        info = self._get_rom_save_info(rom_id)
        if not info:
            self._log_debug(f"_sync_rom_saves({rom_id}): no save info, skipping")
            return 0, [], []
        system = info["system"]
        rom_name = info["rom_name"]
        saves_dir = info["saves_dir"]

        # Fetch server saves (with retry)
        t0 = time.time()
        try:
            device_id = self._get_server_device_id()
            server_saves = self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id))
        except Exception as e:
            self._logger.error(f"_sync_rom_saves({rom_id}): failed to list saves: {e}")
            _code, _msg = classify_error(e)
            return 0, [f"Failed to fetch saves: {_msg}"], []
        self._log_debug(f"[TIMING] _sync_rom_saves({rom_id}): list_saves {time.time() - t0:.3f}s")

        t0 = time.time()
        local_files = self._find_save_files(rom_id)
        self._log_debug(
            f"_sync_rom_saves({rom_id}): system={system}, rom_name={rom_name}, "
            f"local_files={len(local_files)}, server_saves={len(server_saves)}, "
            f"saves_dir={saves_dir}"
        )
        self._log_debug(f"[TIMING] _sync_rom_saves({rom_id}): find_local {time.time() - t0:.3f}s")

        save_state = self._save_sync_state["saves"].get(rom_id_str, {})
        files_state = save_state.get("files", {})

        # Match local files to server saves (domain logic)
        match_result = match_local_to_server_saves(
            local_files,
            server_saves,
            files_state,
            save_state.get("active_slot"),
            rom_name,
            device_id=device_id,
        )

        # Persist any new tracked_save_ids discovered by fallback matching
        for fn, save_id in match_result.new_tracked_ids.items():
            files_state.setdefault(fn, {})["tracked_save_id"] = save_id
            self._log_debug(f"Fallback match: {fn} -> server save id={save_id}")

        synced = 0
        errors: list[str] = []
        conflicts: list[SaveConflict | dict] = []

        for m in match_result.matched:
            # Check for newer-in-slot before normal sync
            if self._check_newer_in_slot(m, files_state, rom_id, save_state, conflicts):
                continue  # Skip normal sync

            method_label = f" [{m.match_method}]" if m.match_method not in ("filename", "local_only") else ""
            self._log_debug(
                f"_sync_rom_saves({rom_id}): {m.filename}{method_label} "
                f"local={'yes' if m.local_file else 'no'} server={m.server_save.get('id') if m.server_save else 'none'}"
            )
            if self._process_single_file_sync(
                rom_id, rom_id_str, m.filename, m.local_file, m.server_save, saves_dir, system, errors, conflicts
            ):
                synced += 1

        # Record when this sync check ran (regardless of whether files transferred)
        save_entry = self._save_sync_state["saves"].setdefault(rom_id_str, {})
        save_entry["last_sync_check_at"] = datetime.now(UTC).isoformat()

        self._log_debug(
            f"[TIMING] _sync_rom_saves({rom_id}): TOTAL {time.time() - t_total:.3f}s"
            f" synced={synced} errors={len(errors)}"
        )
        return synced, errors, conflicts

    def _is_save_sync_enabled(self) -> bool:
        """Check if save sync feature is enabled."""
        return self._save_sync_state.get("settings", {}).get("save_sync_enabled", False)

    @staticmethod
    def _build_file_status(
        filename: str,
        *,
        local_path: str | None,
        local_hash: str | None,
        local_mtime: str | None,
        local_size: int | None,
        server: dict | None,
        last_sync_at: str | None,
        status: str,
        server_device_id: str | None = None,
    ) -> dict:
        """Build a file status dict for the frontend."""
        server_device_syncs = server.get("device_syncs", []) if server else []
        device_syncs = [
            {
                "device_id": ds.get("device_id", ""),
                "device_name": ds.get("device_name", ""),
                "is_current": ds.get("is_current", False),
                "last_synced_at": ds.get("last_synced_at"),
            }
            for ds in server_device_syncs
        ]
        own_sync = (
            next(
                (ds for ds in server_device_syncs if ds.get("device_id") == server_device_id),
                None,
            )
            if server_device_id
            else None
        )
        is_current = own_sync.get("is_current", True) if own_sync else True

        return {
            "filename": filename,
            "local_path": local_path,
            "local_hash": local_hash,
            "local_mtime": local_mtime,
            "local_size": local_size,
            "server_save_id": server.get("id") if server else None,
            "server_updated_at": server.get("updated_at", "") if server else None,
            "server_size": server.get("file_size_bytes") if server else None,
            "last_sync_at": last_sync_at,
            "status": status,
            "device_syncs": device_syncs,
            "is_current": is_current,
        }

    def _get_save_status_io(self, rom_id: int, server_saves: list[dict]) -> dict:
        """Sync helper for get_save_status — runs in executor.

        Performs local file checks, MD5 hashing, and conflict detection.
        """
        rom_id_str = str(rom_id)
        local_files = self._find_save_files(rom_id)
        info = self._get_rom_save_info(rom_id)
        rom_name = info["rom_name"] if info else None

        save_state = self._save_sync_state["saves"].get(rom_id_str, {})
        files_state = save_state.get("files", {})

        # Match local files to server saves (same domain logic as _sync_rom_saves)
        match_result = match_local_to_server_saves(
            local_files,
            server_saves,
            files_state,
            save_state.get("active_slot"),
            rom_name,
        )

        file_statuses = []
        for m in match_result.matched:
            if m.local_file:
                local_hash = self._file_md5(m.local_file["path"])
                server = m.server_save
                if server:
                    action = self._detect_conflict(rom_id, m.filename, local_hash, server)
                elif local_hash:
                    action = "upload"
                else:
                    action = "skip"
                file_statuses.append(
                    self._build_file_status(
                        m.filename,
                        local_path=m.local_file["path"],
                        local_hash=local_hash,
                        local_mtime=datetime.fromtimestamp(os.path.getmtime(m.local_file["path"]), tz=UTC).isoformat(),
                        local_size=os.path.getsize(m.local_file["path"]),
                        server=server,
                        last_sync_at=files_state.get(m.filename, {}).get("last_sync_at"),
                        status=action,
                        server_device_id=self._get_server_device_id(),
                    )
                )
            elif m.server_save:
                # Server-only
                file_statuses.append(
                    self._build_file_status(
                        m.filename,
                        local_path=None,
                        local_hash=None,
                        local_mtime=None,
                        local_size=None,
                        server=m.server_save,
                        last_sync_at=None,
                        status="download",
                        server_device_id=self._get_server_device_id(),
                    )
                )

        playtime = self._save_sync_state.get("playtime", {}).get(rom_id_str, {})
        save_entry = self._save_sync_state.get("saves", {}).get(rom_id_str, {})

        conflicts = [
            {
                "rom_id": rom_id,
                "filename": fs["filename"],
                "local_path": fs["local_path"],
                "local_hash": fs.get("local_hash"),
                "local_mtime": fs.get("local_mtime"),
                "local_size": fs.get("local_size"),
                "server_save_id": fs.get("server_save_id") or 0,
                "server_updated_at": fs.get("server_updated_at") or "",
                "server_size": fs.get("server_size"),
                "created_at": datetime.now(UTC).isoformat(),
            }
            for fs in file_statuses
            if fs["status"] == "conflict"
        ]

        return {
            "rom_id": rom_id,
            "files": file_statuses,
            "playtime": playtime,
            "device_id": self._save_sync_state.get("device_id", ""),
            "last_sync_check_at": save_entry.get("last_sync_check_at"),
            "conflicts": conflicts,
            "save_sort_changed": self._is_save_sort_changed(),
        }

    def _resolve_conflict_io(
        self,
        rom_id: int,
        rom_id_str: str,
        resolution: str,
        conflict: dict,
        saves_dir: str,
        filename: str,
        system: str,
    ) -> dict | None:
        """Sync helper for resolve_conflict — performs blocking I/O in executor."""
        if resolution == "download":
            server_save_id = conflict.get("server_save_id")
            if not server_save_id:
                return {"success": False, "message": "No server save ID"}
            server_save = self._retry.with_retry(lambda: self._romm_api.get_save_metadata(server_save_id))
            self._do_download_save(server_save, saves_dir, filename, rom_id_str, system)
        else:  # upload
            local_path = conflict.get("local_path")
            if not local_path or not os.path.isfile(local_path):
                return {"success": False, "message": "Local file not found"}
            server_save = None
            if conflict.get("server_save_id"):
                with contextlib.suppress(Exception):
                    ssid = conflict["server_save_id"]
                    server_save = self._retry.with_retry(lambda: self._romm_api.get_save_metadata(ssid))
            self._do_upload_save(rom_id, local_path, filename, rom_id_str, system, server_save)
        return None  # Success — caller handles state update

    # ------------------------------------------------------------------
    # Public async API (callable endpoints)
    # ------------------------------------------------------------------

    def ensure_device_registered(self) -> dict:
        """Ensure this device has a unique ID for save sync tracking.

        v4.7+: Register with RomM server via register_device() API.
        v4.6: Generate local UUID (no server registration).
        """
        if not self._is_save_sync_enabled():
            return {"success": False, "device_id": "", "device_name": "", "disabled": True}

        # Already fully registered (local + server, or local-only on v4.6)
        has_device_id = self._save_sync_state.get("device_id")
        has_server_id = self._save_sync_state.get("server_device_id")
        if has_device_id and (has_server_id or not self._romm_api.supports_device_sync()):
            return {
                "success": True,
                "device_id": self._save_sync_state["device_id"],
                "device_name": self._save_sync_state.get("device_name", ""),
                "server_device_id": has_server_id,
            }

        hostname = socket.gethostname()

        # Try v4.7 server registration (also upgrades local-only UUID to server-registered)
        if self._romm_api.supports_device_sync():
            try:
                result = self._romm_api.register_device(
                    name=hostname,
                    platform="linux",
                    client="decky-romm-sync",
                    version=self._plugin_version,
                )
                server_device_id = result.get("id") or result.get("device_id")
                if server_device_id:
                    self._save_sync_state["device_id"] = str(server_device_id)
                    self._save_sync_state["device_name"] = hostname
                    self._save_sync_state["server_device_id"] = str(server_device_id)
                    self.save_state()
                    self._logger.info(f"Device registered with server: {server_device_id} ({hostname})")
                    return {
                        "success": True,
                        "device_id": str(server_device_id),
                        "device_name": hostname,
                        "server_device_id": str(server_device_id),
                    }
            except Exception as e:
                self._logger.warning(f"Server device registration failed, falling back to local: {e}")

        # v4.6 fallback or server registration failed
        device_id = str(uuid.uuid4())
        self._save_sync_state["device_id"] = device_id
        self._save_sync_state["device_name"] = hostname
        self.save_state()
        self._logger.info(f"Device ID generated (local): {device_id} ({hostname})")
        return {"success": True, "device_id": device_id, "device_name": hostname}

    async def get_save_status(self, rom_id: int) -> dict:
        """Get save sync status for a ROM (local files, server saves, conflict state)."""
        rom_id = int(rom_id)

        server_saves: list[dict] = []
        try:
            device_id = self._get_server_device_id()
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id)),
            )
        except Exception as e:
            self._log_debug(f"Failed to fetch saves for rom {rom_id}: {e}")

        return await self._loop.run_in_executor(None, self._get_save_status_io, rom_id, server_saves)

    async def check_save_status_background(self, rom_id: int) -> None:
        """Run full save status check in background and emit result to frontend."""
        try:
            result = await self.get_save_status(rom_id)
            if self._emit is not None:
                await self._emit("save_status_updated", result)
        except Exception as e:
            self._log_debug(f"Background save status check failed for rom {rom_id}: {e}")

    def check_core_change(self, rom_id: int) -> dict:
        """Check if emulator core changed since last sync for a ROM."""
        if not self._is_save_sync_enabled():
            return {"changed": False}

        rom_id_str = str(rom_id)
        save_entry = self._save_sync_state.get("saves", {}).get(rom_id_str)
        if not save_entry:
            return {"changed": False}  # Never synced

        stored_core = save_entry.get("last_synced_core")
        system = save_entry.get("system")
        if not stored_core or not system:
            return {"changed": False}

        # Resolve ROM filename for per-game core detection
        rom_filename = None
        installed = self._state.get("installed_roms", {}).get(rom_id_str)
        if installed:
            file_path = installed.get("file_path", "")
            if file_path:
                rom_filename = os.path.basename(file_path)

        # TODO: Core labels come from ES-DE config which may differ from RetroArch's
        # corename (e.g. "Snes9x - Current" vs "Snes9x"). Align with RetroArch
        # core names when #208 is resolved.
        try:
            active_core, active_label = self._get_active_core(system, rom_filename)
        except Exception:
            return {"changed": False}

        changed = detect_core_change(stored_core, active_core)

        if not changed:
            return {"changed": False}

        # Strip _libretro suffix for display (stored_core is guaranteed non-None here)
        old_label = stored_core.replace("_libretro", "")

        return {
            "changed": True,
            "old_core": stored_core,
            "new_core": active_core,
            "old_label": old_label,
            "new_label": active_label or (active_core.replace("_libretro", "") if active_core else None),
        }

    async def pre_launch_sync(self, rom_id: int) -> dict:
        """Download newer saves from server before game launch."""
        if not self._is_save_sync_enabled():
            return {"success": True, "message": "Save sync disabled", "synced": 0}

        if self._is_save_sort_changed():
            return {
                "success": False,
                "message": "RetroArch save sorting changed — migrate saves in Settings first",
                "synced": 0,
                "save_sort_changed": True,
            }

        settings = self._save_sync_state.get("settings", {})
        if not settings.get("sync_before_launch", True):
            return {"success": True, "message": "Pre-launch sync disabled", "synced": 0}

        if not self._save_sync_state.get("device_id"):
            reg = self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": _DEVICE_NOT_REGISTERED}

        synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
        self.save_state()

        msg = f"Downloaded {synced} save(s)"
        if errors:
            msg += f", {len(errors)} error(s)"
        return {
            "success": len(errors) == 0,
            "message": msg,
            "synced": synced,
            "errors": errors,
            "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
        }

    async def post_exit_sync(self, rom_id: int) -> dict:
        """Upload changed saves after game exit."""
        self._logger.info("post_exit_sync called for rom_id=%d", rom_id)

        if not self._is_save_sync_enabled():
            self._logger.info("post_exit_sync skipped: save sync disabled")
            return {"success": True, "message": "Save sync disabled", "synced": 0}

        settings = self._save_sync_state.get("settings", {})
        if not settings.get("sync_after_exit", True):
            self._logger.info("post_exit_sync skipped: sync_after_exit disabled")
            return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

        try:
            await self._loop.run_in_executor(None, self._romm_api.heartbeat)
        except Exception:
            self._logger.info("post_exit_sync skipped: server offline")
            return {"success": False, "message": "Server offline", "synced": 0, "offline": True}

        if not self._save_sync_state.get("device_id"):
            reg = self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": _DEVICE_NOT_REGISTERED}

        synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
        self.save_state()

        self._logger.info(
            "post_exit_sync complete for rom_id=%d: synced=%d, errors=%d, conflicts=%d",
            rom_id,
            synced,
            len(errors),
            len(conflicts),
        )

        msg = f"Uploaded {synced} save(s)"
        if errors:
            msg += f", {len(errors)} error(s)"
        return {
            "success": len(errors) == 0,
            "message": msg,
            "synced": synced,
            "errors": errors,
            "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
        }

    async def sync_rom_saves(self, rom_id: int) -> dict:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        if not self._is_save_sync_enabled():
            return {"success": False, "message": "Save sync is disabled", "synced": 0}

        if not self._save_sync_state.get("device_id"):
            reg = self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": _DEVICE_NOT_REGISTERED}

        synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, int(rom_id))
        self.save_state()

        msg = f"Synced {synced} save(s)"
        if errors:
            msg += f", {len(errors)} error(s)"
        return {
            "success": len(errors) == 0,
            "message": msg,
            "synced": synced,
            "errors": errors,
            "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
        }

    async def get_save_slots(self, rom_id: int) -> dict:
        """List available save slots for a ROM.

        Merges server slots with locally-created slots. Persists the merged
        result so local slots survive restarts. Promotes local slots to server
        when they appear on the server. Removes server slots that no longer
        exist on the server (unless they are the active_slot).
        """
        rom_id = int(rom_id)
        if not self._is_save_sync_enabled():
            return {"success": False, "slots": [], "active_slot": "default"}

        rom_id_str = str(rom_id)
        device_id = self._get_server_device_id()
        rom_state = self._save_sync_state.get("saves", {}).get(rom_id_str, {})
        active_slot = rom_state.get(
            "active_slot",
            self._save_sync_state.get("settings", {}).get("default_slot", "default"),
        )
        persisted_slots: dict[str, dict] = rom_state.get("slots", {})

        # Fetch server slots
        server_slots_list: list[dict] = []
        try:
            summary = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.get_save_summary(rom_id, device_id=device_id),
                ),
            )
            server_slots_list = summary.get("slots", [])
        except Exception as e:
            self._log_debug(f"Failed to fetch save slots for rom {rom_id}: {e}")

        # Merge: update persisted slots with server data, promote local→server
        merged: dict[str, dict] = {}
        for s in server_slots_list:
            name = s.get("slot") or s.get("slot_name") or "default"
            merged[name] = {
                "source": "server",
                "count": s.get("count", 0),
                "latest_updated_at": s.get("latest_updated_at"),
            }

        # Keep local slots that are NOT on server
        for name, info in persisted_slots.items():
            if name not in merged:
                if info.get("source") == "local":
                    # Still local — keep it
                    merged[name] = {"source": "local", "count": 0, "latest_updated_at": None}
                # If it was "server" but is gone from server now — drop it
                # (unless it's the active_slot)
                elif info.get("source") == "server" and name == active_slot:
                    merged[name] = {"source": "server", "count": 0, "latest_updated_at": None}

        # Persist merged slots in state
        game_entry = self._save_sync_state.setdefault("saves", {}).setdefault(rom_id_str, {})
        game_entry["slots"] = merged
        self.save_state()

        # Build response list
        result_slots = [
            {
                "slot": name,
                "source": info.get("source", "server"),
                "count": info.get("count", 0),
                "latest_updated_at": info.get("latest_updated_at"),
            }
            for name, info in sorted(merged.items())
        ]

        return {"success": True, "slots": result_slots, "active_slot": active_slot}

    def set_game_slot(self, rom_id: int, slot: str) -> dict:
        """Set the active save slot for a specific game.

        If the slot doesn't exist yet (not on server), it is persisted
        as a local slot. It will be promoted to server once a save is
        uploaded to it.
        """
        rom_id = int(rom_id)
        slot_str = str(slot).strip() if slot else ""
        # Empty string = legacy mode (None slot)
        resolved_slot: str | None = slot_str if slot_str else None

        rom_id_str = str(rom_id)
        saves = self._save_sync_state.setdefault("saves", {})
        if rom_id_str not in saves:
            saves[rom_id_str] = {"files": {}, "active_slot": resolved_slot}
        else:
            saves[rom_id_str]["active_slot"] = resolved_slot

        # Ensure slot is in the persisted slots dict (skip for None/legacy)
        if resolved_slot is not None:
            slots_dict: dict[str, dict] = saves[rom_id_str].setdefault("slots", {})
            if resolved_slot not in slots_dict:
                slots_dict[resolved_slot] = {"source": "local", "count": 0, "latest_updated_at": None}

        self.save_state()
        self._loop.create_task(self.check_save_status_background(rom_id))
        return {"success": True, "active_slot": resolved_slot}

    # ------------------------------------------------------------------
    # Save Setup Wizard
    # ------------------------------------------------------------------

    def is_save_tracking_configured(self, rom_id: int) -> dict:
        """Check if save slot tracking is configured for a game.

        Fast, synchronous check — reads only from local state.
        Returns {"configured": bool, "active_slot": str|None}
        """
        rom_id_str = str(int(rom_id))
        game_state = self._save_sync_state["saves"].get(rom_id_str, {})
        configured = game_state.get("slot_confirmed", False)
        active_slot = game_state.get("active_slot") if configured else None
        return {"configured": configured, "active_slot": active_slot}

    async def get_save_setup_info(self, rom_id: int) -> dict:
        """Get info needed for the first-sync setup wizard.

        Fetches server saves, checks local files, determines which
        scenario (A-E) applies so the frontend can display the right UI.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        # Local saves
        local_files = self._find_save_files(rom_id)
        local_file_info = []
        for lf in local_files:
            local_file_info.append(
                {
                    "filename": lf["filename"],
                    "size": os.path.getsize(lf["path"]) if os.path.isfile(lf["path"]) else 0,
                }
            )

        # Server saves
        server_saves: list[dict] = []
        device_id = self._get_server_device_id()
        try:
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                ),
            )
        except Exception as e:
            self._log_debug(f"get_save_setup_info({rom_id}): failed to list saves: {e}")

        # Group server saves by slot
        slots_map: dict[str | None, list[dict]] = {}
        for ss in server_saves:
            slot_key = ss.get("slot")
            slots_map.setdefault(slot_key, []).append(ss)

        server_slots = []
        for slot_key, saves in slots_map.items():
            latest = max((s.get("updated_at", "") for s in saves), default=None)
            server_slots.append(
                {
                    "slot": slot_key,
                    "saves": [
                        {
                            "id": s.get("id"),
                            "file_name": s.get("file_name", ""),
                            "emulator": s.get("emulator", ""),
                            "updated_at": s.get("updated_at", ""),
                            "file_size_bytes": s.get("file_size_bytes", 0),
                        }
                        for s in saves
                    ],
                    "count": len(saves),
                    "latest_updated_at": latest,
                }
            )

        # State info
        game_state = self._save_sync_state["saves"].get(rom_id_str, {})
        default_slot = self._save_sync_state.get("settings", {}).get("default_slot", "default")
        slot_confirmed = game_state.get("slot_confirmed", False)
        active_slot = game_state.get("active_slot") if slot_confirmed else None

        return {
            "has_local_saves": len(local_files) > 0,
            "local_files": local_file_info,
            "server_slots": server_slots,
            "default_slot": default_slot,
            "slot_confirmed": slot_confirmed,
            "active_slot": active_slot,
        }

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None | object = _NO_MIGRATION,
    ) -> dict:
        """Confirm which slot to use for a game's save sync.

        Sets slot_confirmed=true and active_slot in state.

        If migrate_from_slot is provided (can be None for legacy no-slot saves),
        migrates saves: upload local files to chosen_slot, then delete old server saves.
        Pass _NO_MIGRATION sentinel (the default) to skip migration.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        chosen_slot = str(chosen_slot).strip()
        if not chosen_slot:
            return {"success": False, "needs_conflict_resolution": False, "message": "Slot name cannot be empty"}

        # Update state
        saves = self._save_sync_state.setdefault("saves", {})
        if rom_id_str not in saves:
            saves[rom_id_str] = {"files": {}}
        saves[rom_id_str]["active_slot"] = chosen_slot
        saves[rom_id_str]["slot_confirmed"] = True

        # Migration: re-upload local files to new slot, delete old server saves
        if migrate_from_slot is not _NO_MIGRATION:
            # migrate_from_slot can be None (legacy no-slot) or a string slot name
            from_slot: str | None = migrate_from_slot if isinstance(migrate_from_slot, str) else None
            try:
                await self._migrate_slot_saves(rom_id, rom_id_str, chosen_slot, from_slot)
            except Exception as e:
                self._logger.warning(f"confirm_slot_choice({rom_id}): migration failed: {e}")
                self.save_state()
                return {
                    "success": True,
                    "needs_conflict_resolution": False,
                    "message": f"Slot confirmed but migration failed: {e}",
                }

        self.save_state()
        return {"success": True, "needs_conflict_resolution": False, "message": "Slot confirmed"}

    async def _migrate_slot_saves(
        self,
        rom_id: int,
        rom_id_str: str,
        chosen_slot: str,
        migrate_from_slot: str | None,
    ) -> None:
        """Migrate server saves from one slot to another.

        For each local file: upload with new slot, then delete old server save.
        Safe order: POST first, DELETE after.
        """
        device_id = self._get_server_device_id()

        # Find server saves in the old slot
        all_saves = await self._loop.run_in_executor(
            None,
            lambda: self._retry.with_retry(
                lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
            ),
        )
        old_slot_saves = [s for s in all_saves if s.get("slot") == migrate_from_slot]
        if not old_slot_saves:
            return

        # Get local files for re-upload
        local_files = self._find_save_files(rom_id)
        local_by_name = {lf["filename"]: lf for lf in local_files}

        # Resolve emulator tag
        info = self._get_rom_save_info(rom_id)
        system = info["system"] if info else ""
        installed = self._state["installed_roms"].get(rom_id_str, {})
        rom_filename = os.path.basename(installed.get("file_path", "")) or None
        core_so, _label = self._get_active_core(system, rom_filename)
        emulator = build_emulator_tag(core_so)

        ids_to_delete: list[int] = []

        for old_save in old_slot_saves:
            fname = old_save.get("file_name", "")
            local_file = local_by_name.get(fname)
            if local_file and os.path.isfile(local_file["path"]):
                # Upload to new slot
                await self._loop.run_in_executor(
                    None,
                    lambda lf=local_file, em=emulator: self._retry.with_retry(
                        lambda: self._romm_api.upload_save(
                            rom_id,
                            lf["path"],
                            em,
                            device_id=device_id,
                            slot=chosen_slot,
                        ),
                    ),
                )
            old_id = old_save.get("id")
            if old_id is not None:
                ids_to_delete.append(old_id)

        # Delete old saves
        if ids_to_delete:
            await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.delete_server_saves(ids_to_delete),
                ),
            )

    async def sync_all_saves(self) -> dict:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        if not self._is_save_sync_enabled():
            return {"success": False, "message": "Save sync is disabled", "synced": 0, "conflicts": 0}

        if not self._save_sync_state.get("device_id"):
            reg = self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": _DEVICE_NOT_REGISTERED}

        total_synced = 0
        total_errors: list[str] = []
        all_conflicts: list[SaveConflict | dict] = []
        rom_count = 0

        # Only iterate installed ROMs — non-installed ROMs have no save files
        rom_ids = set(self._state["installed_roms"].keys())
        self._log_debug(f"sync_all_saves: {len(rom_ids)} ROMs to check")

        for rom_id_str in sorted(rom_ids):
            rom_count += 1
            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, int(rom_id_str))
            total_synced += synced
            total_errors.extend(errors)
            all_conflicts.extend(conflicts)

        self.save_state()

        conflicts_count = len(all_conflicts)
        msg = f"Synced {total_synced} save(s) across {rom_count} ROM(s)"
        if total_errors:
            msg += f", {len(total_errors)} error(s)"
        if conflicts_count:
            msg += f", {conflicts_count} conflict(s)"
        return {
            "success": len(total_errors) == 0,
            "message": msg,
            "synced": total_synced,
            "conflicts": conflicts_count,
            "conflicts_list": [c if isinstance(c, dict) else asdict(c) for c in all_conflicts],
            "roms_checked": rom_count,
            "errors": total_errors,
        }

    async def resolve_conflict(
        self,
        rom_id: int,
        filename: str,
        resolution: str,
        server_save_id: int | None = None,
        local_path: str | None = None,
    ) -> dict:
        """Resolve a pending save conflict. resolution: ``"upload"`` or ``"download"``."""
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        if resolution not in ("upload", "download"):
            return {"success": False, "message": f"Invalid resolution: {resolution}"}

        # Build conflict from params passed by frontend
        if not server_save_id:
            return {"success": False, "message": "Missing server_save_id"}
        conflict = {
            "rom_id": rom_id,
            "filename": filename,
            "server_save_id": server_save_id,
            "local_path": local_path,
        }

        info = self._get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "message": "ROM not installed"}
        system = info["system"]
        saves_dir = info["saves_dir"]

        try:
            result = await self._loop.run_in_executor(
                None,
                self._resolve_conflict_io,
                rom_id,
                rom_id_str,
                resolution,
                conflict,
                saves_dir,
                filename,
                system,
            )
            if result is not None:
                return result

            self.save_state()
            return {"success": True, "message": f"Conflict resolved: {resolution}"}
        except Exception as e:
            self._logger.error(f"Conflict resolution failed: {e}")
            return {"success": False, "message": "Conflict resolution failed"}

    async def resolve_newer_in_slot(self, rom_id: int, filename: str, resolution: str, newer_save_id: int) -> dict:
        """Resolve a newer-in-slot conflict.

        resolution: ``"use_newer"`` | ``"keep_current"`` | ``"dismiss"``
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        if resolution == "use_newer":
            info = self._get_rom_save_info(rom_id)
            if not info:
                return {"success": False, "message": "ROM save info not found"}
            device_id = self._get_server_device_id()
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id)),
            )
            newer_save = next((s for s in server_saves if s.get("id") == newer_save_id), None)
            if not newer_save:
                return {"success": False, "message": "Newer save not found on server"}
            await self._loop.run_in_executor(
                None,
                self._do_download_save,
                newer_save,
                info["saves_dir"],
                filename,
                rom_id_str,
                info["system"],
            )
            # Re-fetch live reference — _do_download_save replaced the dict
            live_state = self._save_sync_state["saves"].get(rom_id_str, {}).get("files", {}).get(filename, {})
            live_state.pop("dismissed_newer_save_id", None)
            self.save_state()
            return {"success": True, "message": "Downloaded newer save"}

        if resolution == "dismiss":
            files = self._save_sync_state.get("saves", {}).get(rom_id_str, {}).setdefault("files", {})
            live_state = files.setdefault(filename, {})
            live_state["dismissed_newer_save_id"] = newer_save_id
            self.save_state()
            return {"success": True, "message": "Dismissed"}

        # keep_current
        return {"success": True, "message": "Keeping current save"}

    def get_save_sync_settings(self) -> dict:
        """Return current save sync settings."""
        settings = self._save_sync_state.get("settings", {})
        # Defensive defaults for keys added after initial release
        settings.setdefault("default_slot", "default")
        settings.setdefault("autocleanup_limit", 10)
        if not self._save_sync_state.get("settings"):
            settings.setdefault("save_sync_enabled", False)
            settings.setdefault("conflict_mode", "ask_me")
            settings.setdefault("sync_before_launch", True)
            settings.setdefault("sync_after_exit", True)
            settings.setdefault("clock_skew_tolerance_sec", 60)
        return settings

    @staticmethod
    def _sanitize_setting(key: str, value: object, valid_modes: set[str]) -> tuple[object, bool]:
        """Validate and coerce a single settings key/value pair.

        Returns (coerced_value, skip) where skip=True means the value should
        be discarded (e.g. invalid conflict_mode or empty slot name).
        """
        if key == "conflict_mode":
            return value, value not in valid_modes
        if key == "clock_skew_tolerance_sec":
            return max(0, int(value)), False  # type: ignore[arg-type]
        if key == "default_slot":
            if value is None:
                return None, False  # None = legacy mode
            coerced = str(value).strip()
            return (coerced if coerced else None), False  # empty -> None
        if key == "autocleanup_limit":
            return max(1, int(value)), False  # type: ignore[arg-type]
        if key in ("save_sync_enabled", "sync_before_launch", "sync_after_exit"):
            return bool(value), False
        return value, False

    def update_save_sync_settings(self, settings: dict) -> dict:
        """Update save sync settings (conflict_mode, sync toggles, etc.)."""
        allowed_keys = {
            "save_sync_enabled",
            "conflict_mode",
            "sync_before_launch",
            "sync_after_exit",
            "clock_skew_tolerance_sec",
            "default_slot",
            "autocleanup_limit",
        }
        valid_modes = {"newest_wins", "always_upload", "always_download", "ask_me"}

        current = self._save_sync_state.setdefault("settings", {})

        for key, value in settings.items():
            if key not in allowed_keys:
                continue
            value, skip = self._sanitize_setting(key, value, valid_modes)
            if skip:
                continue
            current[key] = value

        self.save_state()
        return {"success": True, "settings": current}

    def delete_local_saves(self, rom_id: int) -> dict:
        """Delete local save files (.srm, .rtc) for a ROM."""
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        files = self._find_save_files(rom_id)
        if not files:
            return {"success": True, "deleted_count": 0, "message": "No local save files found"}

        deleted = 0
        errors = []
        for f in files:
            try:
                os.remove(f["path"])
                deleted += 1
            except Exception as e:
                errors.append(f"{f['filename']}: {e}")

        # Clean up sync state for this ROM
        self._save_sync_state.get("saves", {}).pop(rom_id_str, None)
        self.save_state()

        if errors:
            return {
                "success": False,
                "deleted_count": deleted,
                "message": f"Deleted {deleted} file(s), {len(errors)} error(s)",
            }
        return {
            "success": True,
            "deleted_count": deleted,
            "message": f"Deleted {deleted} save file(s)",
        }

    def delete_platform_saves(self, platform_slug: str) -> dict:
        """Delete local save files for all installed ROMs on a platform."""
        total_deleted = 0
        total_errors: list[str] = []
        rom_count = 0

        for rom_id_str, entry in self._state["installed_roms"].items():
            if entry.get("platform_slug") != platform_slug:
                continue
            rom_count += 1
            rom_id = int(rom_id_str)
            files = self._find_save_files(rom_id)
            for f in files:
                try:
                    os.remove(f["path"])
                    total_deleted += 1
                except Exception as e:
                    total_errors.append(f"{f['filename']}: {e}")
            # Clean up sync state
            self._save_sync_state.get("saves", {}).pop(rom_id_str, None)

        self.save_state()

        if total_errors:
            return {
                "success": False,
                "deleted_count": total_deleted,
                "message": (f"Deleted {total_deleted} file(s) from {rom_count} ROM(s), {len(total_errors)} error(s)"),
            }
        return {
            "success": True,
            "deleted_count": total_deleted,
            "message": f"Deleted {total_deleted} save file(s) from {rom_count} ROM(s)",
        }
