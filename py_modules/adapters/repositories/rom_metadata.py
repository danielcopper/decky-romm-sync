"""SQLite adapter for the ``RomMetadata`` aggregate over the ``rom_metadata`` table.

Keyed externally by ``rom_id``. The genres/companies/game_modes/steam_categories
columns are JSON-array TEXT; they round-trip to/from the aggregate's tuples.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.rom_metadata import RomMetadata

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_COLUMNS = (
    "rom_id, summary, genres, companies, game_modes, steam_categories, "
    "player_count, first_release_date, average_rating, cached_at"
)


class SqliteRomMetadataRepository(BaseRepository):
    """Cached RomM game metadata, keyed by rom_id."""

    def _row_to_metadata(self, row: sqlite3.Row) -> RomMetadata:
        return RomMetadata(
            summary=row["summary"],
            genres=tuple(self._json_or_none(row["genres"]) or ()),
            companies=tuple(self._json_or_none(row["companies"]) or ()),
            first_release_date=row["first_release_date"],
            average_rating=row["average_rating"],
            game_modes=tuple(self._json_or_none(row["game_modes"]) or ()),
            player_count=row["player_count"],
            cached_at=row["cached_at"],
            steam_categories=tuple(self._json_or_none(row["steam_categories"]) or ()),
        )

    def get(self, rom_id: int) -> RomMetadata | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM rom_metadata WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()
        return self._row_to_metadata(row) if row is not None else None

    def save(self, rom_id: int, metadata: RomMetadata) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO rom_metadata ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rom_id,
                metadata.summary,
                self._json(list(metadata.genres)),
                self._json(list(metadata.companies)),
                self._json(list(metadata.game_modes)),
                self._json(list(metadata.steam_categories)),
                metadata.player_count,
                metadata.first_release_date,
                metadata.average_rating,
                metadata.cached_at,
            ),
        )

    def delete(self, rom_id: int) -> None:
        self._conn.execute("DELETE FROM rom_metadata WHERE rom_id = ?", (rom_id,))

    def iter_all(self) -> Iterator[tuple[int, RomMetadata]]:
        for row in self._conn.execute(f"SELECT {_COLUMNS} FROM rom_metadata"):
            yield (row["rom_id"], self._row_to_metadata(row))
