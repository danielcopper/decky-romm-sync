"""In-memory ``PlaytimeRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.playtime import Playtime


class FakePlaytimeRepository:
    """Dict-backed ``PlaytimeRepository`` keyed by externally-supplied ``rom_id``."""

    def __init__(self) -> None:
        self._playtime: dict[int, Playtime] = {}
        self.save_count = 0

    def get(self, rom_id: int) -> Playtime | None:
        return copy.deepcopy(self._playtime.get(rom_id))

    def save(self, rom_id: int, playtime: Playtime) -> None:
        self.save_count += 1
        self._playtime[rom_id] = copy.deepcopy(playtime)

    def delete(self, rom_id: int) -> None:
        self._playtime.pop(rom_id, None)

    def iter_all(self) -> Iterator[tuple[int, Playtime]]:
        return iter([(rom_id, copy.deepcopy(playtime)) for rom_id, playtime in self._playtime.items()])

    def _snapshot(self) -> dict[int, Playtime]:
        return copy.deepcopy(self._playtime)

    def _restore(self, state: dict[int, Playtime]) -> None:
        self._playtime = state
