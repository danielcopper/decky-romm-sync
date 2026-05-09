"""Single source of truth for save_sync_state.json.

Anything that loads, persists, migrates, or owns the on-disk save-sync
state lives here. Other modules read state via the ``data`` property and
trigger persistence via ``save_state()`` — they never open the file
directly.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os


class StateService:
    """Owns ``save_sync_state.json`` — single source of truth for on-disk save-sync state."""

    def __init__(
        self,
        *,
        save_sync_state: dict,
        state: dict,
        runtime_dir: str,
        logger: logging.Logger,
    ) -> None:
        self._save_sync_state = save_sync_state
        self._state = state
        self._runtime_dir = runtime_dir
        self._logger = logger

    @property
    def data(self) -> dict:
        """Live reference to the in-memory state dict."""
        return self._save_sync_state

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
                "sync_before_launch": True,
                "sync_after_exit": True,
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
        - Strip removed legacy settings keys (``conflict_mode``,
          ``clock_skew_tolerance_sec``).
        """
        self._migrate_saves_entries()
        self._strip_legacy_settings()

    def _migrate_saves_entries(self) -> None:
        """Rename ``active_core`` → ``last_synced_core`` and drop dead per-file flags."""
        saves = self._save_sync_state.get("saves")
        if not isinstance(saves, dict):
            return
        for entry in saves.values():
            if not isinstance(entry, dict):
                continue
            if "active_core" in entry:
                entry["last_synced_core"] = entry.pop("active_core")
            files = entry.get("files")
            if not isinstance(files, dict):
                continue
            for file_state in files.values():
                if isinstance(file_state, dict):
                    file_state.pop("dismissed_newer_save_id", None)

    def _strip_legacy_settings(self) -> None:
        """Strip removed settings keys from loaded state.

        Old state files keep these forever otherwise (``load_state`` does
        ``dict.update`` on settings, so orphan keys survive). Idempotent.
        """
        settings = self._save_sync_state.get("settings")
        if isinstance(settings, dict):
            settings.pop("conflict_mode", None)
            settings.pop("clock_skew_tolerance_sec", None)

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

    def clear_files_state(self, rom_id_str: str) -> None:
        """Clear the per-file tracking dict for a ROM, preserving slot config.

        Resets ``data["saves"][rom_id_str]["files"]`` to an empty dict while
        leaving ``active_slot``, ``slot_confirmed``, ``emulator``,
        ``last_synced_core``, ``own_upload_ids``, ``slots``, ``system``, and any
        other slot/attribution metadata untouched. Creates the ROM entry as an
        empty dict (with only ``files``) when none exists. Caller is
        responsible for persisting via ``save_state()``.
        """
        saves = self._save_sync_state.setdefault("saves", {})
        entry = saves.setdefault(rom_id_str, {})
        entry["files"] = {}

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
