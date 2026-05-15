"""SteamGridDB orchestration — API key flow, artwork fetch/cache, icon save.

Owns the runtime decisions for SteamGridDB integration: resolving SGDB
game IDs from registry/RomM hints, fanning out cached vs. remote
artwork requests, and routing icon writes into Steam's grid directory.
All raw I/O is delegated to adapters (``SgdbArtworkCache``,
``SteamConfigAdapter``); pure asset-type / endpoint compute lives in
``domain.sgdb_artwork``.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.sgdb_artwork import (
    asset_type_endpoint,
    asset_type_name,
    sgdb_endpoint_path,
    to_signed_app_id,
)
from lib.errors import SgdbApiError, SteamGridDirMissingError

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        DebugLogger,
        PendingSyncReader,
        RommApiProtocol,
        SettingsPersister,
        SgdbArtworkCache,
        StatePersister,
        SteamConfigAdapter,
        SteamGridDbApi,
    )


@dataclass(frozen=True)
class SteamGridConfig:
    """Frozen wiring bundle handed to ``SteamGridService.__init__``.

    Holds the Protocol-typed adapters (``sgdb_api``, ``romm_api``,
    ``steam_config``, ``sgdb_artwork_cache``), the live state and
    settings dicts, runtime infrastructure, persistence callbacks, the
    pending-sync read seam, and the debug-logger seam SteamGridService
    needs at construction time.

    Note
    ----
    The dataclass is named ``SteamGridConfig`` rather than
    ``SteamGridServiceConfig`` for transitional reasons; renaming is
    tracked separately and does not affect the Always-Config shape.
    """

    sgdb_api: SteamGridDbApi
    romm_api: RommApiProtocol
    steam_config: SteamConfigAdapter
    sgdb_artwork_cache: SgdbArtworkCache
    state: dict
    settings: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    save_state: StatePersister
    save_settings_to_disk: SettingsPersister
    get_pending_sync: PendingSyncReader
    log_debug: DebugLogger


class SteamGridService:
    """SteamGridDB orchestration: API key flow, artwork fetch/cache, icon save."""

    def __init__(self, *, config: SteamGridConfig) -> None:
        self._sgdb_api = config.sgdb_api
        self._romm_api = config.romm_api
        self._steam_config = config.steam_config
        self._sgdb_artwork_cache = config.sgdb_artwork_cache
        self._state = config.state
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._save_state = config.save_state
        self._save_settings_to_disk = config.save_settings_to_disk
        self._get_pending_sync = config.get_pending_sync
        self._log_debug = config.log_debug

    # -- SGDB lookup -------------------------------------------------------

    def _get_sgdb_game_id(self, igdb_id):
        try:
            result = self._sgdb_api.request(f"/games/igdb/{igdb_id}")
            if result and result.get("success") and result.get("data"):
                return result["data"]["id"]
        except Exception as e:
            self._logger.warning(f"SGDB lookup failed for IGDB {igdb_id}: {e}")
        return None

    # -- artwork download --------------------------------------------------

    def _download_sgdb_artwork(self, sgdb_game_id, rom_id, asset_type):
        if asset_type_endpoint(asset_type) is None:
            return None

        art_dir = self._sgdb_artwork_cache.cache_dir()
        cached = os.path.join(art_dir, f"{rom_id}_{asset_type}.png")
        if self._sgdb_artwork_cache.exists(cached):
            return cached

        path = sgdb_endpoint_path(asset_type, sgdb_game_id)
        if path is None:
            return None

        try:
            result = self._sgdb_api.request(path)
            if not result or not result.get("success") or not result.get("data"):
                return None
            image_url = result["data"][0]["url"]
            success = self._sgdb_api.download_image(image_url, cached)
            return cached if success else None
        except Exception as e:
            self._logger.warning(f"SGDB {asset_type} download failed for game {sgdb_game_id}: {e}")
            return None

    # -- artwork base64 (callable) -----------------------------------------

    async def _read_file_as_base64(self, path):
        """Read a file and return base64-encoded string, or None on failure."""
        try:
            data = await self._loop.run_in_executor(None, self._sgdb_artwork_cache.read_bytes, path)
            return base64.b64encode(data).decode("ascii")
        except Exception as e:
            self._logger.warning(f"Failed to read file {path}: {e}")
            return None

    async def _resolve_sgdb_id(self, rom_id):
        """Resolve SGDB game ID from registry, pending sync, RomM API, or IGDB lookup."""
        rom_id_str = str(rom_id)
        reg = self._state["shortcut_registry"].get(rom_id_str, {})
        sgdb_id = reg.get("sgdb_id")
        igdb_id = reg.get("igdb_id")

        if not sgdb_id:
            pending = self._get_pending_sync().get(rom_id, {})
            sgdb_id = pending.get("sgdb_id")
            igdb_id = igdb_id or pending.get("igdb_id")

        # On-demand fetch from RomM API for pre-existing ROMs missing IDs
        if not sgdb_id:
            sgdb_id, igdb_id = await self._fetch_ids_from_romm(rom_id, igdb_id)

        # Fallback: look up SGDB via IGDB ID
        if not sgdb_id and igdb_id:
            sgdb_id = await self._loop.run_in_executor(None, self._get_sgdb_game_id, igdb_id)
            if sgdb_id and rom_id_str in self._state["shortcut_registry"]:
                self._state["shortcut_registry"][rom_id_str]["sgdb_id"] = sgdb_id
                self._save_state()

        return sgdb_id

    async def _fetch_ids_from_romm(self, rom_id, igdb_id):
        """Fetch sgdb_id and igdb_id from RomM API and update registry."""
        rom_id_str = str(rom_id)
        sgdb_id = None
        try:
            rom_data = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
            if rom_data:
                sgdb_id = rom_data.get("sgdb_id")
                igdb_id = igdb_id or rom_data.get("igdb_id")
            self._log_debug(f"SGDB artwork: fetched sgdb_id={sgdb_id}, igdb_id={igdb_id} from RomM for rom_id={rom_id}")
            if rom_id_str in self._state["shortcut_registry"]:
                if sgdb_id:
                    self._state["shortcut_registry"][rom_id_str]["sgdb_id"] = sgdb_id
                if igdb_id:
                    self._state["shortcut_registry"][rom_id_str]["igdb_id"] = igdb_id
                self._save_state()
        except Exception as e:
            self._logger.warning(f"SGDB artwork: failed to fetch IDs from RomM for rom_id={rom_id}: {e}")
        return sgdb_id, igdb_id

    async def get_sgdb_artwork_base64(self, rom_id, asset_type_num):
        rom_id = int(rom_id)
        asset_type_num = int(asset_type_num)
        asset_type = asset_type_name(asset_type_num)
        self._log_debug(f"SGDB artwork request: rom_id={rom_id}, asset_type={asset_type_num}")
        if not asset_type:
            return {"base64": None, "no_api_key": False}

        art_dir = self._sgdb_artwork_cache.cache_dir()
        cached = os.path.join(art_dir, f"{rom_id}_{asset_type}.png")

        # Return from cache if available
        if self._sgdb_artwork_cache.exists(cached):
            self._log_debug(f"SGDB artwork cache hit: {cached}")
            b64 = await self._read_file_as_base64(cached)
            if b64:
                return {"base64": b64, "no_api_key": False}

        if not self._settings.get("steamgriddb_api_key"):
            self._log_debug("SGDB artwork skipped: no API key configured")
            return {"base64": None, "no_api_key": True}

        sgdb_id = await self._resolve_sgdb_id(rom_id)
        if not sgdb_id:
            self._log_debug(f"SGDB artwork skipped: no SGDB game found for rom_id={rom_id}")
            return {"base64": None, "no_api_key": False}

        path = await self._loop.run_in_executor(None, self._download_sgdb_artwork, sgdb_id, rom_id, asset_type)
        if path and self._sgdb_artwork_cache.exists(path):
            self._log_debug(f"SGDB artwork download success: rom_id={rom_id}, asset_type={asset_type}")
            b64 = await self._read_file_as_base64(path)
            if b64:
                return {"base64": b64, "no_api_key": False}
        else:
            self._log_debug(f"SGDB artwork download failed: rom_id={rom_id}, asset_type={asset_type}")

        return {"base64": None, "no_api_key": False}

    # -- API key management ------------------------------------------------

    async def verify_sgdb_api_key(self, api_key=None):
        # Use saved key if no valid key provided (modal pattern doesn't hold the real key)
        if not api_key or api_key == "••••":
            api_key = self._settings.get("steamgriddb_api_key", "")
        if not api_key:
            return {"success": False, "message": "No API key configured"}
        try:
            data = await self._loop.run_in_executor(None, self._sgdb_api.verify_api_key, api_key)
            if data.get("success"):
                return {"success": True, "message": "API key is valid"}
            return {"success": False, "message": "API key rejected by SteamGridDB"}
        except SgdbApiError as e:
            self._logger.warning(f"SGDB API key verification HTTP error: {e.status_code}")
            if e.status_code in (401, 403):
                return {"success": False, "message": "Invalid API key"}
            return {"success": False, "message": f"SteamGridDB error: HTTP {e.status_code}"}
        except Exception as e:
            self._logger.error(f"SGDB API key verification failed: {e}")
            return {"success": False, "message": f"Connection failed: {e}"}

    def save_sgdb_api_key(self, api_key):
        if api_key and api_key != "••••":
            self._settings["steamgriddb_api_key"] = api_key
            self._save_settings_to_disk()
        return {"success": True, "message": "SteamGridDB API key saved"}

    # -- cache pruning -----------------------------------------------------

    def prune_orphaned_artwork_cache(self):
        """Remove SGDB artwork cache files for rom_ids not in the shortcut registry."""
        art_dir = self._sgdb_artwork_cache.cache_dir()
        if not self._sgdb_artwork_cache.isdir(art_dir):
            return
        registry = self._state.get("shortcut_registry", {})
        pruned = 0
        for filename in self._sgdb_artwork_cache.listdir(art_dir):
            # Always remove leftover .tmp files
            if filename.endswith(".tmp"):
                try:
                    self._sgdb_artwork_cache.remove(os.path.join(art_dir, filename))
                    pruned += 1
                    self._logger.info(f"Removed leftover artwork tmp: {filename}")
                except OSError as e:
                    self._logger.warning(f"Failed to remove artwork tmp {filename}: {e}")
                continue
            # Expected format: {rom_id}_{type}.png
            parts = filename.split("_", 1)
            if not parts:
                continue
            rom_id = parts[0]
            if rom_id not in registry:
                try:
                    self._sgdb_artwork_cache.remove(os.path.join(art_dir, filename))
                    pruned += 1
                except OSError as e:
                    self._logger.warning(f"Failed to remove orphaned artwork {filename}: {e}")
        if pruned:
            self._logger.info(f"Pruned {pruned} orphaned SGDB artwork cache file(s)")

    # -- icon saving -------------------------------------------------------

    def _save_icon_to_grid(self, app_id, icon_bytes):
        """Write icon PNG to Steam's grid dir and update shortcuts.vdf icon field."""
        try:
            icon_path = self._steam_config.write_shortcut_icon(app_id, icon_bytes)
        except SteamGridDirMissingError as e:
            self._logger.warning(f"Cannot save icon: {e}")
            return False
        except Exception as e:
            self._logger.error(f"Failed to write icon file for app_id {app_id}: {e}")
            return False

        # Update shortcuts.vdf icon field
        try:
            vdf_data = self._steam_config.read_shortcuts()
            signed_id = to_signed_app_id(app_id)
            shortcuts = vdf_data.get("shortcuts", {})
            for entry in shortcuts.values():
                if entry.get("appid") == signed_id:
                    entry["icon"] = icon_path
                    break
            self._steam_config.write_shortcuts(vdf_data)
        except Exception as e:
            self._logger.warning(f"Failed to update shortcuts.vdf icon field: {e}")
            # Icon file is still saved, just VDF field not set — non-fatal

        return True

    async def save_shortcut_icon(self, app_id, icon_base64):
        """Save icon PNG to Steam grid dir and update VDF. Called from frontend."""
        app_id = int(app_id)
        try:
            icon_bytes = base64.b64decode(icon_base64)
        except Exception as e:
            self._logger.error(f"Failed to decode icon base64: {e}")
            return {"success": False}

        success = await self._loop.run_in_executor(None, self._save_icon_to_grid, app_id, icon_bytes)
        return {"success": success}
