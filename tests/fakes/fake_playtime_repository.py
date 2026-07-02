"""In-memory ``PlaytimeRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from domain.playtime import PendingSessionRow

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

    def iter_pending_sessions(self, limit: int) -> list[PendingSessionRow]:
        """Project up to *limit* outbox rows, ordered by ``(rom_id, start_time)``.

        Mirrors the SQLite adapter's flat SELECT so the flush's per-device
        grouping and index correlation see the same deterministic ordering.
        """
        rows = [
            PendingSessionRow(
                rom_id=rom_id,
                start_time=start_time,
                device_id=session.device_id,
                end_time=session.end_time,
                duration_ms=session.duration_ms,
                attempts=session.attempts,
            )
            for rom_id, playtime in self._playtime.items()
            for start_time, session in playtime.pending_sessions.items()
        ]
        rows.sort(key=lambda r: (r.rom_id, r.start_time))
        return rows[:limit]

    def _snapshot(self) -> dict[int, Playtime]:
        return copy.deepcopy(self._playtime)

    def _restore(self, state: dict[int, Playtime]) -> None:
        self._playtime = state
