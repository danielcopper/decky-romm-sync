"""MetadataService — ROM metadata cache ownership.

Owns the in-memory ROM metadata cache: the canonical shape, the
``app_id -> rom_id`` mapping the launcher uses to resolve session
ROMs, and the periodic-flush policy that lands the cache on disk via
``MetadataCachePersister``. The cache is populated from RomM list
responses; ad-hoc detail HTTP calls are not this service's concern.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, cast

from models.metadata import RomMetadata
from models.metadata_patches import MetadataStampPatch
from models.state import MetadataCache, MetadataCacheEntry, PluginState

from domain.steam_categories import build_steam_categories

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import Clock, DebugLogger, MetadataCachePersister, MetadataCacheStore


@dataclass(frozen=True)
class MetadataServiceConfig:
    """Frozen wiring bundle handed to ``MetadataService.__init__``.

    Holds the live state and metadata cache dicts, runtime
    infrastructure, persistence callback, and the clock/debug-logger
    seams MetadataService needs at construction time.
    """

    state: PluginState
    metadata_cache: MetadataCache
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    metadata_cache_persister: MetadataCachePersister
    metadata_store: MetadataCacheStore
    log_debug: DebugLogger


class MetadataService:
    """ROM metadata cache: extract, store, flush, and fetch on demand."""

    def __init__(self, *, config: MetadataServiceConfig) -> None:
        self._state = config.state
        self._metadata_cache = config.metadata_cache
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._metadata_cache_persister = config.metadata_cache_persister
        self._metadata_store = config.metadata_store
        self._log_debug = config.log_debug

        self._metadata_dirty_count = 0
        self._METADATA_FLUSH_INTERVAL = 50

    def extract_metadata(self, rom: dict) -> MetadataCacheEntry:
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
        return cast(
            "MetadataCacheEntry",
            asdict(
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
            ),
        )

    def mark_metadata_dirty(self):
        """Track metadata cache changes and flush to disk periodically."""
        self._metadata_dirty_count += 1
        if self._metadata_dirty_count >= self._METADATA_FLUSH_INTERVAL:
            self._metadata_cache_persister.save_metadata()
            self._metadata_dirty_count = 0

    def flush_metadata_if_dirty(self):
        """Flush metadata cache to disk if any pending writes."""
        if self._metadata_dirty_count > 0:
            self._metadata_cache_persister.save_metadata()
            self._metadata_dirty_count = 0

    def record_unit_metadata(self, roms: list[dict]) -> None:
        """Stamp metadata cache for the ROMs of one applied unit.

        Called after the frontend has confirmed shortcut application for
        a unit, and only for ROMs that carry fresh metadata. Defensively
        skips ROMs without a ``metadatum`` field — registry-reconstructed
        thin ROMs from the per-unit incremental-skip path must never
        reach the extract path (the orchestrator gates them out via the
        skip-marker), but this guard prevents accidental cache erasure
        should that contract be broken in the future.
        """
        for rom in roms:
            if not rom.get("metadatum"):
                continue
            rom_id_str = str(rom["id"])
            self._metadata_store.apply_stamp(
                MetadataStampPatch(rom_id_str=rom_id_str, entry=self.extract_metadata(rom))
            )
            self.mark_metadata_dirty()
        self.flush_metadata_if_dirty()

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
