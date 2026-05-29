"""SQLite adapter for the ``RomInstall`` aggregate over the ``rom_installs`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.rom_install import RomInstall

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_COLUMNS = "rom_id, file_path, install_path, platform_slug, system, installed_at"


def _row_to_install(row: sqlite3.Row) -> RomInstall:
    return RomInstall(
        rom_id=row["rom_id"],
        file_path=row["file_path"],
        install_path=row["install_path"],
        platform_slug=row["platform_slug"],
        system=row["system"],
        installed_at=row["installed_at"],
    )


class SqliteRomInstallRepository(BaseRepository):
    """Install records — present only while a ROM is downloaded (all columns NOT NULL)."""

    def get(self, rom_id: int) -> RomInstall | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM rom_installs WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()
        return _row_to_install(row) if row is not None else None

    def save(self, install: RomInstall) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO rom_installs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (
                install.rom_id,
                install.file_path,
                install.install_path,
                install.platform_slug,
                install.system,
                install.installed_at,
            ),
        )

    def delete(self, rom_id: int) -> None:
        self._conn.execute("DELETE FROM rom_installs WHERE rom_id = ?", (rom_id,))

    def iter_all(self) -> Iterator[RomInstall]:
        for row in self._conn.execute(f"SELECT {_COLUMNS} FROM rom_installs"):
            yield _row_to_install(row)
