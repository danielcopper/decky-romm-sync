"""SQLite adapter for the ``Playtime`` aggregate.

Spans two tables: the per-ROM scalars live in ``rom_playtime`` and the pending
per-session outbox rows in ``rom_playtime_sessions``. ``get`` rebuilds the
aggregate from both; ``save`` writes the scalar row and replaces the child
outbox rows inside the unit-of-work's open transaction. Keyed externally by
``rom_id`` — the aggregate does not carry it as a field, so ``iter_all`` yields
``(rom_id, Playtime)`` pairs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.playtime import PendingPlaySession, PendingSessionRow, Playtime

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_STATE_COLUMNS = (
    "rom_id, total_seconds, session_count, last_session_start, "
    "last_session_start_monotonic, last_session_duration_sec, last_played"
)
_SESSION_COLUMNS = "rom_id, start_time, device_id, end_time, duration_ms, attempts"


def _row_to_pending(row: sqlite3.Row) -> PendingPlaySession:
    return PendingPlaySession(
        device_id=row["device_id"],
        end_time=row["end_time"],
        duration_ms=row["duration_ms"],
        attempts=row["attempts"],
    )


class SqlitePlaytimeRepository(BaseRepository):
    """Per-ROM cumulative play time, the open-session marker, and the pending-session outbox."""

    def _row_to_playtime(self, row: sqlite3.Row, pending: dict[str, PendingPlaySession]) -> Playtime:
        return Playtime(
            total_seconds=row["total_seconds"],
            session_count=row["session_count"],
            last_session_start=row["last_session_start"],
            last_session_start_monotonic=row["last_session_start_monotonic"],
            last_session_duration_sec=row["last_session_duration_sec"],
            last_played=row["last_played"],
            pending_sessions=pending,
        )

    def get(self, rom_id: int) -> Playtime | None:
        row = self._conn.execute(
            f"SELECT {_STATE_COLUMNS} FROM rom_playtime WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()
        if row is None:
            return None
        pending = {
            session_row["start_time"]: _row_to_pending(session_row)
            for session_row in self._conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM rom_playtime_sessions WHERE rom_id = ?",
                (rom_id,),
            )
        }
        return self._row_to_playtime(row, pending)

    def save(self, rom_id: int, playtime: Playtime) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO rom_playtime ({_STATE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rom_id,
                playtime.total_seconds,
                playtime.session_count,
                playtime.last_session_start,
                playtime.last_session_start_monotonic,
                playtime.last_session_duration_sec,
                playtime.last_played,
            ),
        )
        self._conn.execute("DELETE FROM rom_playtime_sessions WHERE rom_id = ?", (rom_id,))
        self._conn.executemany(
            f"INSERT INTO rom_playtime_sessions ({_SESSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (rom_id, start_time, session.device_id, session.end_time, session.duration_ms, session.attempts)
                for start_time, session in playtime.pending_sessions.items()
            ],
        )

    def iter_all(self) -> Iterator[tuple[int, Playtime]]:
        rom_ids = [row["rom_id"] for row in self._conn.execute("SELECT rom_id FROM rom_playtime")]
        for rom_id in rom_ids:
            playtime = self.get(rom_id)
            if playtime is not None:
                yield (rom_id, playtime)

    def iter_pending_sessions(self, limit: int) -> list[PendingSessionRow]:
        """Return up to ``limit`` outbox rows directly, cheapest-first, for the flush.

        A flat SELECT over ``rom_playtime_sessions`` — O(pending) not O(library) —
        so the flush never rebuilds every ROM's aggregate just to find the queued
        sessions. Ordered by ``(rom_id, start_time)`` for a stable, deterministic
        batch across successive drains.
        """
        rows = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM rom_playtime_sessions ORDER BY rom_id, start_time LIMIT ?",
            (limit,),
        )
        return [
            PendingSessionRow(
                rom_id=row["rom_id"],
                start_time=row["start_time"],
                device_id=row["device_id"],
                end_time=row["end_time"],
                duration_ms=row["duration_ms"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def rom_ids_with_pending_device(self, device_id: str) -> list[int]:
        """Return the rom_ids holding outbox rows addressed to *device_id*.

        A flat DISTINCT over the child table so a device re-address touches only
        the affected aggregates — never a full-library rebuild.
        """
        return [
            row["rom_id"]
            for row in self._conn.execute(
                "SELECT DISTINCT rom_id FROM rom_playtime_sessions WHERE device_id = ? ORDER BY rom_id",
                (device_id,),
            )
        ]
