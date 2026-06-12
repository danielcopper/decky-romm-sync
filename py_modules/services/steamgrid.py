"""SteamGridDB orchestration — API key flow, artwork fetch/cache, icon save.

Owns the runtime decisions for SteamGridDB integration: resolving SGDB
game IDs from registry/RomM hints, fanning out cached vs. remote
artwork requests, and routing icon writes into Steam's grid directory.
All raw I/O is delegated to adapters (``SgdbArtworkCache``,
``SteamConfigStore``); pure asset-type / endpoint compute lives in
``domain.sgdb_artwork``.
"""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.sgdb_artwork import (
    asset_type_endpoint,
    asset_type_name,
    build_autocomplete_path,
    classify_resolution,
    first_grid_url,
    parse_autocomplete_results,
    sgdb_endpoint_path,
    to_signed_app_id,
)
from lib.errors import SgdbApiError, SteamGridDirMissingError
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        DebugLogger,
        PendingSyncReader,
        RommRomReader,
        SettingsPersister,
        SgdbArtworkCache,
        SteamConfigStore,
        SteamGridDbApi,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class SteamGridServiceConfig:
    """Frozen wiring bundle handed to ``SteamGridService.__init__``.

    Holds the Protocol-typed adapters (``sgdb_api``, ``romm_api``,
    ``steam_config``, ``sgdb_artwork_cache``), the live settings dict,
    runtime infrastructure, the ``settings.json`` persister, the SQLite
    Unit-of-Work factory (the ``sgdb_id`` cross-ref is persisted onto the
    ``roms`` aggregate via the UoW), the pending-sync read seam, and the
    debug-logger seam SteamGridService needs at construction time.
    """

    sgdb_api: SteamGridDbApi
    romm_api: RommRomReader
    steam_config: SteamConfigStore
    sgdb_artwork_cache: SgdbArtworkCache
    settings: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    settings_persister: SettingsPersister
    get_pending_sync: PendingSyncReader
    log_debug: DebugLogger
    uow_factory: UnitOfWorkFactory


