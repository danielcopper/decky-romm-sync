"""SQLite adapter for the ``SyncRun`` aggregate over the ``sync_runs`` table.

History table — one row per run. ``get_latest_completed`` finds the newest
``completed`` row; ``get_running`` finds the single in-flight run. The
platforms_completed/collections_completed columns are nullable JSON arrays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from adapters.repositories._base import BaseRepository
from domain.sync_run import SyncRun, SyncRunStatus

if TYPE_CHECKING:
    import sqlite3

_COLUMNS = (
    "id, started_at, status, platforms_planned, roms_planned, finished_at, "
    "platforms_completed, collections_completed, error"
)


class SqliteSyncRunRepository(BaseRepository):
    """Sync-run history, identified by a string UUID."""

    def _row_to_run(self, row: sqlite3.Row) -> SyncRun:
        return SyncRun(
            id=row["id"],
            started_at=row["started_at"],
            status=cast("SyncRunStatus", row["status"]),
            platforms_planned=row["platforms_planned"],
            roms_planned=row["roms_planned"],
            finished_at=row["finished_at"],
            platforms_completed=self._json_or_none(row["platforms_completed"]),
            collections_completed=self._json_or_none(row["collections_completed"]),
            error=row["error"],
        )

    def get(self, run_id: str) -> SyncRun | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row is not None else None

    def save(self, run: SyncRun) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO sync_runs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.started_at,
                run.status,
                run.platforms_planned,
                run.roms_planned,
                run.finished_at,
                None if run.platforms_completed is None else self._json(run.platforms_completed),
                None if run.collections_completed is None else self._json(run.collections_completed),
                run.error,
            ),
        )

    def get_latest_completed(self) -> SyncRun | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM sync_runs WHERE status = 'completed' ORDER BY started_at DESC LIMIT 1",
        ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def get_running(self) -> SyncRun | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM sync_runs WHERE status = 'running' LIMIT 1",
        ).fetchone()
        return self._row_to_run(row) if row is not None else None
