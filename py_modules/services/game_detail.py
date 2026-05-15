"""GameDetailService — game detail page data aggregation.

Aggregates ROM registry data, save-sync state, firmware cache, metadata cache,
and achievement progress into a single response payload for the frontend game
detail page.  Uses callback injection (not direct service references) to stay
independent of other service modules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from models.metadata import AchievementSummary

from domain.bios import compute_bios_label, compute_bios_level, format_bios_status
from domain.save_state import SaveSyncState
from domain.save_status import compute_save_sync_display

if TYPE_CHECKING:
    import logging

    from services.protocols import AchievementsReader, BiosChecker, Clock

METADATA_TTL_SEC = 7 * 24 * 3600  # 7 days
BIOS_TTL_SEC = 3600  # 1 hour
ACHIEVEMENT_TTL_SEC = 3600  # 1 hour


@dataclass(frozen=True)
class GameDetailServiceConfig:
    """Frozen wiring bundle handed to ``GameDetailService.__init__``.

    Holds the live state and metadata cache dicts, the typed save-sync
    aggregate, runtime infrastructure, clock seam, and the Protocol-
    typed reader adapters (``BiosChecker``, ``AchievementsReader``)
    GameDetailService consults to assemble the game-detail payload.
    """

    state: dict
    metadata_cache: dict
    save_sync_state: SaveSyncState
    logger: logging.Logger
    clock: Clock
    bios_checker: BiosChecker
    achievements: AchievementsReader


class GameDetailService:
    """Aggregates game detail page data from multiple state sources."""

    def __init__(self, *, config: GameDetailServiceConfig) -> None:
        self._state = config.state
        self._metadata_cache = config.metadata_cache
        self._save_sync_state = config.save_sync_state
        self._logger = config.logger
        self._clock = config.clock
        self._bios_checker = config.bios_checker
        self._achievements = config.achievements

    def _resolve_rom_by_app_id(self, app_id: int) -> tuple[int | None, dict | None]:
        """Reverse lookup: find rom_id by app_id in shortcut_registry."""
        for rid, reg in self._state["shortcut_registry"].items():
            if reg.get("app_id") == app_id:
                return int(rid), reg
        return None, None

    def _resolve_rom_file(self, rom_id_str: str, entry: dict) -> str:
        """ROM filename from installed_roms or registry fs_name fallback."""
        rom_file = ""
        installed_rom = self._state["installed_roms"].get(rom_id_str, {})
        if installed_rom:
            rom_file = installed_rom.get("file_name", "")
        if not rom_file:
            rom_file = entry.get("fs_name", "")
        return rom_file

    def _build_save_status(self, rom_id_str: str) -> dict | None:
        """Build cached save-sync status for a ROM, or None if no saves."""
        rom_state = self._save_sync_state.saves.get(rom_id_str)
        if rom_state is None:
            return None
        files_list = [
            {
                "filename": fn,
                "status": "synced" if fdata.last_sync_hash else "unknown",
                "last_sync_at": fdata.last_sync_at or None,
            }
            for fn, fdata in rom_state.files.items()
        ]
        return {
            "files": files_list,
            "last_sync_check_at": rom_state.last_sync_check_at,
            "conflicts": [],  # cached only — full conflicts via get_save_status()
        }

    def _build_achievement_summary(self, rom_id_str: str, ra_id) -> dict | None:
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
    def _compute_stale_fields(
        *,
        now: float,
        metadata: dict | None,
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
        """Return cached data for a game."""
        app_id = int(app_id)

        # Reverse lookup: find rom_id by app_id in shortcut_registry
        rom_id, entry = self._resolve_rom_by_app_id(app_id)

        if rom_id is None or entry is None:
            return {"found": False}

        rom_id_str = str(rom_id)

        # Installed status
        installed = rom_id_str in self._state["installed_roms"]

        # Save sync
        save_sync_enabled = self._save_sync_state.settings.save_sync_enabled
        save_status = self._build_save_status(rom_id_str)
        save_sync_display = None
        if save_status is not None:
            save_sync_display = compute_save_sync_display(
                save_status["files"],
                save_status.get("last_sync_check_at"),
            )

        # Metadata from cache
        metadata = self._metadata_cache.get(rom_id_str)

        # ROM file name for per-game core overrides
        # Prefer installed_roms (set during download), fall back to registry (set during sync)
        rom_file = self._resolve_rom_file(rom_id_str, entry)

        platform_slug = entry.get("platform_slug", "")

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
        ra_id = entry.get("ra_id")
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
            "rom_name": entry.get("name", ""),
            "platform_slug": platform_slug,
            "platform_name": entry.get("platform_name", ""),
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
        """Return BIOS status for a ROM by looking up platform/rom_file from registry."""
        rom_id_str = str(rom_id)
        entry = self._state["shortcut_registry"].get(rom_id_str)
        if not entry:
            return {"bios_status": None}

        platform_slug = entry.get("platform_slug", "")
        if not platform_slug:
            return {"bios_status": None}

        # Resolve rom_file for per-game core override detection
        rom_file = self._resolve_rom_file(rom_id_str, entry)

        try:
            bios = await self._bios_checker.check_platform_bios(platform_slug, rom_filename=rom_file or None)
            if bios.get("needs_bios"):
                return {"bios_status": asdict(format_bios_status(bios, platform_slug))}
        except Exception as e:
            self._logger.warning(f"BIOS status check failed for {platform_slug}: {e}")

        return {"bios_status": None}