class SteamGridService:
    """SteamGridDB orchestration: API key flow, artwork fetch/cache, icon save."""

    def __init__(self, *, config: SteamGridServiceConfig) -> None:
        self._sgdb_api = config.sgdb_api
        self._romm_api = config.romm_api
        self._steam_config = config.steam_config
        self._sgdb_artwork_cache = config.sgdb_artwork_cache
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._settings_persister = config.settings_persister
        self._get_pending_sync = config.get_pending_sync
        self._log_debug = config.log_debug
        self._uow_factory = config.uow_factory

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

    def _resolve_sgdb_id_state_only(self, rom_id):
        """Resolve a SGDB game ID from local state only — no network.

        Checks the persisted ROM row first, then the in-memory
        pending-sync map. Returns ``None`` when neither carries a
        ``sgdb_id``. RomM re-reads and IGDB cross-refs are gated behind
        the explicit ``get_sgdb_resolution`` user action, not this
        passive lookup.
        """
        rom_id = int(rom_id)
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
        sgdb_id = rom.sgdb_id if rom is not None else None
        if not sgdb_id:
            pending = self._get_pending_sync().get(rom_id, {})
            sgdb_id = pending.get("sgdb_id")
        return sgdb_id

    async def _fetch_ids_from_romm(self, rom_id):
        """Fetch ``(sgdb_id, igdb_id, rom_data)`` from RomM without persisting.

        Reads the ROM detail from RomM and surfaces its ``sgdb_id`` /
        ``igdb_id`` fields. Persistence is the caller's decision — this
        method never writes the registry. Returns ``(None, None, None)``
        on a network failure (logged, never raised).
        """
        sgdb_id = None
        igdb_id = None
        rom_data = None
        try:
            rom_data = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
            if rom_data:
                sgdb_id = rom_data.get("sgdb_id")
                igdb_id = rom_data.get("igdb_id")
            self._log_debug(f"SGDB artwork: fetched sgdb_id={sgdb_id}, igdb_id={igdb_id} from RomM for rom_id={rom_id}")
        except Exception as e:
            self._logger.warning(f"SGDB artwork: failed to fetch IDs from RomM for rom_id={rom_id}: {e}")
        return sgdb_id, igdb_id, rom_data

    def _persist_sgdb_id(self, rom_id_str, sgdb_id):
        """Stamp a resolved ``sgdb_id`` on the ROM row in a short write UoW.

        No-op when the ROM is absent from ``roms`` — a resolved id only
        matters for a synced ROM, and the schema has no standalone row to
        stamp otherwise.
        """
        rom_id = int(rom_id_str)
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None:
                self._logger.warning(f"Cannot persist sgdb_id for unsynced rom_id={rom_id}")
                return
            rom.assign_sgdb_id(int(sgdb_id))
            uow.roms.save(rom)

    def _first_grid_thumb(self, sgdb_id):
        """Return a thumbnail URL for *sgdb_id*'s first grid, or ``None``.

        Synchronous — callers offload via ``run_in_executor``. Network
        and parse errors are swallowed to ``None`` so a preview thumbnail
        never breaks the resolution flow.
        """
        try:
            payload = self._sgdb_api.request(f"/grids/game/{sgdb_id}?limit=1")
            return first_grid_url(payload)
        except Exception as e:
            self._logger.warning(f"SGDB grid thumb lookup failed for game {sgdb_id}: {e}")
            return None

    # -- resolution cascade (callable) -------------------------------------

    async def get_sgdb_resolution(self, rom_id):
        """Resolve which SGDB game id to use for *rom_id*, picker-driven.

        The single explicit user action that may re-read RomM and run the
        IGDB cross-ref / name-search cascade. RomM's ``sgdb_id`` always
        wins when present. Returns one of:

        - ``{"decision": "no_api_key"}`` — no key configured.
        - ``{"decision": "resolved", "sgdb_id": int}`` — a winning id was
          found (and persisted when it came from RomM or IGDB).
        - ``{"decision": "needs_pick", "candidates": [...]}`` — nothing
          resolved automatically; offer a manual name-search picker.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        if not self._settings.get("steamgriddb_api_key"):
            return {"decision": "no_api_key"}

        state_id = self._resolve_sgdb_id_state_only(rom_id)
        romm_id, igdb_id, rom_data = await self._fetch_ids_from_romm(rom_id)

        decision = classify_resolution(state_id, romm_id)
        if decision == "use_romm" and romm_id is not None:
            if romm_id != state_id:
                self._persist_sgdb_id(rom_id_str, romm_id)
            return {"decision": "resolved", "sgdb_id": int(romm_id)}
        if decision == "use_state" and state_id is not None:
            return {"decision": "resolved", "sgdb_id": int(state_id)}

        # Unresolved: try IGDB cross-ref, then fall back to a name search.
        if igdb_id:
            resolved = await self._loop.run_in_executor(None, self._get_sgdb_game_id, igdb_id)
            if resolved:
                self._persist_sgdb_id(rom_id_str, resolved)
                return {"decision": "resolved", "sgdb_id": int(resolved)}

        name = (rom_data or {}).get("name") or ""
        search = await self.search_sgdb_games(name)
        return {"decision": "needs_pick", "candidates": search.get("games", [])}

    async def search_sgdb_games(self, term):
        """Search SGDB by name and enrich the top candidates with thumbnails.

        Returns ``{"success": bool, "games": [{"id", "name",
        "release_year", "thumb_url"}]}`` plus a ``reason`` slug on failure.
        Returns an empty, unsuccessful result (``reason="no_api_key"``) when
        no API key is configured. Network failures are logged and surfaced
        as ``{"success": False, "reason": "server_unreachable", "games":
        []}`` — the callable never raises.
        """
        if not self._settings.get("steamgriddb_api_key"):
            return {
                "success": False,
                "reason": "no_api_key",
                "message": "No SteamGridDB API key configured",
                "games": [],
            }
        try:
            path = build_autocomplete_path(str(term))
            payload = await self._loop.run_in_executor(None, self._sgdb_api.request, path)
            candidates = parse_autocomplete_results(payload)
        except Exception as e:
            self._logger.warning(f"SGDB name search failed for term={term!r}: {e}")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": f"SteamGridDB search failed: {e}",
                "games": [],
            }

        capped = candidates[:6]
        thumb_futures = [
            self._loop.run_in_executor(None, self._first_grid_thumb, candidate["id"]) for candidate in capped
        ]
        thumbs = await asyncio.gather(*thumb_futures)
        games = [
            {
                "id": candidate["id"],
                "name": candidate["name"],
                "release_year": candidate["release_year"],
                "thumb_url": thumb,
            }
            for candidate, thumb in zip(capped, thumbs, strict=True)
        ]
        return {"success": True, "games": games}

    async def apply_sgdb_game_id(self, rom_id, sgdb_id):
        """Paint a manually-picked game's artwork into *rom_id*'s cache.

        A manual pick only paints pixels — it downloads the chosen
        game's four asset types into the rom's artwork cache (which the
        cache-first ``get_sgdb_artwork_base64`` then serves onto the
        Steam shortcut) and persists **nothing**. Only authoritative ids
        (RomM ``sgdb_id`` or an IGDB cross-ref) are remembered as the
        resolved id; a manual pick is not. So a later "Refresh Artwork"
        on an otherwise-unresolvable rom stays ``needs_pick`` and reopens
        the picker, giving the user a free re-pick. The previously
        applied art stays visible until replaced.
        """
        rom_id = int(rom_id)
        sgdb_id = int(sgdb_id)

        # Start clean: ``_download_sgdb_artwork`` early-returns an
        # existing cache file, so a re-pick of a different game must
        # evict the prior PNGs before downloading.
        await self._loop.run_in_executor(None, self._clear_cached_artwork, rom_id)

        downloads = [
            self._loop.run_in_executor(None, self._download_sgdb_artwork, sgdb_id, rom_id, asset_type)
            for asset_type in ("hero", "logo", "grid", "icon")
        ]
        await asyncio.gather(*downloads)
        return {"success": True}

    def _clear_cached_artwork(self, rom_id):
        """Remove the four cached artwork PNGs for *rom_id* (sync)."""
        art_dir = self._sgdb_artwork_cache.cache_dir()
        for asset_type in ("hero", "logo", "grid", "icon"):
            path = os.path.join(art_dir, f"{rom_id}_{asset_type}.png")
            if self._sgdb_artwork_cache.exists(path):
                try:
                    self._sgdb_artwork_cache.remove_file(path)
                except OSError as e:
                    self._logger.warning(f"Failed to clear cached artwork {path}: {e}")

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

        sgdb_id = self._resolve_sgdb_id_state_only(rom_id)
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
            return {"success": False, "reason": "no_api_key", "message": "No API key configured"}
        try:
            data = await self._loop.run_in_executor(None, self._sgdb_api.verify_api_key, api_key)
            if data.get("success"):
                return {"success": True, "message": "API key is valid"}
            return {
                "success": False,
                "reason": ErrorCode.AUTH_FAILED.value,
                "message": "API key rejected by SteamGridDB",
            }
        except SgdbApiError as e:
            self._logger.warning(f"SGDB API key verification HTTP error: {e.status_code}")
            if e.status_code in (401, 403):
                return {"success": False, "reason": ErrorCode.AUTH_FAILED.value, "message": "Invalid API key"}
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": f"SteamGridDB error: HTTP {e.status_code}",
            }
        except Exception as e:
            self._logger.error(f"SGDB API key verification failed: {e}")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": f"Connection failed: {e}",
            }

    def save_sgdb_api_key(self, api_key):
        if api_key and api_key != "••••":
            self._settings["steamgriddb_api_key"] = api_key
            self._settings_persister.save_settings()
        return {"success": True, "message": "SteamGridDB API key saved"}

    # -- cache pruning -----------------------------------------------------

    def prune_orphaned_artwork_cache(self):
        """Remove SGDB artwork cache files for rom_ids not bound in ``uow.roms``."""
        art_dir = self._sgdb_artwork_cache.cache_dir()
        if not self._sgdb_artwork_cache.is_dir(art_dir):
            return
        with self._uow_factory() as uow:
            registry = {str(rom.rom_id) for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None}
        pruned = 0
        for filename in self._sgdb_artwork_cache.listdir(art_dir):
            # Always remove leftover .tmp files
            if filename.endswith(".tmp"):
                try:
                    self._sgdb_artwork_cache.remove_file(os.path.join(art_dir, filename))
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
                    self._sgdb_artwork_cache.remove_file(os.path.join(art_dir, filename))
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
            return {"success": False, "reason": "invalid_payload", "message": "Failed to decode icon data"}

        success = await self._loop.run_in_executor(None, self._save_icon_to_grid, app_id, icon_bytes)
        return {"success": success}
