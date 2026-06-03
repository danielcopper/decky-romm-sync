"""Pure mapping from a RomM list-response ROM dict to a ``RomMetadata`` aggregate.

The single place that knows how RomM's untrusted ``metadatum`` shape (summary,
genres, companies, ratings, release date in milliseconds, game modes) translates
into the cached :class:`~domain.rom_metadata.RomMetadata` aggregate, including the
derived Steam categories. Pure compute — the caller supplies ``cached_at`` so no
clock is read here; staleness and persistence live elsewhere.
"""

from __future__ import annotations

from typing import Any

from domain.rom_metadata import RomMetadata
from domain.steam_categories import build_steam_categories


def build_rom_metadata(rom: dict[str, Any], cached_at: float) -> RomMetadata:
    """Map a RomM ROM dict + ``cached_at`` epoch into a ``RomMetadata`` aggregate.

    Reads the nested ``metadatum`` block, normalising RomM's wire quirks:
    ``first_release_date`` is milliseconds (divided to whole seconds),
    ``average_rating`` coerces to ``float``, and the list-shaped fields fall
    back to empty tuples when absent or ``None``. Steam categories are derived
    from the genres + game modes via :func:`build_steam_categories`. Untrusted
    numeric fields may raise ``ValueError`` / ``TypeError`` on bad input — the
    caller decides whether to skip that ROM.
    """
    metadatum = rom.get("metadatum") or {}

    first_release_date = metadatum.get("first_release_date")
    if first_release_date is not None:
        first_release_date = int(first_release_date) // 1000

    average_rating = metadatum.get("average_rating")
    if average_rating is not None:
        average_rating = float(average_rating)

    genres = tuple(metadatum.get("genres") or [])
    game_modes = tuple(metadatum.get("game_modes") or [])
    steam_categories = tuple(build_steam_categories(list(genres), list(game_modes)))

    return RomMetadata.cached(
        summary=rom.get("summary", "") or "",
        genres=genres,
        companies=tuple(metadatum.get("companies") or []),
        first_release_date=first_release_date,
        average_rating=average_rating,
        game_modes=game_modes,
        player_count=metadatum.get("player_count", "") or "",
        cached_at=cached_at,
        steam_categories=steam_categories,
    )
