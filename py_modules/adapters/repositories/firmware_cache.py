"""SQLite adapter for the ``FirmwareCacheEntry`` aggregate over the ``firmware_cache`` table.

Composite identity ``(platform_slug, name)``. The cache is refreshed wholesale
(``replace_all``), not mutated per row; ``get_cache_epoch`` reads any row's
``cached_at`` for the whole-cache TTL check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.firmware_cache import FirmwareCacheEntry

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_COLUMNS = "platform_slug, name, id, file_size_bytes, cached_at"


def _row_to_entry(row: sqlite3.Row) -> FirmwareCacheEntry:
    return FirmwareCacheEntry(
        id=row["id"],
        name=row["name"],
        platform_slug=row["platform_slug"],
        file_size_bytes=row["file_size_bytes"],
        cached_at=row["cached_at"],
    )


class SqliteFirmwareCacheRepository(BaseRepository):
    """TTL-cached RomM firmware inventory, replaced wholesale on refresh."""

    def get(self, platform_slug: str, name: str) -> FirmwareCacheEntry | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM firmware_cache WHERE platform_slug = ? AND name = ?",
            (platform_slug, name),
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def iter_all(self) -> Iterator[FirmwareCacheEntry]:
        for row in self._conn.execute(f"SELECT {_COLUMNS} FROM firmware_cache"):
            yield _row_to_entry(row)

    def replace_all(self, entries: list[FirmwareCacheEntry]) -> None:
        self._conn.execute("DELETE FROM firmware_cache")
        self._conn.executemany(
            f"INSERT OR REPLACE INTO firmware_cache ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    entry.platform_slug,
                    entry.name,
                    entry.id,
                    entry.file_size_bytes,
                    entry.cached_at,
                )
                for entry in entries
            ],
        )

    def clear(self) -> None:
        self._conn.execute("DELETE FROM firmware_cache")

    def get_cache_epoch(self) -> float | None:
        row = self._conn.execute("SELECT cached_at FROM firmware_cache LIMIT 1").fetchone()
        return row["cached_at"] if row is not None else None
