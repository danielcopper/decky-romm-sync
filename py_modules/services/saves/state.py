"""In-memory save-sync state and on-disk persistence orchestration.

Owns the live :class:`SaveSyncState` aggregate. Anything that mutates
the aggregate at service level (defaults seeding, file-tracking reset,
orphan pruning) or coordinates persistence with the
``SaveSyncStatePersister`` lives here. Schema migrations for newly
loaded payloads live in :class:`SaveSyncState.from_dict` — this service
never opens the JSON file directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.save_state import (
    FileSyncState,
    PlaytimeEntry,
    RomSaveState,
    SaveSyncSettings,
    SaveSyncState,
)

if TYPE_CHECKING:
    import logging

    from services.protocols import SaveSyncStatePersister


class StateService:
    """Owns the live ``SaveSyncState`` aggregate and its persistence."""

    def __init__(
        self,
        *,
        save_sync_state: SaveSyncState,
        state: dict,
        persister: SaveSyncStatePersister,
        logger: logging.Logger,
    ) -> None:
        self._save_sync_state = save_sync_state
        self._state = state
        self._persister = persister
        self._logger = logger

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

    def get_rom_state(self, rom_id_str: str) -> RomSaveState | None:
        """Return the typed per-ROM state, or ``None`` if not tracked."""
        return self._save_sync_state.saves.get(rom_id_str)

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

    def get_playtime(self, rom_id_str: str) -> PlaytimeEntry | None:
        """Return the typed playtime entry, or ``None`` if not tracked."""
        return self._save_sync_state.playtime.get(rom_id_str)

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
