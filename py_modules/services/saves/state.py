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
    from services.protocols.persistence import SettingsPersister


@dataclass(frozen=True)
class StateServiceConfig:
    """Frozen wiring bundle handed to ``StateService.__init__``.

    Holds the live :class:`SaveSyncState` aggregate, the main plugin
    state dict (used for orphan-pruning against the shortcut registry),
    the live ``settings.json`` dict + its Protocol-typed persister (the
    home of the save-sync feature toggles and the device label), the
    save-sync-state persister, and the standard-library logger.
    """

    save_sync_state: SaveSyncState
    state: PluginState
    settings: dict
    persister: SaveSyncStatePersister
    settings_persister: SettingsPersister
    logger: logging.Logger


class StateService:
    """Owns the live ``SaveSyncState`` aggregate and its persistence.

    Also the settings.json-backed owner of the five save-sync feature
    toggles and the device label: those values live in the injected
    settings dict and flush through the ``SettingsPersister``, not the
    save-sync aggregate.
    """

    def __init__(self, *, config: StateServiceConfig) -> None:
        self._config = config
        self._save_sync_state = config.save_sync_state
        self._state = config.state
        self._settings = config.settings
        self._persister = config.persister
        self._settings_persister = config.settings_persister
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
        """Build a read-view of the five save-sync knobs from the settings dict.

        Applies the same coercions the legacy on-disk parse did: booleans
        via ``bool(...)``; ``default_slot`` keeps ``None`` (no-slots mode)
        and collapses empty strings to ``None``; ``autocleanup_limit`` via
        ``int(...)`` with a ``or 10`` guard against ``0`` / ``None``.
        """
        raw_slot = self._settings.get("default_slot", "default")
        if raw_slot is None:
            default_slot: str | None = None
        else:
            slot_str = str(raw_slot)
            default_slot = slot_str if slot_str else None
        return SaveSyncSettings(
            save_sync_enabled=bool(self._settings.get("save_sync_enabled", False)),
            sync_before_launch=bool(self._settings.get("sync_before_launch", True)),
            sync_after_exit=bool(self._settings.get("sync_after_exit", True)),
            default_slot=default_slot,
            autocleanup_limit=int(self._settings.get("autocleanup_limit", 10) or 10),
        )

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
        rom.files = {}  # pragma: no aggregate-check  (rom is a RomSaveState; becomes a method in #788 PR4)

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
        return bool(self._settings.get("save_sync_enabled", False))

    def get_server_device_id(self) -> str | None:
        """Server-side device id (None when this device is not yet registered)."""
        sid = self._save_sync_state.server_device_id
        return str(sid) if sid is not None else None

    def get_save_sync_settings(self) -> dict:
        """Return current save sync settings as the frontend dict shape."""
        return self.get_settings().to_dict()

    def update_save_sync_settings(self, settings: dict) -> dict:
        """Update save sync settings (sync toggles, slot, etc.) in settings.json."""
        allowed_keys = {
            "save_sync_enabled",
            "sync_before_launch",
            "sync_after_exit",
            "default_slot",
            "autocleanup_limit",
        }

        for key, value in settings.items():
            if key not in allowed_keys:
                continue
            value, skip = self._sanitize_setting(key, value)
            if skip:
                continue
            self._settings[key] = value

        self._settings_persister.save_settings()
        return {"success": True, "settings": self.get_settings().to_dict()}

    def get_device_name(self) -> str | None:
        """Return the user-set device label from settings.json (``None`` if unset)."""
        return self._settings.get("device_name")

    def set_device_name(self, name: str) -> None:
        """Persist the device label to settings.json."""
        self._settings["device_name"] = name
        self._settings_persister.save_settings()

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
