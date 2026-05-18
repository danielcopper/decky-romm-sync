"""In-memory save-sync state and on-disk persistence orchestration.

Owns the live :class:`SaveSyncState` aggregate. Anything that mutates
the aggregate at service level (defaults seeding, file-tracking reset,
orphan pruning) or coordinates persistence with the
``SaveSyncStatePersister`` lives here. Schema migrations for newly
loaded payloads live in :class:`SaveSyncState.from_dict` — this service
never opens the JSON file directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.state import PluginState

from domain.save_state import (
    FileSyncState,
    RomSaveState,
    SaveSyncSettings,
    SaveSyncState,
)

if TYPE_CHECKING:
    import logging

    from services.protocols import SaveSyncStatePersister


@dataclass(frozen=True)
class StateServiceConfig:
    """Frozen wiring bundle handed to ``StateService.__init__``.

    Holds the live :class:`SaveSyncState` aggregate, the main plugin
    state dict (used for orphan-pruning against the shortcut registry),
    the Protocol-typed persister, and the standard-library logger.
    """

    save_sync_state: SaveSyncState
    state: PluginState
    persister: SaveSyncStatePersister
    logger: logging.Logger


class StateService:
    """Owns the live ``SaveSyncState`` aggregate and its persistence."""

    def __init__(self, *, config: StateServiceConfig) -> None:
        self._config = config
        self._save_sync_state = config.save_sync_state
        self._state = config.state
        self._persister = config.persister
        self._logger = config.logger

    @property
    def state(self) -> SaveSyncState:
        """Live reference to the typed save-sync aggregate."""
        return self._save_sync_state

    @staticmethod
    def make_default_state() -> SaveSyncState:
        """Return a fresh default save-sync state aggregate."""
        return SaveSyncState()

    def init_state(self) -> None:
        """No-op: the aggregate ships with defaults at construction.

        Kept for backward compatibility with callers that historically
        seeded a dict with default keys after construction. Schema
        migrations on loaded data live in :meth:`load_state`.
        """

    def load_state(self) -> None:
        """Load save-sync state from disk via the persister.

        Mutates the live aggregate in place so callers holding a
        reference (other services, sub-services, ``main.py``) keep
        observing the latest state without rewiring.
        """
        saved = self._persister.load()
        if saved is None:
            return
        loaded = SaveSyncState.from_dict(saved)
        self._save_sync_state.replace_with(loaded)

    def save_state(self) -> None:
        """Persist the live aggregate to disk via the injected persister."""
        self._persister.save(self._save_sync_state.to_dict())

    # ------------------------------------------------------------------
    # Typed accessors for the per-ROM substructure
    # ------------------------------------------------------------------

    def ensure_rom_state(self, rom_id_str: str) -> RomSaveState:
        """Return the per-ROM state, creating an empty one if missing."""
        return self._save_sync_state.saves.setdefault(rom_id_str, RomSaveState())

    def get_file_state(self, rom_id_str: str, filename: str) -> FileSyncState | None:
        """Return the typed per-file tracking entry, or ``None`` if not tracked."""
        rom = self._save_sync_state.saves.get(rom_id_str)
        if rom is None:
            return None
        return rom.files.get(filename)

    def get_settings(self) -> SaveSyncSettings:
        """Return the live settings dataclass."""
        return self._save_sync_state.settings

    # ------------------------------------------------------------------
    # File tracking mutations
    # ------------------------------------------------------------------

    def clear_files_state(self, rom_id_str: str) -> None:
        """Clear the per-file tracking dict for a ROM, preserving slot config.

        Resets the ROM's ``files`` dict to empty while leaving slot
        attribution and the rest of the entry untouched. Creates the
        ROM entry when none exists. Caller is responsible for persisting
        via :meth:`save_state`.
        """
        rom = self.ensure_rom_state(rom_id_str)
        rom.files = {}

    def prune_orphaned_state(self) -> None:
        """Remove save-sync state entries for rom_ids no longer in the shortcut registry."""
        registry = self._state.get("shortcut_registry", {})
        changed = False

        saves_stale = [rid for rid in self._save_sync_state.saves if rid not in registry]
        for rid in saves_stale:
            del self._save_sync_state.saves[rid]
            self._logger.info(f"Pruned orphaned save sync state: saves[{rid}]")
        if saves_stale:
            changed = True

        playtime_stale = [rid for rid in self._save_sync_state.playtime if rid not in registry]
        for rid in playtime_stale:
            del self._save_sync_state.playtime[rid]
            self._logger.info(f"Pruned orphaned save sync state: playtime[{rid}]")
        if playtime_stale:
            changed = True

        if changed:
            self.save_state()

    # ------------------------------------------------------------------
    # Settings convenience accessors
    # ------------------------------------------------------------------

    def is_save_sync_enabled(self) -> bool:
        """Whether the save-sync feature toggle is on."""
        return self._save_sync_state.settings.save_sync_enabled

    def get_server_device_id(self) -> str | None:
        """Server-side device id (None when this device is not yet registered)."""
        sid = self._save_sync_state.server_device_id
        return str(sid) if sid is not None else None

    def get_save_sync_settings(self) -> dict:
        """Return current save sync settings as the on-disk dict shape."""
        return self._save_sync_state.settings.to_dict()

    def update_save_sync_settings(self, settings: dict) -> dict:
        """Update save sync settings (sync toggles, slot, etc.)."""
        allowed_keys = {
            "save_sync_enabled",
            "sync_before_launch",
            "sync_after_exit",
            "default_slot",
            "autocleanup_limit",
        }

        current = self._save_sync_state.settings

        for key, value in settings.items():
            if key not in allowed_keys:
                continue
            value, skip = self._sanitize_setting(key, value)
            if skip:
                continue
            setattr(current, key, value)

        self.save_state()
        return {"success": True, "settings": current.to_dict()}

    @staticmethod
    def _sanitize_setting(key: str, value: object) -> tuple[object, bool]:
        """Validate and coerce a single settings key/value pair.

        Returns (coerced_value, skip) where skip=True means the value should
        be discarded (e.g. empty slot name).
        """
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
