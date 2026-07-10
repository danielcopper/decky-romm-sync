"""SQLite adapter for the ``PlatformSyncState`` aggregate over ``platform_sync_state``.

One row per platform, keyed by ``platform_slug`` — the per-platform completion
stamp the incremental-skip gate reads (ADR-0023). A leaf table with no cascade
children, so ``save`` upserts with ``INSERT OR REPLACE``, ``delete`` drops one
platform's row (apply-start clear + local destructive flows), and ``clear`` drops
the whole table (Force Full Sync).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.platform_sync_state import PlatformSyncState

if TYPE_CHECKING:
    import sqlite3

_COLUMNS = "platform_slug, completed_at, rom_count"


def _row_to_state(row: sqlite3.Row) -> PlatformSyncState:
    return PlatformSyncState(
        platform_slug=row["platform_slug"],
        completed_at=row["completed_at"],
        rom_count=row["rom_count"],
    )


class SqlitePlatformSyncStateRepository(BaseRepository):
    """Per-platform completion stamps keyed by ``platform_slug``."""

    def get(self, platform_slug: str) -> PlatformSyncState | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM platform_sync_state WHERE platform_slug = ?",
            (platform_slug,),
        ).fetchone()
        return _row_to_state(row) if row is not None else None

    def save(self, state: PlatformSyncState) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO platform_sync_state ({_COLUMNS}) VALUES (?, ?, ?)",
            (state.platform_slug, state.completed_at, state.rom_count),
        )

    def delete(self, platform_slug: str) -> None:
        self._conn.execute("DELETE FROM platform_sync_state WHERE platform_slug = ?", (platform_slug,))

    def clear(self) -> None:
        self._conn.execute("DELETE FROM platform_sync_state")
