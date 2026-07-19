"""In-memory ``RomSaveSyncStateRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.rom_save_sync_state import RomSaveSyncState


class FakeRomSaveSyncStateRepository:
    """Dict-backed ``RomSaveSyncStateRepository`` keyed by ``rom_id``.

    The real adapter spans two tables; the fake stores the whole aggregate (the
    ``files`` mapping included) under one key, deep-copied on ``save``.
    """

    def __init__(self) -> None:
        self._states: dict[int, RomSaveSyncState] = {}
        self.save_count = 0

    def get(self, rom_id: int) -> RomSaveSyncState | None:
        return copy.deepcopy(self._states.get(rom_id))

    def save(self, rom_id: int, state: RomSaveSyncState) -> None:
        # Mirror rom_save_files' NOT NULL on last_sync_hash so service tests reject
        # what real SQLite rejects (db/migrations/001_initial.sql). tracked_save_id is
        # intentionally NOT checked — it is nullable (hash-only baselines).
        for filename, file in state.files.items():
            if not file.last_sync_hash:
                raise ValueError(f"rom_save_files.last_sync_hash is NOT NULL; file {filename!r} has none")
        self.save_count += 1
        self._states[rom_id] = copy.deepcopy(state)

    def delete(self, rom_id: int) -> None:
        self._states.pop(rom_id, None)

    def iter_all(self) -> Iterator[tuple[int, RomSaveSyncState]]:
        return iter([(rom_id, copy.deepcopy(state)) for rom_id, state in self._states.items()])

    def _snapshot(self) -> dict[int, RomSaveSyncState]:
        return copy.deepcopy(self._states)

    def _restore(self, state: dict[int, RomSaveSyncState]) -> None:
        self._states = state
