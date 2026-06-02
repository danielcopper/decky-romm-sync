"""SQLite adapter for the ``Rom`` aggregate over the ``roms`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.rom import Rom

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_COLUMNS = "rom_id, platform_slug, name, fs_name, shortcut_app_id, last_synced_at, cover_path, igdb_id, sgdb_id, ra_id"


def _row_to_rom(row: sqlite3.Row) -> Rom:
    return Rom(
        rom_id=row["rom_id"],
        platform_slug=row["platform_slug"],
        name=row["name"],
        fs_name=row["fs_name"],
        shortcut_app_id=row["shortcut_app_id"],
        last_synced_at=row["last_synced_at"],
        cover_path=row["cover_path"],
        igdb_id=row["igdb_id"],
        sgdb_id=row["sgdb_id"],
        ra_id=row["ra_id"],
    )


class SqliteRomRepository(BaseRepository):
    """The synced-shortcut registry, one row per tracked ROM."""

    def get(self, rom_id: int) -> Rom | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM roms WHERE rom_id = ?", (rom_id,)).fetchone()
        return _row_to_rom(row) if row is not None else None

    def get_by_app_id(self, app_id: int) -> Rom | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM roms WHERE shortcut_app_id = ?",
            (app_id,),
        ).fetchone()
        return _row_to_rom(row) if row is not None else None

    def save(self, rom: Rom) -> None:
        # UPSERT (ON CONFLICT … DO UPDATE), never INSERT OR REPLACE: REPLACE
        # deletes-then-inserts the parent row, and that DELETE fires the
        # ON DELETE CASCADE on the per-ROM child tables, silently wiping install,
        # playtime, and save-sync baselines on every re-sync (#887).
        self._conn.execute(
            f"INSERT INTO roms ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(rom_id) DO UPDATE SET "
            "platform_slug = excluded.platform_slug, "
            "name = excluded.name, "
            "fs_name = excluded.fs_name, "
            "shortcut_app_id = excluded.shortcut_app_id, "
            "last_synced_at = excluded.last_synced_at, "
            "cover_path = excluded.cover_path, "
            "igdb_id = excluded.igdb_id, "
            "sgdb_id = excluded.sgdb_id, "
            "ra_id = excluded.ra_id",
            (
                rom.rom_id,
                rom.platform_slug,
                rom.name,
                rom.fs_name,
                rom.shortcut_app_id,
                rom.last_synced_at,
                rom.cover_path,
                rom.igdb_id,
                rom.sgdb_id,
                rom.ra_id,
            ),
        )

    def delete(self, rom_id: int) -> None:
        self._conn.execute("DELETE FROM roms WHERE rom_id = ?", (rom_id,))

    def iter_all(self) -> Iterator[Rom]:
        for row in self._conn.execute(f"SELECT {_COLUMNS} FROM roms"):
            yield _row_to_rom(row)

    def iter_by_platform(self, platform_slug: str) -> Iterator[Rom]:
        cursor = self._conn.execute(
            f"SELECT {_COLUMNS} FROM roms WHERE platform_slug = ?",
            (platform_slug,),
        )
        for row in cursor:
            yield _row_to_rom(row)

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM roms").fetchone()[0])
