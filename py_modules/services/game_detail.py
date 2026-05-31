"""GameDetailService — game detail page data aggregation.

Aggregates the synced-ROM registry, install record, cached save-sync state,
firmware cache, cached ROM metadata, and achievement progress into a single
response payload for the frontend game detail page. Reads the relational state
from SQLite through the Unit of Work; the platform display name comes from the
offline ``kv_config`` cache (not stored on the ROM — see ADR-0003). Cross-service
reads (BIOS, achievements) go through callback-injected Protocols so the service
stays independent of other service modules.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, cast

from models.metadata import AchievementSummary
from models.state import MetadataCacheEntry

from domain.bios import compute_bios_label, compute_bios_level, format_bios_status
from domain.platform_names import decode_platform_names
from domain.save_status import compute_save_sync_display

if TYPE_CHECKING:
    import logging

    from domain.rom import Rom
    from domain.rom_install import RomInstall
    from domain.rom_save_state import RomSaveState
    from services.protocols import AchievementsReader, BiosChecker, Clock, UnitOfWorkFactory

# kv_config key for the offline ``platform_slug → display_name`` cache the library
# sync refreshes every run. Read here so the game-detail panel shows "Super
# Nintendo" rather than the bare "snes" slug. Mirrors
# ``library.reporter._PLATFORM_NAMES_KEY``.
_PLATFORM_NAMES_KEY = "platform_names"

METADATA_TTL_SEC = 7 * 24 * 3600  # 7 days
BIOS_TTL_SEC = 3600  # 1 hour
ACHIEVEMENT_TTL_SEC = 3600  # 1 hour


@dataclass(frozen=True)
class GameDetailServiceConfig:
    """Frozen wiring bundle handed to ``GameDetailService.__init__``.

    Holds the live settings dict, runtime infrastructure, the clock seam, the
    SQLite Unit-of-Work factory (the read seam over the ``roms`` /
    ``rom_installs`` / ``rom_save_states`` / ``rom_metadata`` / ``kv_config``
    aggregates), and the Protocol-typed reader adapters (``BiosChecker``,
    ``AchievementsReader``) GameDetailService consults to assemble the
    game-detail payload.
    """

    settings: dict
    logger: logging.Logger
    clock: Clock
    uow_factory: UnitOfWorkFactory
    bios_checker: BiosChecker
    achievements: AchievementsReader


class GameDetailService:
    """Aggregates game detail page data from SQLite + cross-service readers."""

    def __init__(self, *, config: GameDetailServiceConfig) -> None:
        self._settings = config.settings
        self._logger = config.logger
        self._clock = config.clock
        self._uow_factory = config.uow_factory
        self._bios_checker = config.bios_checker
        self._achievements = config.achievements

    @staticmethod
    def _resolve_rom_file(install: RomInstall | None, rom: Rom) -> str:
        """ROM filename from the install record, falling back to ``Rom.fs_name``."""
        if install is not None and install.file_path:
            return os.path.basename(install.file_path)
        return rom.fs_name

    @staticmethod
    def _build_save_status(save_state: RomSaveState | None) -> dict | None:
        """Build cached save-sync status from the ROM's save state, or None."""
        if save_state is None:
            return None
        files_list = [
            {
                "filename": fn,
                "status": "synced" if fdata.last_sync_hash else "unknown",
                "last_sync_at": fdata.last_sync_at or None,
            }
            for fn, fdata in save_state.files.items()
        ]
        return {
            "files": files_list,
            "last_sync_check_at": save_state.last_sync_check_at,
            "conflicts": [],  # cached only — full conflicts via get_save_status()
        }

    def _build_achievement_summary(self, rom_id_str: str, ra_id: int | None) -> dict | None:
        """Build cached achievement summary for badge rendering, or None."""
        if not ra_id or not self._achievements.get_ra_username():
            return None
        cached_progress = self._achievements.get_progress_cache_entry(rom_id_str)
        if not cached_progress:
            return None
        return asdict(
            AchievementSummary(
                earned=cached_progress.get("earned", 0),
                total=cached_progress.get("total", 0),
                earned_hardcore=cached_progress.get("earned_hardcore", 0),
                cached_at=cached_progress.get("cached_at", 0.0),
            )
        )

    @staticmethod
    def _project_metadata(cached) -> MetadataCacheEntry | None:
        """Project the cached ``RomMetadata`` aggregate into the frontend entry shape.

        Returns the metadata entry (tuple fields flattened to list arrays,
        nullable date/rating preserved) or ``None`` on a cache miss — the
        ``None`` drives ``"metadata"`` into stale_fields so the frontend
        triggers a background refresh.
        """
        if cached is None:
            return None
        return cast(
            "MetadataCacheEntry",
            {
                "summary": cached.summary,
                "genres": list(cached.genres),
                "companies": list(cached.companies),
                "first_release_date": cached.first_release_date,
                "average_rating": cached.average_rating,
                "game_modes": list(cached.game_modes),
                "player_count": cached.player_count,
                "cached_at": cached.cached_at,
                "steam_categories": list(cached.steam_categories),
            },
        )

    @staticmethod
    def _compute_stale_fields(
        *,
        now: float,
        metadata: MetadataCacheEntry | None,
        bios_status: dict | None,
        platform_slug: str,
        ra_id: int | None,
        achievement_summary: dict | None,
    ) -> list[str]:
        """Return list of cache keys that are stale and need background refresh."""
        stale: list[str] = []

        meta_cached_at = metadata.get("cached_at", 0) if metadata else 0
        if not metadata or (now - meta_cached_at) > METADATA_TTL_SEC:
            stale.append("metadata")

        if bios_status is not None:
            if (now - bios_status.get("cached_at", 0)) > BIOS_TTL_SEC:
                stale.append("bios")
        elif platform_slug:
            stale.append("bios")

        if ra_id:
            if achievement_summary:
                if (now - achievement_summary.get("cached_at", 0)) > ACHIEVEMENT_TTL_SEC:
                    stale.append("achievements")
            else:
                stale.append("achievements")

        return stale

    def get_cached_game_detail(self, app_id) -> dict:
        """Return cached data for a game keyed by its Steam ``app_id``."""
        app_id = int(app_id)

        # ── Single unified read UoW (ADR-0006): one transaction reads the ROM,
        # its install/save-state/metadata children, and the platform-name cache.
        # Capture locals, close the UoW, then build the response and call the
        # bios_checker (HTTP-free cache read) entirely outside the transaction —
        # no I/O of any kind runs inside the ``with`` block.
        with self._uow_factory() as uow:
            rom = uow.roms.get_by_app_id(app_id)
            if rom is None:
                return {"found": False}
            rom_id = rom.rom_id
            install = uow.rom_installs.get(rom_id)
            save_state = uow.rom_save_states.get(rom_id)
            metadata_raw = uow.rom_metadata.get(rom_id)
            platform_names = decode_platform_names(uow.kv_config.get(_PLATFORM_NAMES_KEY))

        rom_id_str = str(rom_id)
        platform_slug = rom.platform_slug
        ra_id = rom.ra_id

        installed = install is not None
        rom_file = self._resolve_rom_file(install, rom)

        # Save sync
        save_sync_enabled = bool(self._settings.get("save_sync_enabled", False))
        save_status = self._build_save_status(save_state)
        save_sync_display = None
        if save_status is not None:
            save_sync_display = asdict(
                compute_save_sync_display(
                    save_status["files"],
                    save_status.get("last_sync_check_at"),
                )
            )

        metadata = self._project_metadata(metadata_raw)

        # Platform display name from the offline cache, degrading to the slug.
        platform_name = platform_names.get(platform_slug, platform_slug) if platform_slug else ""

        # BIOS status from firmware cache (no HTTP — cache-only read)
        bios_status = None
        bios_level = None
        bios_label = None
        if platform_slug:
            cached_bios = self._bios_checker.check_platform_bios_cached(platform_slug, rom_filename=rom_file or None)
            if cached_bios and cached_bios.get("needs_bios"):
                bios_obj = format_bios_status(cached_bios, platform_slug, cached_at=cached_bios.get("cached_at", 0.0))
                bios_status = asdict(bios_obj)
                bios_level = compute_bios_level(bios_obj)
                bios_label = compute_bios_label(bios_obj)

        # Achievement summary (for badge rendering)
        achievement_summary = self._build_achievement_summary(rom_id_str, ra_id)

        stale_fields = self._compute_stale_fields(
            now=self._clock.time(),
            metadata=metadata,
            bios_status=bios_status,
            platform_slug=platform_slug,
            ra_id=ra_id,
            achievement_summary=achievement_summary,
        )

        return {
            "found": True,
            "rom_id": rom_id,
            "rom_name": rom.name,
            "platform_slug": platform_slug,
            "platform_name": platform_name,
            "installed": installed,
            "save_sync_enabled": save_sync_enabled,
            "save_status": save_status,
            "save_sync_display": save_sync_display,
            "metadata": metadata,
            "bios_status": bios_status,
            "bios_level": bios_level,
            "bios_label": bios_label,
            "rom_file": rom_file,
            "ra_id": ra_id,
            "achievement_summary": achievement_summary,
            "stale_fields": stale_fields,
        }

    async def get_bios_status(self, rom_id) -> dict:
        """Return BIOS status for a ROM by looking up platform/rom_file from SQLite.

        Response always includes ``bios_status`` (dict or ``None``), ``bios_level``
        (``"ok"`` / ``"partial"`` / ``"missing"`` or ``None``), and ``bios_label``
        (str or ``None``). The pre-computed level + label match what the cached
        ``get_cached_game_detail`` ships so the frontend never re-derives them.
        """
        rom_id = int(rom_id)

        # Short read UoW: resolve platform_slug + rom_file, then await the BIOS
        # check OUTSIDE the transaction (ADR-0006 — no network I/O in the UoW).
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            install = uow.rom_installs.get(rom_id) if rom is not None else None

        if rom is None:
            return {"bios_status": None, "bios_level": None, "bios_label": None}

        platform_slug = rom.platform_slug
        if not platform_slug:
            return {"bios_status": None, "bios_level": None, "bios_label": None}

        rom_file = self._resolve_rom_file(install, rom)

        try:
            bios = await self._bios_checker.check_platform_bios(platform_slug, rom_filename=rom_file or None)
            if bios.get("needs_bios"):
                bios_obj = format_bios_status(bios, platform_slug)
                return {
                    "bios_status": asdict(bios_obj),
                    "bios_level": compute_bios_level(bios_obj),
                    "bios_label": compute_bios_label(bios_obj),
                }
        except Exception as e:
            self._logger.warning(f"BIOS status check failed for {platform_slug}: {e}")

        return {"bios_status": None, "bios_level": None, "bios_label": None}
