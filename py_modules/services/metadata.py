"""MetadataService — ROM metadata caching.

Handles ROM metadata extraction, caching (with periodic flush),
and app_id→rom_id mapping. Metadata is populated during sync via
the list API and served from cache on demand (no detail API calls).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from models.metadata import RomMetadata

from domain.steam_categories import build_steam_categories

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import Clock, DebugLogger, RommApiProtocol, StatePersister


@dataclass(frozen=True)
class MetadataServiceConfig:
    """Frozen wiring bundle handed to ``MetadataService.__init__``.

    Holds the Protocol-typed RomM adapter, the live state and metadata
    cache dicts, runtime infrastructure, persistence callback, and the
    clock/debug-logger seams MetadataService needs at construction time.
    """

    romm_api: RommApiProtocol
    state: dict
    metadata_cache: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    save_metadata_cache: StatePersister
    log_debug: DebugLogger


class MetadataService:
    """ROM metadata cache: extract, store, flush, and fetch on demand."""

    def __init__(self, *, config: MetadataServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._state = config.state
        self._metadata_cache = config.metadata_cache
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._save_metadata_cache = config.save_metadata_cache
        self._log_debug = config.log_debug

        self._metadata_dirty_count = 0
        self._METADATA_FLUSH_INTERVAL = 50

    def extract_metadata(self, rom):
        """Extract metadata fields from a ROM dict into cache format."""
        metadatum = rom.get("metadatum") or {}
        first_release_date = metadatum.get("first_release_date")
        if first_release_date is not None:
            first_release_date = int(first_release_date) // 1000
        average_rating = metadatum.get("average_rating")
        if average_rating is not None:
            average_rating = float(average_rating)
        genres_list = metadatum.get("genres") or []
        game_modes_list = metadatum.get("game_modes") or []
        steam_cats = build_steam_categories(genres_list, game_modes_list)
        return asdict(
            RomMetadata(
                summary=rom.get("summary", "") or "",
                genres=tuple(genres_list),
                companies=tuple(metadatum.get("companies") or []),
                first_release_date=first_release_date,
                average_rating=average_rating,
                game_modes=tuple(game_modes_list),
                player_count=metadatum.get("player_count", "") or "",
                cached_at=self._clock.time(),
                steam_categories=tuple(steam_cats),
            )
        )

    def mark_metadata_dirty(self):
        """Track metadata cache changes and flush to disk periodically."""
        self._metadata_dirty_count += 1
        if self._metadata_dirty_count >= self._METADATA_FLUSH_INTERVAL:
            self._save_metadata_cache()
            self._metadata_dirty_count = 0

    def flush_metadata_if_dirty(self):
        """Flush metadata cache to disk if any pending writes."""
        if self._metadata_dirty_count > 0:
            self._save_metadata_cache()
            self._metadata_dirty_count = 0

    def get_rom_metadata(self, rom_id):
        """Return cached metadata for a ROM.

        Metadata is populated during sync via the list API. This method
        returns whatever is cached — stale or fresh — and never calls
        the detail API (GET /api/roms/{id}), which can timeout for ROMs
        with very large file lists (e.g. WiiU with 53K+ files).
        """
        rom_id_str = str(int(rom_id))

        cached = self._metadata_cache.get(rom_id_str)
        if isinstance(cached, dict) and cached:
            self._log_debug(f"Metadata cache hit for rom_id={rom_id_str}")
            return cached

        self._log_debug(f"Metadata cache miss for rom_id={rom_id_str}, will refresh on next sync")
        return asdict(
            RomMetadata(
                summary="",
                genres=(),
                companies=(),
                first_release_date=None,
                average_rating=None,
                game_modes=(),
                player_count="",
                cached_at=0.0,
                steam_categories=(),
            )
        )

    def get_all_metadata_cache(self):
        """Return the full metadata cache dict for frontend to load on plugin start."""
        return self._metadata_cache

    def get_app_id_rom_id_map(self):
        """Return {app_id: rom_id} mapping from shortcut_registry for frontend lookup."""
        result = {}
        for rom_id, entry in self._state["shortcut_registry"].items():
            app_id = entry.get("app_id")
            if app_id is not None:
                result[str(app_id)] = int(rom_id)
        return result
