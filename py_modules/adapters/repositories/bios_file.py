"""SQLite adapter for the ``BiosFile`` aggregate over the ``downloaded_bios`` table.

Composite identity ``(platform_slug, file_name)`` — ``get``/``delete`` take both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.bios_file import BiosFile

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_COLUMNS = "platform_slug, file_name, file_path, downloaded_at, firmware_id"


def _row_to_bios(row: sqlite3.Row) -> BiosFile:
    return BiosFile(
        platform_slug=row["platform_slug"],
        file_name=row["file_name"],
        file_path=row["file_path"],
        downloaded_at=row["downloaded_at"],
        firmware_id=row["firmware_id"],
    )


class SqliteBiosFileRepository(BaseRepository):
    """Downloaded BIOS records keyed by (platform_slug, file_name)."""

    def get(self, platform_slug: str, file_name: str) -> BiosFile | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM downloaded_bios WHERE platform_slug = ? AND file_name = ?",
            (platform_slug, file_name),
        ).fetchone()
        return _row_to_bios(row) if row is not None else None

    def save(self, bios_file: BiosFile) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO downloaded_bios ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (
                bios_file.platform_slug,
                bios_file.file_name,
                bios_file.file_path,
                bios_file.downloaded_at,
                bios_file.firmware_id,
            ),
        )

    def delete(self, platform_slug: str, file_name: str) -> None:
        self._conn.execute(
            "DELETE FROM downloaded_bios WHERE platform_slug = ? AND file_name = ?",
            (platform_slug, file_name),
        )

    def iter_all(self) -> Iterator[BiosFile]:
        for row in self._conn.execute(f"SELECT {_COLUMNS} FROM downloaded_bios"):
            yield _row_to_bios(row)

    def iter_by_platform(self, platform_slug: str) -> Iterator[BiosFile]:
        cursor = self._conn.execute(
            f"SELECT {_COLUMNS} FROM downloaded_bios WHERE platform_slug = ?",
            (platform_slug,),
        )
        for row in cursor:
            yield _row_to_bios(row)
