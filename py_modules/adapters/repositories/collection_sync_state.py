"""SQLite adapter for the ``CollectionSyncState`` aggregate over ``collection_sync_state``.

One row per synced standard/smart collection, keyed by the composite
``(collection_id, collection_kind)`` — the per-collection completion stamp the
incremental-skip gate reads (ADR-0023, the collection sibling of
``platform_sync_state``). A leaf table with no cascade children, so ``save``
upserts with ``INSERT OR REPLACE``, ``delete`` drops one collection's row (local
destructive flows that intersect a removed ROM), ``iter_all`` scans every stamp
(so a removal can find the ones whose member set contains a removed ROM), and
``clear`` drops the whole table (Force Full Sync). ``member_rom_ids`` is stored
as a JSON array TEXT and decoded back to a tuple.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.collection_sync_state import CollectionSyncState

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_COLUMNS = "collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids"


class SqliteCollectionSyncStateRepository(BaseRepository):
    """Per-collection completion stamps keyed by ``(collection_id, collection_kind)``."""

    def _row_to_state(self, row: sqlite3.Row) -> CollectionSyncState:
        return CollectionSyncState(
            collection_id=row["collection_id"],
            collection_kind=row["collection_kind"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            rom_count=row["rom_count"],
            member_rom_ids=tuple(int(rid) for rid in self._json_or_none(row["member_rom_ids"]) or []),
        )

    def get(self, collection_id: str, collection_kind: str) -> CollectionSyncState | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM collection_sync_state WHERE collection_id = ? AND collection_kind = ?",
            (collection_id, collection_kind),
        ).fetchone()
        return self._row_to_state(row) if row is not None else None

    def save(self, state: CollectionSyncState) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO collection_sync_state ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (
                state.collection_id,
                state.collection_kind,
                state.updated_at,
                state.completed_at,
                state.rom_count,
                self._json(list(state.member_rom_ids)),
            ),
        )

    def delete(self, collection_id: str, collection_kind: str) -> None:
        self._conn.execute(
            "DELETE FROM collection_sync_state WHERE collection_id = ? AND collection_kind = ?",
            (collection_id, collection_kind),
        )

    def iter_all(self) -> Iterator[CollectionSyncState]:
        for row in self._conn.execute(f"SELECT {_COLUMNS} FROM collection_sync_state").fetchall():
            yield self._row_to_state(row)

    def clear(self) -> None:
        self._conn.execute("DELETE FROM collection_sync_state")
