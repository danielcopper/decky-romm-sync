"""SQLite adapter for the ``Playtime`` aggregate over the ``rom_playtime`` table.

Keyed externally by ``rom_id`` — the aggregate does not carry it as a field, so
``iter_all`` yields ``(rom_id, Playtime)`` pairs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.playtime import Playtime

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_COLUMNS = "rom_id, total_seconds, session_count, last_session_start, last_session_duration_sec, note_id"


def _row_to_playtime(row: sqlite3.Row) -> Playtime:
    return Playtime(
        total_seconds=row["total_seconds"],
        session_count=row["session_count"],
        last_session_start=row["last_session_start"],
        last_session_duration_sec=row["last_session_duration_sec"],
        note_id=row["note_id"],
    )


class SqlitePlaytimeRepository(BaseRepository):
    """Per-ROM cumulative play time and the open-session marker."""

    def get(self, rom_id: int) -> Playtime | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM rom_playtime WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()
        return _row_to_playtime(row) if row is not None else None

    def save(self, rom_id: int, playtime: Playtime) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO rom_playtime ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (
                rom_id,
                playtime.total_seconds,
                playtime.session_count,
                playtime.last_session_start,
                playtime.last_session_duration_sec,
                playtime.note_id,
            ),
        )

    def delete(self, rom_id: int) -> None:
        self._conn.execute("DELETE FROM rom_playtime WHERE rom_id = ?", (rom_id,))

    def iter_all(self) -> Iterator[tuple[int, Playtime]]:
        for row in self._conn.execute(f"SELECT {_COLUMNS} FROM rom_playtime"):
            yield (row["rom_id"], _row_to_playtime(row))
