"""In-memory ``RomSaveStateRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.rom_save_state import RomSaveState


class FakeRomSaveStateRepository:
    """Dict-backed ``RomSaveStateRepository`` keyed by ``rom_id``.

    The real adapter spans two tables; the fake stores the whole aggregate (the
    ``files`` mapping included) under one key, deep-copied on ``save``.
    """

    def __init__(self) -> None:
        self._states: dict[int, RomSaveState] = {}
        self.save_count = 0

    def get(self, rom_id: int) -> RomSaveState | None:
        return copy.deepcopy(self._states.get(rom_id))

    def save(self, rom_id: int, state: RomSaveState) -> None:
        self.save_count += 1
        self._states[rom_id] = copy.deepcopy(state)

    def delete(self, rom_id: int) -> None:
        self._states.pop(rom_id, None)

    def iter_all(self) -> Iterator[tuple[int, RomSaveState]]:
        return iter([(rom_id, copy.deepcopy(state)) for rom_id, state in self._states.items()])

    def _snapshot(self) -> dict[int, RomSaveState]:
        return copy.deepcopy(self._states)

    def _restore(self, state: dict[int, RomSaveState]) -> None:
        self._states = state
