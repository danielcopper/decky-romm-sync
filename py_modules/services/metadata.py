"""MetadataService — ROM metadata read surface.

Owns the frontend-facing reads of cached ROM metadata: the per-ROM
``get_rom_metadata`` lookup, the full ``get_all_metadata_cache`` dump
the frontend loads on plugin start, and the ``app_id -> rom_id`` mapping
the launcher uses to resolve session ROMs. Cached metadata is persisted
by the library sync (the per-unit ``roms`` + ``rom_metadata`` commit);
this service only reads it back. Ad-hoc detail HTTP calls are not this
service's concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.rom_metadata import RomMetadata
    from services.protocols import (
        DebugLogger,
        UnitOfWorkFactory,
    )


def _empty_metadata_entry() -> dict[str, Any]:
    """Build the empty-default metadata entry returned on a cache miss.

    Array fields are empty lists (not tuples) to match the ``RomMetadata``
    wire shape; date/rating are nullable; ``cached_at`` is ``0.0`` so the
    frontend treats the entry as stale.
    """
    return {
        "summary": "",
        "genres": [],
        "companies": [],
        "first_release_date": None,
        "average_rating": None,
        "game_modes": [],
        "player_count": "",
        "cached_at": 0.0,
        "steam_categories": [],
    }


def _metadata_to_entry(metadata: RomMetadata) -> dict[str, Any]:
    """Map a ``rom_metadata`` aggregate to the frontend wire entry.

    Tuple fields flatten to lists to match the ``RomMetadata`` interface
    (``genres`` / ``companies`` / ``game_modes`` / ``steam_categories``
    are JS arrays); ``first_release_date`` / ``average_rating`` stay
    nullable.
    """
    return {
        "summary": metadata.summary,
        "genres": list(metadata.genres),
        "companies": list(metadata.companies),
        "first_release_date": metadata.first_release_date,
        "average_rating": metadata.average_rating,
        "game_modes": list(metadata.game_modes),
        "player_count": metadata.player_count,
        "cached_at": metadata.cached_at,
        "steam_categories": list(metadata.steam_categories),
    }


@dataclass(frozen=True)
class MetadataServiceConfig:
    """Frozen wiring bundle handed to ``MetadataService.__init__``.

    Holds the runtime infrastructure, the debug-logger seam, and the
    SQLite Unit-of-Work factory (the transactional seam over the
    ``rom_metadata`` / ``roms`` repositories MetadataService reads).
    """

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    log_debug: DebugLogger
    uow_factory: UnitOfWorkFactory


class MetadataService:
    """ROM metadata reads from the ``rom_metadata`` aggregate."""

    def __init__(self, *, config: MetadataServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._log_debug = config.log_debug
        self._uow_factory = config.uow_factory

    def get_rom_metadata(self, rom_id):
        """Return cached metadata for a ROM.

        Metadata is populated during sync via the per-unit commit. This
        method returns whatever is cached — stale or fresh — and never
        calls the detail API (GET /api/roms/{id}), which can timeout for
        ROMs with very large file lists (e.g. WiiU with 53K+ files). A
        cache miss returns the empty-default entry.
        """
        rid = int(rom_id)
        with self._uow_factory() as uow:
            cached = uow.rom_metadata.get(rid)
        if cached is not None:
            self._log_debug(f"Metadata cache hit for rom_id={rid}")
            return _metadata_to_entry(cached)

        self._log_debug(f"Metadata cache miss for rom_id={rid}, will refresh on next sync")
        return _empty_metadata_entry()

    def get_all_metadata_cache(self):
        """Return the full metadata cache dict for frontend to load on plugin start.

        Keyed by ``str(rom_id)``; each value is the frontend metadata
        entry (list-shaped array fields). Callable-only, so its own short
        read UoW is safe (no in-transaction caller).
        """
        with self._uow_factory() as uow:
            return {str(rom_id): _metadata_to_entry(metadata) for rom_id, metadata in uow.rom_metadata.iter_all()}

    def get_app_id_rom_id_map(self):
        """Return ``{str(app_id): rom_id}`` from the ``roms`` registry for frontend lookup.

        Rows with a NULL ``shortcut_app_id`` (unbound / stale) are
        excluded — they carry no Steam shortcut to map. Callable-only, so
        its own short read UoW is safe (no in-transaction caller).
        """
        with self._uow_factory() as uow:
            return {
                str(rom.shortcut_app_id): rom.rom_id for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None
            }
