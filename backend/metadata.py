import time
import json
import ssl
import urllib.request
import urllib.error
from typing import TYPE_CHECKING, Any

import decky

if TYPE_CHECKING:
    import asyncio
    from typing import Protocol

    class _MetadataDeps(Protocol):
        _metadata_cache: dict
        _state: dict
        loop: asyncio.AbstractEventLoop
        def _log_debug(self, msg: str) -> None: ...
        def _romm_request(self, path: str) -> Any: ...
        def _save_metadata_cache(self) -> None: ...


class MetadataMixin:
    def _extract_metadata(self, rom):
        """Extract metadata fields from a ROM dict into cache format."""
        metadatum = rom.get("metadatum") or {}
        first_release_date = metadatum.get("first_release_date")
        if first_release_date is not None:
            first_release_date = int(first_release_date) // 1000
        average_rating = metadatum.get("average_rating")
        if average_rating is not None:
            average_rating = float(average_rating)
        return {
            "summary": rom.get("summary", "") or "",
            "genres": metadatum.get("genres") or [],
            "companies": metadatum.get("companies") or [],
            "first_release_date": first_release_date,
            "average_rating": average_rating,
            "game_modes": metadatum.get("game_modes") or [],
            "player_count": metadatum.get("player_count", "") or "",
            "cached_at": time.time(),
        }

    async def get_rom_metadata(self, rom_id):
        """Return cached metadata for a ROM, fetching from API if stale/missing."""
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        CACHE_TTL = 7 * 24 * 3600  # 7 days

        cached = self._metadata_cache.get(rom_id_str)
        if cached:
            age = time.time() - cached.get("cached_at", 0)
            if age < CACHE_TTL:
                self._log_debug(f"Metadata cache hit for rom_id={rom_id}")
                return cached

        # Cache miss or stale — fetch from RomM API
        self._log_debug(f"Metadata cache miss for rom_id={rom_id}, fetching from API")
        try:
            rom_data = await self.loop.run_in_executor(
                None, self._romm_request, f"/api/roms/{rom_id}"
            )
            metadata = self._extract_metadata(rom_data)
            self._metadata_cache[rom_id_str] = metadata
            self._save_metadata_cache()
            return metadata
        except Exception as e:
            decky.logger.warning(f"Failed to fetch metadata for rom_id={rom_id}: {e}")
            # Return stale cache if available
            if cached:
                return cached
            return {
                "summary": "",
                "genres": [],
                "companies": [],
                "first_release_date": None,
                "average_rating": None,
                "game_modes": [],
                "player_count": "",
                "cached_at": 0,
            }

    async def get_all_metadata_cache(self):
        """Return the full metadata cache dict for frontend to load on plugin start."""
        return self._metadata_cache

    async def get_app_id_rom_id_map(self):
        """Return {app_id: rom_id} mapping from shortcut_registry for frontend lookup."""
        result = {}
        for rom_id, entry in self._state["shortcut_registry"].items():
            app_id = entry.get("app_id")
            if app_id is not None:
                result[str(app_id)] = int(rom_id)
        return result
