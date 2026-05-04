"""SaveService — save sync business logic.

All RomM communication goes through ``RommApiProtocol``.
No ``import decky`` — error utilities come from ``lib.errors``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import socket
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from models.saves import SaveConflict

from domain.emulator_tag import build_emulator_tag, detect_core_change
from domain.save_extensions import get_save_extensions
from domain.save_path import resolve_save_dir
from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    Upload,
    compute_sync_action,
)
from lib.errors import RommApiError, classify_error
from lib.iso_time import parse_iso_to_epoch
from services.protocols import (
    CoreNameProviderFn,
    CoreResolverFn,
    RetryStrategy,
    RommApiProtocol,
    RomsPathProvider,
    SavesPathProvider,
)

_DEVICE_NOT_REGISTERED = "Device not registered"
_NO_MIGRATION = object()  # sentinel: no slot migration requested

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from services.protocols import EventEmitter


_SYNC_DISABLED_MSG = "Save sync is disabled"


def _compute_uploaded_by_us(
    server_save: dict | None,
    own_upload_ids: list[int] | None,
) -> bool | None:
    """Three-way uploader attribution flag.

    Returns True/False when own_upload_ids is known for this ROM (we can tell
    whether this installation POSTed the save), or None for legacy ROM state
    without the ``own_upload_ids`` field (attribution unknown).
    """
    if server_save is None or own_upload_ids is None:
        return None
    sid = server_save.get("id")
    if sid is None:
        return None
    return sid in own_upload_ids


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
        This is an ES-DE question (``which core runs this ROM?``).
    get_core_name:
        Callable returning the RetroArch canonical ``corename`` field from
        a core's ``.info`` file for a given ``core_so`` (e.g. ``"mgba_libretro"``
        → ``"mGBA"``). Optional. When ``sort_savefiles_enable`` is active on
        RetroArch, this is the authoritative name used for the per-core save
        subdirectory — it is NOT the same as the ES-DE UI label returned by
        ``get_active_core`` (see the Config-Source-Parsers wiki page for the
        one-parser-per-source rationale). When ``None`` or when resolution
        fails at runtime, SaveService warns and falls back to the parent
        directory path; see ``_resolve_retroarch_corename``.
    detect_sort_change:
        Optional synchronous callback that refreshes save-sort state from
        the live RetroArch config (wired to
        ``MigrationService.detect_save_sort_change`` in ``bootstrap``).
        Save-sync MUST see fresh save-sort state before computing
        ``saves_dir`` — otherwise a direct-Steam-launch with no pre-launch
        detect trigger would silently download stale server content to the
        wrong layout and destroy real user progress during the subsequent
        migration (#238). ``pre_launch_sync`` and ``post_exit_sync`` invoke
        this callback once at their entry point. ``None`` disables the
        call (used only in unit tests where state is seeded explicitly);
        failures are logged and swallowed so save-sync degrades
        gracefully to the previously-known state.
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
        get_core_name: CoreNameProviderFn | None = None,
        plugin_version: str = "0.0.0",
        emit: EventEmitter | None = None,
        detect_sort_change: Callable[[], None] | None = None,
        is_retrodeck_migration_pending: Callable[[], bool] | None = None,
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
        self._get_core_name = get_core_name
        self._plugin_version = plugin_version
        self._emit = emit
        self._detect_sort_change = detect_sort_change
        self._is_retrodeck_migration_pending = is_retrodeck_migration_pending
        # Per-rom lock dict — serializes concurrent sync operations on the
        # same rom_id (pre_launch_sync, post_exit_sync, manual sync, resolve).
        self._rom_sync_locks: dict[int, asyncio.Lock] = {}

    def _rom_lock(self, rom_id: int) -> asyncio.Lock:
        """Return the lock for this rom_id, creating it lazily."""
        if rom_id not in self._rom_sync_locks:
            self._rom_sync_locks[rom_id] = asyncio.Lock()
        return self._rom_sync_locks[rom_id]

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
        """Populate ``_save_sync_state`` with defaults (idempotent).

        Defaults only — schema migrations on loaded data live in
        ``load_state``. Running them here would be a no-op because
        ``init_state`` is called before any disk data is loaded.
        """
        defaults = self.make_default_state()
        for key, value in defaults.items():
            self._save_sync_state.setdefault(key, value)
        self._save_sync_state.setdefault("settings", {})
        for key, value in defaults["settings"].items():
            self._save_sync_state["settings"].setdefault(key, value)

    def _migrate_loaded_state(self) -> None:
        """Apply schema migrations to data just read from disk.

        Migrations are idempotent. Called from ``load_state`` after the
        disk content has been merged into ``_save_sync_state``; the next
        ``save_state`` then persists the cleaned form.

        Currently:
        - Rename per-game ``active_core`` → ``last_synced_core``.
        - Drop legacy per-file ``dismissed_newer_save_id`` (was used by
          the removed newer-in-slot detection).
        """
        saves = self._save_sync_state.get("saves", {})
        if not isinstance(saves, dict):
            return
        for entry in saves.values():
            if not isinstance(entry, dict):
                continue
            if "active_core" in entry:
                entry["last_synced_core"] = entry.pop("active_core")
            files = entry.get("files", {})
            if isinstance(files, dict):
                for file_state in files.values():
                    if isinstance(file_state, dict):
                        file_state.pop("dismissed_newer_save_id", None)

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
            return
        self._migrate_loaded_state()

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

    def _resolve_retroarch_corename(self, system: str, rom_filename: str) -> tuple[str | None, str | None]:
        """Resolve the RetroArch ``corename`` for a system/ROM.

        Asks ES-DE (via ``get_active_core``) **which** core is active for
        this ROM, then asks the RetroArch ``.info`` parser (via
        ``get_core_name``) **what** RetroArch calls that core in its own
        subsystem — which is the authoritative name used for per-core save
        subdirectories when ``sort_savefiles_enable`` is active.

        One parser per source: the ES-DE label (second element of the
        ``get_active_core`` tuple) is NOT a valid substitute for the
        RetroArch corename. See the Config-Source-Parsers wiki page and
        the reference implementation in ``MigrationService``.

        Returns ``(corename, core_so)``. Either element may be ``None``
        when resolution fails at that step: ``core_so`` is ``None`` when
        ES-DE cannot determine the active core, ``corename`` is ``None``
        when ``.info`` parsing returns nothing (or when ``get_core_name``
        is not injected). Returning the tuple — rather than just
        ``corename`` — lets callers include ``core_so`` in diagnostic
        logs so users can identify which ``.info`` file is at fault.
        Callers choose their own fallback strategy (e.g. warn and fall
        back for critical-path SaveService flows; skip and warn for
        one-shot migrations).
        """
        if self._get_core_name is None:
            return (None, None)
        core_so, _label = self._get_active_core(system, rom_filename)
        if not core_so:
            return (None, None)
        corename = self._get_core_name(core_so)
        return (corename or None, core_so)

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
        # When a save-sort migration is pending, prefer the *previous* layout:
        # RetroArch caches its runtime save-path at game-load time, so the
        # session that just ended still wrote to the old directory. Reading
        # the current settings here would point sync at the wrong location
        # and risk downloading stale server content to the new layout (#238).
        saves_base = self._get_saves_path()
        roms_base = self._get_roms_path()
        sort_state = self._pending_sort_settings() or self._state.get("save_sort_settings")
        if sort_state:
            sort_by_content = sort_state.get("sort_by_content", True)
            sort_by_core = sort_state.get("sort_by_core", False)
        else:
            sort_by_content, sort_by_core = True, False  # RetroDECK defaults

        # When sort-by-core is active, RetroArch writes per-core subdirs named
        # by the .info ``corename`` field. Resolve it via the dedicated parser.
        # See docs: Config-Source-Parsers wiki page ("one parser per source").
        # Decision: warn-and-fallback (not fail-loud like MigrationService).
        # SaveService is the critical-path sync flow — every game launch
        # depends on it. Fail-loud would take down save sync entirely on any
        # .info hiccup. MigrationService can afford strictness (one-shot),
        # SaveService cannot (continuous). See issue #232 for history.
        core_name: str | None = None
        if sort_by_core:
            rom_filename = os.path.basename(file_path)
            core_name, core_so = self._resolve_retroarch_corename(system, rom_filename)
            if core_name is None:
                self._logger.warning(
                    "SaveService: unable to resolve RetroArch corename for "
                    "%s/%s (core_so=%s) while sort_by_core is enabled. "
                    "Falling back to the parent save directory, which will "
                    "not match what RetroArch reads at runtime. Check that "
                    "the core's .info file is readable under the RetroDECK "
                    "Flatpak cores directory.",
                    system,
                    rom_filename,
                    core_so if core_so else "unresolved",
                )

        saves_dir = resolve_save_dir(
            file_path,
            saves_base,
            system,
            roms_base=roms_base,
            sort_by_content=sort_by_content,
            sort_by_core=sort_by_core,
            core_name=core_name,
        )

        return {
            "system": system,
            "rom_name": rom_name,
            "saves_dir": saves_dir,
            "platform_slug": platform_slug,
            "file_path": file_path,
        }

    def _pending_sort_settings(self) -> dict | None:
        """Return previous save-sort settings if a migration is pending, else None.

        Rejects empty dicts to avoid the half-state where ``_get_rom_save_info``'s
        ``or`` fallback would treat ``{}`` as "no pending migration" (and read
        current settings) while ``_is_save_sort_changed`` would treat the same
        ``{}`` as "pending" (and gate sync). Both call sites must agree on
        what counts as pending — see #238 review finding 3.
        """
        prev = self._state.get("save_sort_settings_previous")
        return prev if prev else None

    def _is_save_sort_changed(self) -> bool:
        """Check if a save sort migration is pending (detected by MigrationService)."""
        return self._pending_sort_settings() is not None

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
                "own_upload_ids": [],
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

        device_id = self._get_server_device_id()
        self._retry.with_retry(
            lambda: self._romm_api.download_save_content(
                server_save["id"],
                tmp_path,
                device_id=device_id,
                optimistic=True,
            ),
        )

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

        is_post = save_id is None
        result = self._retry.with_retry(
            lambda: self._romm_api.upload_save(
                int(rom_id), file_path, emulator, save_id, device_id=device_id, slot=slot
            )
        )

        self._update_file_sync_state(
            rom_id_str, filename, result, file_path, system, emulator_tag=emulator, core_so=core_so
        )

        if is_post:
            self._record_own_upload(rom_id_str, result.get("id"))

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
        if device_id and upload_id:
            try:
                self._romm_api.confirm_download(upload_id, device_id)
            except Exception:
                self._log_debug(f"confirm_download after upload failed for save {upload_id} (non-fatal)")

        self._log_debug(f"Uploaded save: {filename} for rom {rom_id_str} (emulator={emulator})")
        return result

    def _record_own_upload(self, rom_id_str: str, new_id: int | None) -> None:
        """Track a save_id we POSTed ourselves for uploader attribution.

        POST = brand-new save; PUT updates an existing tracked save without
        changing ownership. Assumes POST is not upsert-by-filename on the
        server — if RomM ever changes that, revisit this tracker.
        """
        if new_id is None:
            return
        rom_state = self._save_sync_state["saves"].setdefault(rom_id_str, {"own_upload_ids": []})
        own_ids: list[int] = rom_state.get("own_upload_ids", [])
        if new_id in own_ids:
            return
        own_ids.append(new_id)
        rom_state["own_upload_ids"] = own_ids
        self.save_state()

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

    @staticmethod
    def _filter_server_saves_to_slot(server_saves: list[dict], active_slot: str | None) -> list[dict]:
        """Filter server saves to the active slot.

        Saves with ``slot=None`` (legacy/no-slot) are accepted under any active
        slot; in legacy mode (no active slot) we only keep saves without a slot.
        """
        if active_slot:
            return [ss for ss in server_saves if ss.get("slot") == active_slot or ss.get("slot") is None]
        return [ss for ss in server_saves if not ss.get("slot")]

    @staticmethod
    def _build_local_input(local_path: str, filename: str) -> dict:
        """Build the dict shape consumed by ``compute_sync_action``."""
        return {
            "filename": filename,
            "path": local_path,
            "size": os.path.getsize(local_path) if os.path.isfile(local_path) else None,
            "mtime": os.path.getmtime(local_path) if os.path.isfile(local_path) else None,
        }

    @staticmethod
    def _local_save_target(server_save: dict, rom_name: str) -> str:
        """The canonical local filename for a server save: ``<rom_name>.<ext>``.

        ``rom_name`` is the deterministic identity from RetroArch's
        perspective — it's the ROM file's basename without extension, the
        same string RetroArch uses to look up SRAM. Callers must have
        already resolved the ROM via ``_get_rom_save_info`` (which only
        returns when the ROM is actually installed); there is no fallback
        to server-derived names because those can mismatch RetroArch's
        actual lookup path and silently break the sync.
        """
        ext = server_save.get("file_extension", "srm")
        return f"{rom_name}.{ext}"

    def _build_sync_conflict_entry(
        self,
        rom_id: int,
        filename: str,
        server: dict,
        local_path: str | None,
        local_hash: str | None,
    ) -> dict:
        """Build a Phase-2 ``sync_conflict`` descriptor for the frontend."""
        local_mtime = None
        local_size = None
        if local_path and os.path.isfile(local_path):
            local_mtime = datetime.fromtimestamp(os.path.getmtime(local_path), tz=UTC).isoformat()
            local_size = os.path.getsize(local_path)
        return {
            "type": "sync_conflict",
            "rom_id": rom_id,
            "filename": filename,
            "server_save_id": server.get("id"),
            "server_updated_at": server.get("updated_at", ""),
            "server_size": server.get("file_size_bytes"),
            "local_path": local_path,
            "local_hash": local_hash,
            "local_mtime": local_mtime,
            "local_size": local_size,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def _dispatch_skip(
        self,
        action: Skip,
        *,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local_hash: str | None,
    ) -> None:
        if action.adopt_baseline and local_hash is not None:
            # State-only mutation: write the current local_hash as the baseline
            # so future runs can detect drift. No I/O, no synced count.
            self._log_debug(f"_sync_rom_saves({rom_id}): skip + adopt_baseline {filename} ({action.reason})")
            self._adopt_baseline_hash(rom_id_str, filename, local_hash)
        else:
            self._log_debug(f"_sync_rom_saves({rom_id}): skip {filename} ({action.reason})")

    def _dispatch_upload(
        self,
        action: Upload,
        *,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local_path: str | None,
        system: str,
        server_saves: list[dict],
        errors: list[str],
    ) -> bool:
        """Execute an ``Upload`` action. Returns True iff the upload was issued."""
        if local_path is None:
            errors.append(f"{filename}: upload requested but no local file")
            return False
        if action.target_save_id is None:
            # POST path: brand-new save in slot.
            self._do_upload_save(rom_id, local_path, filename, rom_id_str, system, None)
            return True
        # PUT path: re-upload to update the tracked save (local diverged while
        # is_current=true).
        server_save = next((s for s in server_saves if s.get("id") == action.target_save_id), None)
        if server_save is None:
            # Picked save vanished between read and dispatch — best-effort.
            self._log_debug(
                f"_dispatch_sync_action: target_save_id={action.target_save_id} not in server_saves; skipping",
            )
            return False
        self._do_upload_save(rom_id, local_path, filename, rom_id_str, system, server_save)
        return True

    def _dispatch_sync_action(
        self,
        action: object,
        *,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local_path: str | None,
        local_hash: str | None,
        saves_dir: str,
        system: str,
        server_saves: list[dict],
        errors: list[str],
        conflicts: list[SaveConflict | dict],
    ) -> bool:
        """Execute one ``SyncAction`` outcome. Returns True if a transfer happened.

        Centralises the I/O dispatch so ``_sync_rom_saves`` stays declarative.
        Errors are caught and pushed onto ``errors`` so a single failure can't
        abort the whole rom-level sync.
        """
        try:
            if isinstance(action, Skip):
                self._dispatch_skip(
                    action,
                    rom_id=rom_id,
                    rom_id_str=rom_id_str,
                    filename=filename,
                    local_hash=local_hash,
                )
                return False
            if isinstance(action, Upload):
                return self._dispatch_upload(
                    action,
                    rom_id=rom_id,
                    rom_id_str=rom_id_str,
                    filename=filename,
                    local_path=local_path,
                    system=system,
                    server_saves=server_saves,
                    errors=errors,
                )
            if isinstance(action, Download):
                self._do_download_save(action.server_save, saves_dir, filename, rom_id_str, system)
                return True
            if isinstance(action, Conflict):
                conflicts.append(
                    self._build_sync_conflict_entry(rom_id, filename, action.server_save, local_path, local_hash)
                )
                return False
        except RommApiError as e:
            _code, _msg = classify_error(e)
            errors.append(f"{filename}: {_msg}")
        except Exception as e:
            self._handle_unexpected_error(e, filename, saves_dir, errors)
        return False

    def _adopt_baseline_hash(self, rom_id_str: str, filename: str, local_hash: str) -> None:
        """Persist ``local_hash`` as the file's ``last_sync_hash`` baseline.

        Used by Skip(adopt_baseline=True) — the algorithm has detected that
        we've observed an is_current=true situation with local content but no
        baseline yet. Recording the baseline lets subsequent runs detect
        offline-edit drift. State mutation only, no I/O.
        """
        saves = self._save_sync_state.setdefault("saves", {})
        rom_entry = saves.setdefault(rom_id_str, {"files": {}})
        files = rom_entry.setdefault("files", {})
        file_state = files.setdefault(filename, {})
        file_state["last_sync_hash"] = local_hash

    def _sync_local_files(
        self,
        local_files: list[dict],
        *,
        rom_id: int,
        rom_id_str: str,
        device_id: str,
        server_in_slot: list[dict],
        files_state: dict,
        system: str,
        saves_dir: str,
        errors: list[str],
        conflicts: list[SaveConflict | dict],
    ) -> tuple[int, set[str]]:
        """Run ``compute_sync_action`` on every local file and dispatch each outcome.

        Returns ``(synced_count, handled_filenames)``. ``handled_filenames`` is
        used by the server-only sweep to skip targets already addressed.
        """
        synced = 0
        handled_filenames: set[str] = set()
        for lf in local_files:
            filename = lf["filename"]
            local_path = lf["path"]
            local_hash = self._file_md5(local_path) if os.path.isfile(local_path) else None
            file_state = files_state.get(filename, {})
            action = compute_sync_action(
                local_file=self._build_local_input(local_path, filename),
                server_saves_in_slot=server_in_slot,
                files_state=file_state,
                device_id=device_id,
                local_hash=local_hash,
            )
            handled_filenames.add(filename)
            self._log_debug(f"_sync_rom_saves({rom_id}): local {filename} -> {type(action).__name__}")
            if self._dispatch_sync_action(
                action,
                rom_id=rom_id,
                rom_id_str=rom_id_str,
                filename=filename,
                local_path=local_path,
                local_hash=local_hash,
                saves_dir=saves_dir,
                system=system,
                server_saves=server_in_slot,
                errors=errors,
                conflicts=conflicts,
            ):
                synced += 1
        return synced, handled_filenames

    def _sync_server_only_saves(
        self,
        server_in_slot: list[dict],
        *,
        rom_id: int,
        rom_id_str: str,
        rom_name: str,
        device_id: str,
        files_state: dict,
        handled_filenames: set[str],
        pending_migration: bool,
        system: str,
        saves_dir: str,
        errors: list[str],
        conflicts: list[SaveConflict | dict],
    ) -> int:
        """Address server saves whose canonical local target wasn't already
        handled by the local-file sweep. Returns the number of transfers.

        Skipped entirely while a save-sort migration is pending (see #238).
        """
        # Group server saves by canonical local target filename. compute_sync_action
        # picks newest-in-group automatically.
        server_only_groups: dict[str, list[dict]] = {}
        for ss in server_in_slot:
            target = self._local_save_target(ss, rom_name)
            if target in handled_filenames:
                continue
            server_only_groups.setdefault(target, []).append(ss)

        synced = 0
        for target_filename, group in server_only_groups.items():
            if pending_migration:
                self._log_debug(
                    f"_sync_rom_saves({rom_id}): skipping server_only {target_filename} — migration pending"
                )
                continue
            file_state = files_state.get(target_filename, {})
            action = compute_sync_action(
                local_file=None,
                server_saves_in_slot=group,
                files_state=file_state,
                device_id=device_id,
                local_hash=None,
            )
            self._log_debug(f"_sync_rom_saves({rom_id}): server-only {target_filename} -> {type(action).__name__}")
            if self._dispatch_sync_action(
                action,
                rom_id=rom_id,
                rom_id_str=rom_id_str,
                filename=target_filename,
                local_path=None,
                local_hash=None,
                saves_dir=saves_dir,
                system=system,
                server_saves=group,
                errors=errors,
                conflicts=conflicts,
            ):
                synced += 1
        return synced

    def _sync_rom_saves(self, rom_id: int) -> tuple[int, list[str], list[SaveConflict | dict]]:
        """Sync saves for a single ROM.

        Runs ``compute_sync_action`` for every local file and every
        server-only save in the active slot via two focused helpers,
        dispatching each outcome through ``_dispatch_sync_action``. Returns
        ``(synced_count, errors_list, conflicts_list)``.
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
        active_slot = save_state.get("active_slot")
        server_in_slot = self._filter_server_saves_to_slot(server_saves, active_slot)

        errors: list[str] = []
        conflicts: list[SaveConflict | dict] = []
        device_id_str = device_id or ""

        synced_local, handled_filenames = self._sync_local_files(
            local_files,
            rom_id=rom_id,
            rom_id_str=rom_id_str,
            device_id=device_id_str,
            server_in_slot=server_in_slot,
            files_state=files_state,
            system=system,
            saves_dir=saves_dir,
            errors=errors,
            conflicts=conflicts,
        )
        synced_server = self._sync_server_only_saves(
            server_in_slot,
            rom_id=rom_id,
            rom_id_str=rom_id_str,
            rom_name=rom_name,
            device_id=device_id_str,
            files_state=files_state,
            handled_filenames=handled_filenames,
            pending_migration=self._is_save_sort_changed(),
            system=system,
            saves_dir=saves_dir,
            errors=errors,
            conflicts=conflicts,
        )
        synced = synced_local + synced_server

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
        uploaded_by_us: bool | None = None,
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
            "server_file_name": server.get("file_name") if server else None,
            "server_emulator": server.get("emulator") if server else None,
            "server_updated_at": server.get("updated_at", "") if server else None,
            "server_size": server.get("file_size_bytes") if server else None,
            "last_sync_at": last_sync_at,
            "status": status,
            "device_syncs": device_syncs,
            "is_current": is_current,
            "uploaded_by_us": uploaded_by_us,
        }

    @staticmethod
    def _status_from_action(action: object) -> str:
        """Map a ``SyncAction`` outcome to the legacy file-status string."""
        if isinstance(action, Skip):
            return "synced"
        if isinstance(action, Upload):
            return "upload"
        if isinstance(action, Download):
            return "download"
        if isinstance(action, Conflict):
            return "conflict"
        return "synced"

    @staticmethod
    def _resolve_chosen_server(action: object, candidates: list[dict]) -> dict | None:
        """Pick the server-save dict to display alongside the file-status row.

        - ``Download`` and ``Conflict`` carry the chosen save explicitly on the
          action — use that.
        - ``Skip`` falls back to the newest in *candidates* so the status panel
          still shows a server reference where one exists (e.g. synced rows
          continue to display the server save's metadata).
        - ``Upload(target_save_id=None)`` (POST-as-new) has no server reference
          yet → ``None``.
        - ``Upload(target_save_id=int)`` (PUT) targets an existing save in
          *candidates* — fall back to the newest so the status panel still
          shows the server-side metadata while the upload is pending.
        """
        if isinstance(action, Download | Conflict):
            return action.server_save
        if isinstance(action, Upload) and action.target_save_id is None:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)

    def _status_entry_for_local_file(
        self,
        local_file: dict,
        *,
        rom_id: int,
        rom_id_str: str,
        server_in_slot: list[dict],
        files_state: dict,
        server_device_id: str | None,
        own_upload_ids: list[int] | None,
    ) -> tuple[dict, dict | None]:
        """Build the file-status entry for an existing local file.

        Returns ``(status_entry, conflict_entry_or_None)``. The conflict
        entry is the ``sync_conflict`` descriptor when ``compute_sync_action``
        returns ``Conflict``; otherwise None.
        """
        filename = local_file["filename"]
        local_path = local_file["path"]
        local_hash = self._file_md5(local_path) if os.path.isfile(local_path) else None
        file_state = files_state.get(filename, {})
        action = compute_sync_action(
            local_file=self._build_local_input(local_path, filename),
            server_saves_in_slot=server_in_slot,
            files_state=file_state,
            device_id=server_device_id or "",
            local_hash=local_hash,
        )
        if isinstance(action, Skip) and action.adopt_baseline and local_hash is not None:
            self._adopt_baseline_hash(rom_id_str, filename, local_hash)
        chosen_server = self._resolve_chosen_server(action, server_in_slot)
        local_mtime = (
            datetime.fromtimestamp(os.path.getmtime(local_path), tz=UTC).isoformat()
            if os.path.isfile(local_path)
            else None
        )
        local_size = os.path.getsize(local_path) if os.path.isfile(local_path) else None
        status_entry = self._build_file_status(
            filename,
            local_path=local_path,
            local_hash=local_hash,
            local_mtime=local_mtime,
            local_size=local_size,
            server=chosen_server,
            last_sync_at=file_state.get("last_sync_at"),
            status=self._status_from_action(action),
            server_device_id=server_device_id,
            uploaded_by_us=_compute_uploaded_by_us(chosen_server, own_upload_ids),
        )
        conflict_entry: dict | None = None
        if isinstance(action, Conflict):
            self._log_debug(
                f"_get_save_status_io({rom_id}): conflict {filename} server_save_id={action.server_save.get('id')}"
            )
            conflict_entry = self._build_sync_conflict_entry(
                rom_id, filename, action.server_save, local_path, local_hash
            )
        return status_entry, conflict_entry

    def _status_entry_for_server_only(
        self,
        server_in_slot: list[dict],
        *,
        rom_name: str,
        server_device_id: str | None,
        own_upload_ids: list[int] | None,
    ) -> dict:
        """Build the ready-to-download status entry when no local file exists
        but the slot has server saves. Picks newest by ``updated_at``."""
        newest = max(server_in_slot, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)
        return self._build_file_status(
            self._local_save_target(newest, rom_name),
            local_path=None,
            local_hash=None,
            local_mtime=None,
            local_size=None,
            server=newest,
            last_sync_at=None,
            status="download",
            server_device_id=server_device_id,
            uploaded_by_us=_compute_uploaded_by_us(newest, own_upload_ids),
        )

    def _get_save_status_io(self, rom_id: int, server_saves: list[dict]) -> dict:
        """Sync helper for get_save_status — runs in executor.

        Builds the saves-tab status for one ROM as a single-entry view of
        the active slot:

        - Local file present: run ``compute_sync_action`` and surface the
          resulting status, server attribution, and any conflict.
        - No local file but the slot has server saves: surface the newest
          server save as "ready to download". The canonical local target
          is ``<rom_name>.<server.file_extension>`` — derived purely from
          RetroArch's view of the ROM.
        - ROM not installed (no rom_name available) → no entry. There is
          no server-derived filename fallback: without a deterministic
          local path we cannot tell the user where a download would land.
        - Empty slot → no entry.

        Older versions of the same slot are reachable via the lazy-fetched
        ``Previous Versions`` dropdown (``list_file_versions``).

        The one allowed mutation is recording an adopted baseline hash when
        the action requests it (``Skip(adopt_baseline=True)``) — pure state
        hygiene, no network traffic.
        """
        rom_id_str = str(rom_id)
        info = self._get_rom_save_info(rom_id)
        server_device_id = self._get_server_device_id()

        save_state = self._save_sync_state["saves"].get(rom_id_str, {})
        files_state = save_state.get("files", {})
        active_slot = save_state.get("active_slot")
        server_in_slot = self._filter_server_saves_to_slot(server_saves, active_slot)

        # own_upload_ids: None means missing key (legacy entry — unknown attribution).
        raw_own_ids = save_state.get("own_upload_ids")
        own_upload_ids: list[int] | None = raw_own_ids if isinstance(raw_own_ids, list) else None

        file_statuses: list[dict] = []
        conflicts: list[SaveConflict | dict] = []

        if info is not None:
            rom_name = info["rom_name"]
            local_files = self._find_save_files(rom_id)
            local_file = local_files[0] if local_files else None

            if local_file is not None:
                status_entry, conflict_entry = self._status_entry_for_local_file(
                    local_file,
                    rom_id=rom_id,
                    rom_id_str=rom_id_str,
                    server_in_slot=server_in_slot,
                    files_state=files_state,
                    server_device_id=server_device_id,
                    own_upload_ids=own_upload_ids,
                )
                file_statuses.append(status_entry)
                if conflict_entry is not None:
                    conflicts.append(conflict_entry)
            elif server_in_slot:
                file_statuses.append(
                    self._status_entry_for_server_only(
                        server_in_slot,
                        rom_name=rom_name,
                        server_device_id=server_device_id,
                        own_upload_ids=own_upload_ids,
                    )
                )

        playtime = self._save_sync_state.get("playtime", {}).get(rom_id_str, {})
        save_entry = self._save_sync_state.get("saves", {}).get(rom_id_str, {})

        return {
            "rom_id": rom_id,
            "files": file_statuses,
            "playtime": playtime,
            "device_id": self._save_sync_state.get("device_id", ""),
            "last_sync_check_at": save_entry.get("last_sync_check_at"),
            "conflicts": conflicts,
            "save_sort_changed": self._is_save_sort_changed(),
        }

    # ------------------------------------------------------------------
    # Public async API (callable endpoints)
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        if not self._is_save_sync_enabled():
            return {"success": False, "device_id": "", "device_name": "", "disabled": True}

        # Already registered
        has_device_id = self._save_sync_state.get("device_id")
        has_server_id = self._save_sync_state.get("server_device_id")
        if has_device_id and has_server_id:
            with contextlib.suppress(Exception):
                await self._loop.run_in_executor(
                    None,
                    lambda: self._romm_api.update_device(has_server_id, client_version=self._plugin_version),
                )
            return {
                "success": True,
                "device_id": self._save_sync_state["device_id"],
                "device_name": self._save_sync_state.get("device_name", ""),
                "server_device_id": has_server_id,
            }

        hostname = socket.gethostname()

        try:
            result = await self._loop.run_in_executor(
                None,
                lambda: self._romm_api.register_device(
                    name=hostname,
                    platform="linux",
                    client="decky-romm-sync",
                    client_version=self._plugin_version,
                ),
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
            self._logger.warning(f"Server device registration failed: {e}")

        return {"success": False, "device_id": "", "device_name": "", "error": "registration_failed"}

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

    async def list_devices(self) -> dict:
        """List all devices registered with the RomM server for this user."""
        if not self._is_save_sync_enabled():
            return {"success": False, "devices": [], "disabled": True}
        try:
            own_id = self._get_server_device_id()
            devices = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_devices()),
            )
            own_id_str = str(own_id or "")
            enriched = [
                {**d, "is_current_device": bool(own_id_str) and (str(d.get("id") or "")) == own_id_str} for d in devices
            ]
            return {"success": True, "devices": enriched}
        except Exception as e:
            self._log_debug(f"list_devices failed: {e}")
            return {"success": False, "devices": [], "error": "list_failed"}

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

    async def _refresh_save_sort_state(self, where: str) -> None:
        """Refresh save-sort state from the live RetroArch config.

        Save-sync must observe fresh save-sort state before computing
        ``saves_dir``. This call ensures ``detect_save_sort_change`` has
        run at least once before we read state, closing the race where
        another frontend detect trigger arrives after our backend entry
        point. Without this, a direct-Steam-launch with no pre-detect
        would silently download stale server content to the wrong
        layout and destroy real user progress during the subsequent
        migration (#238).

        Graceful degradation: if detect fails (e.g. retroarch.cfg is
        temporarily unreadable) we log and continue with the
        previously-known state — save-sync must not abort because of a
        config read error.
        """
        if self._detect_sort_change is None:
            return
        try:
            await self._loop.run_in_executor(None, self._detect_sort_change)
        except Exception as e:
            self._logger.warning(
                "%s: detect_sort_change failed (%s) — proceeding with stale state",
                where,
                e,
            )

    async def pre_launch_sync(self, rom_id: int) -> dict:
        """Download newer saves from server before game launch."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._is_save_sync_enabled():
                return {"success": True, "message": "Save sync disabled", "synced": 0}

            # Defense in depth: block pre_launch_sync if a future caller bypasses
            # the @migration_blocked decorator at the public callable. saves_dir
            # would otherwise resolve under the new home and silently desync from
            # files still living at the old home. Internal _sync_rom_saves callers
            # (sync_all_saves, rollback_to_version) are protected by the decorator
            # on their own public callables — this guard is for pre_launch_sync.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            # Refresh save-sort state before the migration gate — see #238.
            await self._refresh_save_sort_state("pre_launch_sync")

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
                reg = await self.ensure_device_registered()
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
        rom_id = int(rom_id)

        async with self._rom_lock(rom_id):
            if not self._is_save_sync_enabled():
                self._logger.info("post_exit_sync skipped: save sync disabled")
                return {"success": True, "message": "Save sync disabled", "synced": 0}

            # Defense in depth: same rationale as pre_launch_sync — internal
            # _sync_rom_saves callers are protected by @migration_blocked on
            # their public callables; this guard covers post_exit_sync only.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                self._logger.info("post_exit_sync skipped: retrodeck migration pending")
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            settings = self._save_sync_state.get("settings", {})
            if not settings.get("sync_after_exit", True):
                self._logger.info("post_exit_sync skipped: sync_after_exit disabled")
                return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            await self._refresh_save_sort_state("post_exit_sync")

            try:
                await self._loop.run_in_executor(None, self._romm_api.heartbeat)
            except Exception:
                self._logger.info("post_exit_sync skipped: server offline")
                return {"success": False, "message": "Server offline", "synced": 0, "offline": True}

            if not self._save_sync_state.get("device_id"):
                reg = await self.ensure_device_registered()
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
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def sync_rom_saves(self, rom_id: int) -> dict:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._is_save_sync_enabled():
                return {"success": False, "message": _SYNC_DISABLED_MSG, "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            # Manual sync paths must observe fresh sort state too: a user could
            # edit retroarch.cfg outside of a session and then trigger a manual
            # sync before any detect has fired.
            await self._refresh_save_sort_state("sync_rom_saves")

            if not self._save_sync_state.get("device_id"):
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": _DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            self.save_state()

            msg = f"Synced {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
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
            raw = s.get("slot") or s.get("slot_name")
            name = raw if raw else ""
            merged[name] = {
                "source": "server",
                "count": s.get("count", 0),
                "latest_updated_at": (s.get("latest") or {}).get("updated_at"),
            }

        self._merge_persisted_slots(persisted_slots, merged, active_slot)

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

    @staticmethod
    def _merge_persisted_slots(
        persisted: dict[str, dict],
        merged: dict[str, dict],
        active_slot: str | None,
    ) -> None:
        """Add persisted local slots (or the active slot) that aren't on the server.

        Mutates ``merged`` in place. Local slots are always kept. A persisted
        server slot that's gone from the server is dropped unless it's the
        active slot — we want to keep the UI functional until the user
        explicitly switches away.
        """
        for name, info in persisted.items():
            if name in merged:
                continue
            if info.get("source") == "local":
                merged[name] = {"source": "local", "count": 0, "latest_updated_at": None}
            elif info.get("source") == "server" and name == (active_slot or ""):
                merged[name] = {"source": "server", "count": 0, "latest_updated_at": None}

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict:
        """Fetch server save files for a specific slot.

        Used by the frontend to show save files when expanding an inactive slot panel.
        Lightweight — no local file scanning or conflict detection.
        """
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        if not self._is_save_sync_enabled():
            return {"success": False, "slot": slot, "saves": [], "error": _SYNC_DISABLED_MSG}

        device_id = self._get_server_device_id()

        try:
            server_saves: list[dict] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                ),
            )
            saves = [
                {
                    "filename": s["file_name"],
                    "id": s["id"],
                    "size": s.get("file_size_bytes"),
                    "updated_at": s.get("updated_at", ""),
                    "emulator": s.get("emulator", ""),
                }
                for s in server_saves
            ]
            return {"success": True, "slot": slot, "saves": saves}
        except Exception as e:
            return {"success": False, "slot": slot, "saves": [], "error": str(e)}

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

        # Ensure slot is in the persisted slots dict (use "" as key for legacy/None)
        slot_key = resolved_slot if resolved_slot is not None else ""
        slots_dict: dict[str, dict] = saves[rom_id_str].setdefault("slots", {})
        if slot_key not in slots_dict:
            slots_dict[slot_key] = {"source": "local", "count": 0, "latest_updated_at": None}

        self.save_state()
        self._loop.create_task(self.check_save_status_background(rom_id))
        return {"success": True, "active_slot": resolved_slot}

    def _check_slot_switch_readiness(self, rom_id: int) -> dict:
        """Check whether it is safe to switch slots for this ROM.

        A switch is unsafe if local files have changed since the last sync
        to the current slot — those changes would be lost.
        Files that were never synced do not block (they'll be deleted on switch).

        Returns ``{"ready": True}`` or
        ``{"ready": False, "reason": str, "files": list[str]}``.
        """
        rom_id_str = str(rom_id)
        save_state = self._save_sync_state["saves"].get(rom_id_str, {})
        files_state = save_state.get("files", {})

        pending: list[str] = []
        local_files = self._find_save_files(rom_id)
        for lf in local_files:
            filename = lf["filename"]
            file_state = files_state.get(filename, {})
            last_sync_hash = file_state.get("last_sync_hash")
            if last_sync_hash:
                current_hash = self._file_md5(lf["path"])
                if current_hash != last_sync_hash:
                    pending.append(filename)

        if pending:
            return {"ready": False, "reason": "pending_uploads", "files": pending}

        return {"ready": True}

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict:
        """Switch the active save slot with immediate state sync.

        Pre-checks (all must pass):
        1. Save sync must be enabled.
        2. ROM must be installed.
        3. No local files with pending changes (changed since last sync to current slot).
        4. Server must be reachable.

        On success:
        - If the new slot has server saves: downloads them, replacing local files.
        - If the new slot is empty: deletes local save files (fresh start).
        - Never uploads — saves are not carried between slots.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        # 1. Save sync must be enabled
        if not self._is_save_sync_enabled():
            return {"success": False, "reason": "sync_disabled"}

        # 2. Slot normalisation (empty → None for legacy mode)
        slot_str = str(new_slot).strip() if new_slot else ""
        resolved_slot: str | None = slot_str if slot_str else None

        # 3. ROM must be installed
        info = self._get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "reason": "not_installed"}

        saves_dir = info["saves_dir"]
        system = info["system"]

        # 4. Check for pending local changes (hashing — run in executor)
        readiness = await self._loop.run_in_executor(None, self._check_slot_switch_readiness, rom_id)
        if not readiness.get("ready"):
            return {
                "success": False,
                "reason": readiness.get("reason", "pending_uploads"),
                "files": readiness.get("files", []),
            }

        # 5. Fetch server saves for the new slot (also proves server is reachable)
        device_id = self._get_server_device_id()
        try:
            all_server_saves: list[dict] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                ),
            )
        except Exception:
            return {"success": False, "reason": "server_unreachable"}

        # Filter to the target slot (FakeSaveApi doesn't filter, real API may not either)
        # Normalize "" and None both to None before comparing (legacy saves may use either)
        slot_saves = [s for s in all_server_saves if (s.get("slot") or None) == resolved_slot]

        # 6. Update active slot in state
        self.set_game_slot(rom_id, new_slot)

        # 7. Sync local state to match the new slot
        if slot_saves:
            # New slot has server saves — download them, replacing local files.
            # rom_name is guaranteed by the earlier ``info`` check (line 1642).
            await self._loop.run_in_executor(
                None,
                self._do_switch_downloads,
                slot_saves,
                saves_dir,
                rom_id_str,
                system,
                info["rom_name"],
            )
        else:
            # New slot is empty — delete local save files for a fresh start
            await self._loop.run_in_executor(
                None,
                self._delete_local_saves_for_switch,
                rom_id,
                rom_id_str,
            )

        # 8. Update last_sync_check_at
        save_entry = self._save_sync_state["saves"].setdefault(rom_id_str, {})
        save_entry["last_sync_check_at"] = datetime.now(UTC).isoformat()
        self.save_state()

        # 9. Return fresh status
        save_status = await self.get_save_status(rom_id)
        return {"success": True, "save_status": save_status}

    def _do_switch_downloads(
        self,
        slot_saves: list[dict],
        saves_dir: str,
        rom_id_str: str,
        system: str,
        rom_name: str,
    ) -> None:
        """Download all saves from *slot_saves* into *saves_dir*.

        Each save lands at ``<saves_dir>/<rom_name>.<server.file_extension>`` —
        the canonical RetroArch path. Runs synchronously; call via
        ``run_in_executor``.
        """
        for server_save in slot_saves:
            target = self._local_save_target(server_save, rom_name)
            self._do_download_save(server_save, saves_dir, target, rom_id_str, system)

    def _delete_local_saves_for_switch(self, rom_id: int, rom_id_str: str) -> None:
        """Delete local save files and clear file tracking state for a slot switch.

        Unlike delete_local_saves (the callable), this preserves slot config
        (active_slot, slot_confirmed, slots dict) and only clears files + tracking.
        Runs synchronously — call via run_in_executor.
        """
        local_files = self._find_save_files(rom_id)
        for lf in local_files:
            try:
                os.remove(lf["path"])
                self._log_debug(f"Deleted local save for switch: {lf['filename']}")
            except Exception as e:
                self._log_debug(f"Failed to delete {lf['filename']} during switch: {e}")

        # Clear file tracking state (but keep slot config)
        save_entry = self._save_sync_state.get("saves", {}).get(rom_id_str, {})
        save_entry["files"] = {}

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
            return {"success": False, "message": _SYNC_DISABLED_MSG, "synced": 0, "conflicts": 0}

        # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
        # Manual sync paths must observe fresh sort state too: a user could
        # edit retroarch.cfg outside of a session and then trigger a manual
        # sync before any detect has fired.
        await self._refresh_save_sort_state("sync_all_saves")

        if not self._save_sync_state.get("device_id"):
            reg = await self.ensure_device_registered()
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
            rom_id_int = int(rom_id_str)
            async with self._rom_lock(rom_id_int):
                synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id_int)
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

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        action: str,
    ) -> dict:
        """Resolve a pending sync conflict (true two-sided divergence).

        Reached when ``compute_sync_action`` returned ``Conflict`` — the
        server moved AND local diverged from baseline, so the user picked a
        side via the conflict UI.

        ``action`` is one of:

        - ``"keep_local"`` — push local to the current server save (PUT). When
          the local content already matches the server's content hash we adopt
          it silently without re-uploading.
        - ``"use_server"`` — download the current server save, replacing local.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        if action not in ("keep_local", "use_server"):
            return {"success": False, "message": f"Invalid action: {action}"}

        async with self._rom_lock(rom_id):
            info = self._get_rom_save_info(rom_id)
            if not info:
                return {"success": False, "message": "ROM not installed"}
            system = info["system"]
            saves_dir = info["saves_dir"]

            try:
                device_id = self._get_server_device_id()
                server_saves = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                    ),
                )
            except Exception as e:
                _code, _msg = classify_error(e)
                return {"success": False, "message": f"Failed to fetch saves: {_msg}"}

            save_state = self._save_sync_state["saves"].get(rom_id_str, {})
            active_slot = save_state.get("active_slot")
            server_in_slot = self._filter_server_saves_to_slot(server_saves, active_slot)
            if not server_in_slot:
                return {"success": False, "message": "No server save in active slot"}
            server = max(server_in_slot, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)

            try:
                if action == "use_server":
                    await self._loop.run_in_executor(
                        None,
                        self._apply_resolve_use_server,
                        rom_id_str,
                        server,
                        saves_dir,
                        system,
                        info["rom_name"],
                    )
                    self._logger.info(
                        "resolve_sync_conflict(rom_id=%d, filename=%s, action=%s) -> success",
                        rom_id,
                        filename,
                        action,
                    )
                    return {"success": True, "action": "use_server"}

                # keep_local
                await self._loop.run_in_executor(
                    None,
                    self._apply_resolve_keep_local,
                    rom_id,
                    rom_id_str,
                    filename,
                    server,
                    saves_dir,
                    system,
                )
                self._logger.info(
                    "resolve_sync_conflict(rom_id=%d, filename=%s, action=%s) -> success",
                    rom_id,
                    filename,
                    action,
                )
                return {"success": True, "action": "keep_local"}
            except Exception as e:
                self._logger.error(f"resolve_sync_conflict({rom_id}, {filename}, {action}) failed: {e}")
                return {"success": False, "message": str(e)}

    def _apply_resolve_use_server(
        self,
        rom_id_str: str,
        server: dict,
        saves_dir: str,
        system: str,
        rom_name: str,
    ) -> None:
        """Download *server* into the canonical local save file and update state.

        The write path is always ``<rom_name>.<server.file_extension>`` — the
        path RetroArch reads. Drives state-key consistency too:
        ``_update_file_sync_state`` receives the same target name the file
        lands at.
        """
        target = self._local_save_target(server, rom_name)
        self._do_download_save(server, saves_dir, target, rom_id_str, system)
        self.save_state()

    def _apply_resolve_keep_local(
        self,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        server: dict,
        saves_dir: str,
        system: str,
    ) -> None:
        """Push the local file to *server* (PUT). Adopt-without-upload when the
        local content already matches the server's content hash.
        """
        local_path = os.path.join(saves_dir, filename)
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local save not found: {local_path}")
        local_hash = self._file_md5(local_path)
        try:
            server_hash = self._retry.with_retry(self._get_server_save_hash, server)
        except Exception:
            server_hash = None

        if server_hash and local_hash == server_hash:
            # Hashes match — adopt server's id without re-uploading.
            self._log_debug(
                f"keep_local: hash matches server, adopting without upload (rom={rom_id} filename={filename})"
            )
            saves = self._save_sync_state.setdefault("saves", {})
            rom_entry = saves.setdefault(rom_id_str, {"files": {}})
            files = rom_entry.setdefault("files", {})
            file_state = files.setdefault(filename, {})
            file_state["tracked_save_id"] = server.get("id")
            file_state["last_sync_hash"] = local_hash
            file_state["last_sync_at"] = datetime.now(UTC).isoformat()
            file_state["last_sync_server_updated_at"] = server.get("updated_at", "")
            file_state["last_sync_server_size"] = server.get("file_size_bytes")
            file_state["last_sync_local_mtime"] = os.path.getmtime(local_path)
            file_state["last_sync_local_size"] = os.path.getsize(local_path)
            self.save_state()
            return

        # Upload local content as a PUT against the existing server save.
        self._do_upload_save(rom_id, local_path, filename, rom_id_str, system, server)
        self.save_state()

    # ------------------------------------------------------------------
    # Version History API
    # ------------------------------------------------------------------

    def _find_file_state(self, rom_id_str: str, filename: str, server_saves: list[dict]) -> dict:  # noqa: ARG002 — server_saves kept for callable signature stability
        """Look up the per-file sync state for *filename* (canonical local name).

        State keys are always ``<rom_name>.<ext>`` — the same canonical
        local filename produced by ``_local_save_target`` and consumed by
        RetroArch — so a single dict lookup is enough. The previous
        ``file_name_no_tags``-anchored slow path is gone.
        """
        files_state = self._save_sync_state.get("saves", {}).get(rom_id_str, {}).get("files", {})
        return files_state.get(filename, {})

    async def list_file_versions(self, rom_id: int, slot: str, filename: str) -> list[dict]:
        """List server-side saves in the active slot, excluding the currently-tracked one.

        The slot is the unit, not the filename. Saves uploaded by other
        clients (RomM web UI, third-party clients, etc.) whose naming
        convention differs from ours are first-class versions of the same
        slot, so no filename filter is applied — every save in the slot
        except the one we're currently tracking shows up here.

        Sorted by ``updated_at`` descending (newest first). Each entry
        contains: id, file_name, emulator, updated_at, file_size_bytes,
        device_syncs, uploaded_by_us.

        ``filename`` is kept in the signature for compatibility with the
        callable wiring but no longer affects which versions are returned.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        device_id = self._get_server_device_id()

        try:
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot if slot else None)
                ),
            )
        except Exception:
            return []

        file_state = self._find_file_state(rom_id_str, filename, server_saves)
        tracked_id = file_state.get("tracked_save_id")

        rom_state = self._save_sync_state["saves"].get(rom_id_str, {})
        raw = rom_state.get("own_upload_ids")
        own_upload_ids: list[int] | None = raw if isinstance(raw, list) else None

        versions = [
            {
                "id": s["id"],
                "file_name": s.get("file_name", ""),
                "emulator": s.get("emulator"),
                "updated_at": s.get("updated_at", ""),
                "file_size_bytes": s.get("file_size_bytes"),
                "device_syncs": s.get("device_syncs", []),
                "uploaded_by_us": (s["id"] in own_upload_ids) if own_upload_ids is not None else None,
            }
            for s in server_saves
            if s.get("id") != tracked_id
        ]

        versions.sort(key=lambda v: v["updated_at"], reverse=True)
        return versions

    def _rollback_to_version_io(
        self,
        rom_id_str: str,
        save_id: int,
        info: dict,
        server_saves: list[dict],
    ) -> dict:
        """Blocking I/O portion of the version-switch flow — runs in executor.

        The caller is responsible for the matrix pre-flight: by the time
        this function runs, the currently-tracked save is already in sync
        with the server (or the switch was aborted before we got here).
        This function is purely the destructive switch:

        1. Download id=save_id content → overwrite local file.
           ``_do_download_save`` updates ``tracked_save_id`` /
           ``last_sync_hash`` to point at the target version locally.
        2. PUT id=save_id with the same content. RomM v4.8.1 fires the
           SQLAlchemy ``onupdate=utc_now`` hook, so ``save.updated_at``
           becomes NOW and id=save_id is now newest in the slot — beating
           anything else there.
        3. ``_do_upload_save`` calls ``confirm_download(save_id, device_id)``,
           setting our ``last_synced_at = save.updated_at`` so
           ``is_current`` evaluates true for us. Required because v4.8.1
           PUT does NOT auto-upsert sync rows.
        4. ``_do_upload_save`` refreshes local sync state via
           ``_update_file_sync_state`` to match the post-PUT response.

        After this, the next ``compute_sync_action`` run picks id=save_id
        (now newest), our ``is_current=true``, hash matches →
        ``Skip(synced)``. Other devices on their next sync see id=save_id
        as newest with their ``is_current=false`` → ``Download`` → adopt
        our switch. Cross-device propagation works.
        """
        target_save = next(
            (s for s in server_saves if s.get("id") == save_id),
            None,
        )
        if target_save is None:
            return {"status": "not_found"}

        saves_dir = info["saves_dir"]
        system = info["system"]
        rom_name = info["rom_name"]
        target_filename = self._local_save_target(target_save, rom_name)
        local_path = os.path.join(saves_dir, target_filename)

        self._do_download_save(target_save, saves_dir, target_filename, rom_id_str, system)

        try:
            self._do_upload_save(
                rom_id=int(rom_id_str),
                file_path=local_path,
                filename=target_filename,
                rom_id_str=rom_id_str,
                system=system,
                server_save=target_save,
            )
        except Exception as e:
            # Download already mutated local state to reflect ``save_id``, so
            # the switch is locally complete — but cross-device propagation
            # failed because ``updated_at`` was not bumped. Surface this so
            # the caller can prompt the user to retry.
            self._logger.error(
                "_rollback_to_version_io: PUT to bump updated_at failed for rom=%s save=%s: %s",
                rom_id_str,
                save_id,
                e,
            )
            return {"status": "put_failed", "error": str(e)}

        return {"status": "ok"}

    async def rollback_to_version(self, rom_id: int, slot: str, save_id: int) -> dict:
        """Switch the local + tracked save to a chosen older server version.

        Flow:

        1. Run ``_sync_rom_saves`` as a matrix pre-flight on the
           currently-tracked save. The matrix decides:

           - ``Skip(synced)`` / ``Skip(adopt_baseline=True)`` — proceed.
           - ``Upload(POST/PUT)`` — silently push local up, then proceed.
           - ``Download(server)`` — silently adopt the server-newest, then
             proceed (the user's chosen target is still in the slot).
           - ``Conflict`` — abort with ``conflict_blocked``; user must
             resolve via the standard ``SyncConflictModal`` first.

        2. After a clean pre-flight, the destructive switch runs in
           ``_rollback_to_version_io``: download chosen → write to
           canonical local target → PUT same content → ``confirm_download``.

        ``filename`` is kept in the signature for callable-wiring stability
        but no longer drives any decision — the canonical local path is
        derived from the target save and the ROM name.

        Returns a status dict:
        - ``{"status": "ok"}`` on success.
        - ``{"status": "not_found"}`` if the ROM is not installed or the
          chosen save id is no longer on the server.
        - ``{"status": "conflict_blocked", "conflicts": [...]}`` if the
          pre-flight surfaced a conflict on the currently-tracked save.
          The frontend resolves it via the standard conflict modal.
        - ``{"status": "preflight_failed", "errors": [...]}`` if the
          pre-flight hit non-conflict errors (network, server, etc.).
          No switch was attempted.
        - ``{"status": "put_failed", "error": ...}`` if the local download
          succeeded but the server-side ``updated_at`` bump failed. Local
          file and state already point at the target; retrying is safe
          and idempotent. Without a successful re-PUT the switch will not
          propagate cross-device.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        save_id = int(save_id)

        async with self._rom_lock(rom_id):
            info = self._get_rom_save_info(rom_id)
            if not info:
                return {"status": "not_found"}

            # Matrix pre-flight: get the tracked save in sync first, or surface
            # a conflict that the user must resolve before any switch can run.
            _synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            if conflicts:
                self.save_state()
                return {
                    "status": "conflict_blocked",
                    "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
                }
            if errors:
                self.save_state()
                return {"status": "preflight_failed", "errors": errors}

            # Re-fetch server saves after the pre-flight: it may have created
            # or modified saves the switch needs to see.
            device_id = self._get_server_device_id()
            try:
                server_saves: list[dict] = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot if slot else None)
                    ),
                )
            except Exception as e:
                self._log_debug(f"rollback_to_version: failed to list saves: {e}")
                return {"status": "not_found"}

            result = await self._loop.run_in_executor(
                None,
                self._rollback_to_version_io,
                rom_id_str,
                save_id,
                info,
                server_saves,
            )

            if result.get("status") in ("ok", "put_failed"):
                self.save_state()

            return result

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

    # ------------------------------------------------------------------
    # Slot deletion
    # ------------------------------------------------------------------

    def _validate_slot_operation(self, rom_id: int, slot: str) -> dict | tuple[str, dict, dict[str, dict]]:
        """Shared validation for slot delete operations.

        Returns an error dict on failure, or a (rom_id_str, save_state, slots_dict)
        tuple on success.
        """
        if not self._is_save_sync_enabled():
            return {"success": False, "reason": "disabled"}
        if not self._get_rom_save_info(rom_id):
            return {"success": False, "reason": "not_installed"}
        rom_id_str = str(rom_id)
        save_state = self._save_sync_state.get("saves", {}).get(rom_id_str, {})
        slots_dict: dict[str, dict] = save_state.get("slots", {})
        if slot not in slots_dict:
            return {"success": False, "reason": "not_found"}
        return rom_id_str, save_state, slots_dict

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict:
        """Return info about what deleting a slot would do, for the confirmation modal."""
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        result = self._validate_slot_operation(rom_id, slot)
        if isinstance(result, dict):
            return result
        _rom_id_str, save_state, slots_dict = result

        slot_info = slots_dict[slot]
        source = slot_info.get("source", "server")
        active_slot = save_state.get("active_slot")
        is_active = slot == (active_slot or "")

        # Server save count
        server_save_ids: list[int] = []
        if source == "server":
            device_id = self._get_server_device_id()
            try:
                server_saves: list[dict] = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                    ),
                )
                server_save_ids = [s["id"] for s in server_saves]
            except Exception as e:
                self._log_debug(f"get_slot_delete_info: failed to list saves for slot '{slot}': {e}")

        # Local tracked files pointing to server saves in this slot
        files_state = save_state.get("files", {})
        local_filenames: list[str] = []
        if server_save_ids:
            id_set = set(server_save_ids)
            for filename, fstate in files_state.items():
                if fstate.get("tracked_save_id") in id_set:
                    local_filenames.append(filename)

        return {
            "success": True,
            "slot": slot,
            "source": source,
            "server_save_count": len(server_save_ids),
            "server_save_ids": server_save_ids,
            "local_file_count": len(local_filenames),
            "local_filenames": local_filenames,
            "is_active": is_active,
        }

    async def _delete_server_slot_saves(self, rom_id: int, slot: str) -> dict:
        """Delete all server saves in a slot. Returns result dict with count and IDs."""
        device_id = self._get_server_device_id()
        try:
            server_saves: list[dict] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                ),
            )
            save_ids = [s["id"] for s in server_saves]
            if save_ids:
                await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.delete_server_saves(save_ids),
                    ),
                )
            return {"success": True, "count": len(save_ids), "ids": set(save_ids)}
        except Exception as e:
            self._logger.warning(f"delete_slot: server delete failed for slot '{slot}': {e}")
            return {
                "success": False,
                "reason": "server_error",
                "message": f"Failed to delete server saves: {e}",
            }

    async def delete_slot(self, rom_id: int, slot: str) -> dict:
        """Delete a save slot and all its saves (local state + server if applicable)."""
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        result = self._validate_slot_operation(rom_id, slot)
        if isinstance(result, dict):
            return result
        _rom_id_str, save_state, slots_dict = result

        effective_active = save_state.get("active_slot") or ""
        if slot == effective_active:
            return {
                "success": False,
                "reason": "active_slot",
                "message": "Cannot delete the active slot. Switch to a different slot first.",
            }

        slot_info = slots_dict[slot]
        source = slot_info.get("source", "server")

        deleted_server_saves = 0
        cleaned_files = 0
        deleted_ids: set[int] = set()

        if source == "server":
            result = await self._delete_server_slot_saves(rom_id, slot)
            if not result["success"]:
                return result
            deleted_server_saves = result["count"]
            deleted_ids = result["ids"]

        # Clean up tracked file entries pointing to deleted saves
        files_state = save_state.get("files", {})
        if deleted_ids:
            to_remove = [fn for fn, fs in files_state.items() if fs.get("tracked_save_id") in deleted_ids]
            for fn in to_remove:
                del files_state[fn]
                cleaned_files += 1

        del slots_dict[slot]
        self.save_state()

        return {
            "success": True,
            "deleted_server_saves": deleted_server_saves,
            "cleaned_files": cleaned_files,
        }
