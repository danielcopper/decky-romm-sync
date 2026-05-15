"""Library fetch sub-service.

Owns every roundtrip to the RomM library: listing platforms,
listing collections, the incremental/full ROM pagination loop, and
the per-ROM metadata-cache stamping that follows a successful fetch.
Settings reads/writes about which platforms/collections are enabled
live here too, since they shape the fetch query. Anything that
transforms fetched ROMs into Steam-shortcut shape belongs on the
façade or downstream sub-services; this file stops at "we now have
the ROM list and metadata is cached".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.shortcut_data import build_shortcuts_data
from domain.sync_state import SyncState
from domain.work_unit import WorkUnit
from lib.errors import classify_error
from services.library._state import LibrarySyncStateBox

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from services.protocols import (
        DebugLogger,
        MetadataExtractor,
        RommApiProtocol,
        SettingsPersister,
    )

    # Orchestrator-supplied progress emitter. Matches the kw-only signature
    # of ``SyncOrchestrator._emit_progress``: phase positional, every other
    # field keyword. Sub-services consume this through the Config seam.
    EmitProgressFn = Callable[..., Awaitable[None]]


_SYNC_CANCELLED = "Sync cancelled"


@dataclass(frozen=True)
class LibraryFetcherConfig:
    """Frozen wiring bundle handed to ``LibraryFetcher.__init__``.

    Holds the Protocol-typed RomM adapter, the live state/settings/
    metadata-cache dicts, runtime infrastructure (loop, logger),
    plugin-dir reference (used for shortcut-data path construction),
    settings persistence callback, debug-logger seam, the shared
    ``LibrarySyncStateBox`` (read for the cancel signal), an
    ``_emit_progress`` callback the fetcher uses to surface long
    paginated fetches to the frontend, and the optional
    ``MetadataExtractor`` peer service the fetcher stamps the metadata
    cache through.
    """

    romm_api: RommApiProtocol
    state: dict
    settings: dict
    metadata_cache: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    settings_persister: SettingsPersister
    log_debug: DebugLogger
    sync_state_box: LibrarySyncStateBox
    emit_progress: EmitProgressFn
    metadata_service: MetadataExtractor | None = None


class LibraryFetcher:
    """Library fetch sub-service: platform/collection metadata + ROM pagination."""

    def __init__(self, *, config: LibraryFetcherConfig) -> None:
        self._romm_api = config.romm_api
        self._state = config.state
        self._settings = config.settings
        self._metadata_cache = config.metadata_cache
        self._loop = config.loop
        self._logger = config.logger
        self._plugin_dir = config.plugin_dir
        self._settings_persister = config.settings_persister
        self._log_debug = config.log_debug
        self._sync_state = config.sync_state_box
        self._emit_progress = config.emit_progress
        self._metadata_service = config.metadata_service

    # ── Platform metadata callables ──────────────────────────────

    async def get_platforms(self):
        try:
            platforms = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            self._logger.error(f"Failed to fetch platforms: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}

        if not isinstance(platforms, list):
            self._logger.error(f"Unexpected platforms response type: {type(platforms).__name__}")
            return {"success": False, "message": "Invalid server response", "error_code": "api_error"}

        enabled = self._settings.get("enabled_platforms", {})
        result = []
        for p in platforms:
            rom_count = p.get("rom_count", 0)
            if rom_count == 0:
                continue
            pid = str(p["id"])
            result.append(
                {
                    "id": p["id"],
                    "name": p.get("name", ""),
                    "slug": p.get("slug", ""),
                    "rom_count": rom_count,
                    "sync_enabled": enabled.get(pid, len(enabled) == 0),
                }
            )
        return {"success": True, "platforms": result}

    def save_platform_sync(self, platform_id, enabled):
        pid = str(platform_id)
        self._settings["enabled_platforms"][pid] = bool(enabled)
        self._settings_persister.save_settings()
        return {"success": True}

    async def set_all_platforms_sync(self, enabled):
        enabled = bool(enabled)
        try:
            platforms = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            self._logger.error(f"Failed to fetch platforms: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}

        ep = {}
        for p in platforms:
            ep[str(p["id"])] = enabled
        self._settings["enabled_platforms"] = ep
        self._settings_persister.save_settings()
        return {"success": True}

    # ── Collection metadata callables ────────────────────────────

    async def get_collections(self):
        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}
        try:
            franchise_collections = await self._loop.run_in_executor(
                None, self._romm_api.list_virtual_collections, "franchise"
            )
        except Exception as e:
            self._logger.warning(f"Failed to fetch franchise collections, continuing without them: {e}")
            franchise_collections = []

        enabled = self._settings.get("enabled_collections", {})
        result = []
        for c in user_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled.get(cid, False),
                    "category": "favorites" if c.get("is_favorite") else "user",
                }
            )
        for c in franchise_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled.get(cid, False),
                    "category": "franchise",
                }
            )

        _category_order = {"favorites": 0, "user": 1, "franchise": 2}
        result.sort(key=lambda x: (_category_order.get(x["category"], 99), x["name"].lower()))
        return {"success": True, "collections": result}

    def save_collection_sync(self, collection_id, enabled):
        self._settings.setdefault("enabled_collections", {})[str(collection_id)] = bool(enabled)
        self._settings_persister.save_settings()
        return {"success": True}

    async def set_all_collections_sync(self, enabled, category=None):
        enabled = bool(enabled)
        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}
        try:
            franchise_collections = await self._loop.run_in_executor(
                None, self._romm_api.list_virtual_collections, "franchise"
            )
        except Exception as e:
            self._logger.warning(f"Failed to fetch franchise collections, continuing without them: {e}")
            franchise_collections = []

        all_collections = []
        for c in user_collections:
            cat = "favorites" if c.get("is_favorite") else "user"
            all_collections.append((str(c["id"]), cat))
        for c in franchise_collections:
            all_collections.append((str(c["id"]), "franchise"))

        ec = self._settings.setdefault("enabled_collections", {})
        for cid, cat in all_collections:
            if category is None or cat == category:
                ec[cid] = enabled
        self._settings_persister.save_settings()
        return {"success": True}

    # ── ROM fetch pipeline ───────────────────────────────────────

    async def _fetch_enabled_platforms(self):
        """Fetch and filter platforms by enabled_platforms setting."""
        platforms = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        if not isinstance(platforms, list):
            self._logger.error(f"Unexpected platforms response type: {type(platforms).__name__}")
            return []

        enabled = self._settings.get("enabled_platforms", {})
        no_prefs = len(enabled) == 0
        self._logger.info(f"Platform filter: {len(enabled)} prefs saved, no_prefs={no_prefs}")
        self._logger.info(f"Enabled platforms: {[k for k, v in enabled.items() if v]}")
        platforms = [p for p in platforms if enabled.get(str(p["id"]), no_prefs)]
        self._logger.info(f"Syncing {len(platforms)} platforms: {[p['name'] for p in platforms]}")
        return platforms

    def _reconstruct_platform_from_registry(self, registry, platform_name, platform_slug):
        """Reconstruct ROM list from registry for an unchanged platform."""
        return [
            {
                "id": int(rid),
                "name": entry["name"],
                "fs_name": entry.get("fs_name", ""),
                "platform_name": platform_name,
                "platform_slug": platform_slug,
                "platform_display_name": platform_name,
                "igdb_id": entry.get("igdb_id"),
                "sgdb_id": entry.get("sgdb_id"),
                "ra_id": entry.get("ra_id"),
            }
            for rid, entry in registry.items()
            if entry.get("platform_name") == platform_name
        ]

    async def _try_incremental_skip(
        self, platform, registry, last_sync, platform_name, platform_slug, all_roms, pi, total_platforms
    ):
        """Try incremental fetch; return True if platform was skipped (unchanged)."""
        registry_count = sum(1 for e in registry.values() if e.get("platform_name") == platform_name)
        if not last_sync or registry_count == 0:
            return False

        try:
            delta_resp = await self._loop.run_in_executor(
                None,
                self._romm_api.list_roms_updated_after,
                platform["id"],
                last_sync,
                1,
                0,
            )
            server_total = delta_resp.get("total", 0) if isinstance(delta_resp, dict) else 0
            platform_total = platform.get("rom_count", 0)

            if server_total == 0 and platform_total == registry_count:
                self._logger.info(f"Skipping {platform_name}: {registry_count} ROMs unchanged")
                all_roms.extend(self._reconstruct_platform_from_registry(registry, platform_name, platform_slug))
                await self._emit_progress(
                    "roms",
                    current=len(all_roms),
                    message=f"{platform_name} unchanged ({pi}/{total_platforms})",
                )
                return True

            self._logger.info(
                f"{platform_name}: {server_total} updated, "
                f"server={platform_total} vs registry={registry_count} — full fetch"
            )
        except Exception as e:
            self._logger.warning(f"Incremental check failed for {platform_name}, falling back to full fetch: {e}")
        return False

    async def _full_fetch_platform_roms(self, platform_id, platform_name, platform_slug, all_roms, pi, total_platforms):
        """Full paginated fetch of ROMs for a single platform."""
        offset = 0
        limit = 50
        await self._emit_progress(
            "roms",
            current=len(all_roms),
            message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
        )

        while True:
            self._check_cancelling()
            try:
                roms = await self._loop.run_in_executor(
                    None,
                    self._romm_api.list_roms,
                    platform_id,
                    limit,
                    offset,
                )
            except Exception as e:
                self._logger.error(f"Failed to fetch ROMs for platform {platform_name}: {e}")
                break

            rom_list = roms.get("items", []) if isinstance(roms, dict) else roms
            for rom in rom_list:
                rom.pop("files", None)
                rom["platform_name"] = platform_name
                rom["platform_slug"] = platform_slug

            all_roms.extend(rom_list)
            await self._emit_progress(
                "roms",
                current=len(all_roms),
                message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
            )
            if len(rom_list) < limit:
                break
            offset += limit

    def _check_cancelling(self):
        """Raise CancelledError if sync is being cancelled."""
        if self._sync_state.sync_state == SyncState.CANCELLING:
            raise asyncio.CancelledError(_SYNC_CANCELLED)

    def _build_shortcuts_data(self, all_roms):
        """Build shortcut data list from ROM list."""
        return build_shortcuts_data(all_roms, self._plugin_dir)

    async def _fetch_single_collection_roms(
        self, collection: dict, all_seen: set[int], collection_only_roms: list[dict]
    ) -> list[int]:
        """Fetch ROMs for a single collection, deduplicating against all_seen.

        Mutates all_seen and collection_only_roms in place.
        Returns the list of all rom_ids belonging to this collection.
        """
        cid = str(collection.get("id", ""))
        is_virtual = collection.get("is_virtual", False)
        coll_rom_ids: list[int] = []

        offset = 0
        limit = 50
        while True:
            self._check_cancelling()
            if is_virtual:
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_virtual_collection, cid, limit, offset
                )
            else:
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_collection, collection["id"], limit, offset
                )

            items = page.get("items", [])
            for rom in items:
                rid = rom["id"]
                coll_rom_ids.append(rid)
                if rid not in all_seen:
                    all_seen.add(rid)
                    rom["platform_name"] = rom.get("platform_name", rom.get("platform_display_name", "Unknown"))
                    rom["platform_slug"] = rom.get("platform_slug", rom.get("platform_fs_slug", ""))
                    rom.pop("files", None)
                    collection_only_roms.append(rom)

            if len(items) < limit:
                break
            offset += limit

        return coll_rom_ids

    async def _fetch_collection_roms(self, seen_rom_ids: set[int]) -> tuple[list[dict], dict[str, list[int]]]:
        """Fetch ROMs from enabled collections, deduplicating against seen_rom_ids.

        Returns (collection_only_roms, collection_memberships).
        collection_only_roms: ROMs not already fetched via platforms
        collection_memberships: {collection_name: [all rom_ids in collection]}
        """
        collection_only_roms: list[dict] = []
        collection_memberships: dict[str, list[int]] = {}

        enabled_collections = self._settings.get("enabled_collections", {})
        enabled_ids = {k for k, v in enabled_collections.items() if v}
        self._log_debug(f"Collection sync: {len(enabled_ids)} enabled: {enabled_ids}")
        if not enabled_ids:
            return collection_only_roms, collection_memberships

        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
            franchise_collections: list[dict] = []
            try:
                franchise_collections = await self._loop.run_in_executor(
                    None, self._romm_api.list_virtual_collections, "franchise"
                )
            except Exception as e:
                self._logger.warning(f"Failed to fetch franchise collections: {e}")

            self._log_debug(
                f"Collection metadata: {len(user_collections)} user, {len(franchise_collections)} franchise"
            )
            all_seen = set(seen_rom_ids)  # Copy so we don't mutate caller's set

            for c in user_collections + franchise_collections:
                cid = str(c.get("id", ""))
                if cid not in enabled_ids:
                    self._log_debug(f"  Skipping collection '{c.get('name', cid)}' (id={cid}, not enabled)")
                    continue

                coll_name = c.get("name", cid)
                is_virtual = c.get("is_virtual", False)
                self._log_debug(f"  Fetching collection '{coll_name}' (id={cid}, virtual={is_virtual})")

                coll_rom_ids = await self._fetch_single_collection_roms(c, all_seen, collection_only_roms)

                if coll_rom_ids:
                    collection_memberships[coll_name] = coll_rom_ids
                    self._log_debug(f"  Collection '{coll_name}': {len(coll_rom_ids)} ROMs")

        except Exception as e:
            self._logger.warning(f"Failed to fetch collection ROMs: {e}")

        if collection_only_roms:
            self._logger.info(
                f"Fetched {len(collection_only_roms)} additional ROMs from {len(collection_memberships)} collections"
            )

        return collection_only_roms, collection_memberships

    # ── Per-unit work queue ──────────────────────────────────────

    async def build_work_queue(self) -> list[WorkUnit]:
        """Phase 0 of the per-unit pipeline: enumerate enabled platforms + collections.

        Returns an ordered list of :class:`WorkUnit` entries (platforms
        first, then user collections, then franchise collections) with
        ROM counts pulled from the listing endpoints. No ROMs are
        fetched here — the queue is a dispatch plan, not a payload.
        """
        units: list[WorkUnit] = []

        platforms = await self._fetch_enabled_platforms()
        for platform in platforms:
            units.append(
                WorkUnit(
                    type="platform",
                    id=int(platform["id"]),
                    name=platform.get("name", platform.get("display_name", "Unknown")),
                    slug=platform.get("slug", ""),
                    rom_count=int(platform.get("rom_count", 0)),
                )
            )

        enabled_collections = self._settings.get("enabled_collections", {})
        enabled_ids = {k for k, v in enabled_collections.items() if v}
        if not enabled_ids:
            return units

        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.warning(f"Failed to fetch user collections for work queue: {e}")
            user_collections = []
        try:
            franchise_collections = await self._loop.run_in_executor(
                None, self._romm_api.list_virtual_collections, "franchise"
            )
        except Exception as e:
            self._logger.warning(f"Failed to fetch franchise collections for work queue: {e}")
            franchise_collections = []

        for c in user_collections:
            cid = str(c.get("id", ""))
            if cid not in enabled_ids:
                continue
            units.append(
                WorkUnit(
                    type="collection",
                    id=cid,
                    name=c.get("name", cid),
                    slug=c.get("slug", ""),
                    rom_count=int(c.get("rom_count", len(c.get("rom_ids", [])))),
                    is_virtual=bool(c.get("is_virtual", False)),
                )
            )
        for c in franchise_collections:
            cid = str(c.get("id", ""))
            if cid not in enabled_ids:
                continue
            units.append(
                WorkUnit(
                    type="collection",
                    id=cid,
                    name=c.get("name", cid),
                    slug=c.get("slug", ""),
                    rom_count=int(c.get("rom_count", len(c.get("rom_ids", [])))),
                    is_virtual=bool(c.get("is_virtual", True)),
                )
            )

        return units

    async def _try_unit_incremental_skip(self, unit: WorkUnit) -> list[dict] | None:
        """Per-unit incremental-skip pre-check for a platform unit.

        Returns the registry-reconstructed ROM list when the platform is
        unchanged (server reports zero rows updated after ``last_sync``
        and the unit's ``rom_count`` matches the registry count for this
        platform). Returns ``None`` to signal "fall through to a full
        paginated fetch" — either the registry has no entries for this
        platform, no prior sync timestamp exists, the delta check
        raised, or the server reports changes.
        """
        platform_name = unit.name
        platform_slug = unit.slug

        registry = self._state.get("shortcut_registry", {})
        last_sync = self._state.get("last_sync")
        registry_count = sum(1 for e in registry.values() if e.get("platform_name") == platform_name)

        if not last_sync or registry_count == 0:
            return None

        try:
            delta_resp = await self._loop.run_in_executor(
                None,
                self._romm_api.list_roms_updated_after,
                int(unit.id),
                last_sync,
                1,
                0,
            )
        except Exception as e:
            self._logger.warning(
                f"Per-unit incremental check failed for {platform_name}, falling back to full fetch: {e}"
            )
            return None

        server_total = delta_resp.get("total", 0) if isinstance(delta_resp, dict) else 0
        if server_total == 0 and unit.rom_count == registry_count:
            self._logger.info(f"Per-unit skip: {platform_name} unchanged ({registry_count} ROMs in registry)")
            return self._reconstruct_platform_from_registry(registry, platform_name, platform_slug)

        self._logger.info(
            f"Per-unit fetch {platform_name}: {server_total} updated, "
            f"server={unit.rom_count} registry={registry_count} — full fetch"
        )
        return None

    async def fetch_platform_unit(self, unit: WorkUnit) -> tuple[list[dict], bool]:
        """Fetch ROMs for a single platform unit.

        Tries the incremental-skip path first: if the platform's
        ``rom_count`` matches the registry's count for that platform
        and no rows have ``updated_after`` last_sync, the registry is
        used to reconstruct the ROM list (avoids re-paginating).

        Returns ``(unit_roms, skipped)`` where ``skipped`` is True when
        the incremental check succeeded — callers can use this to skip
        the artwork-download step entirely if the registry already
        carries the cover_path.
        """
        if unit.type != "platform":
            raise ValueError(f"fetch_platform_unit called with non-platform unit type={unit.type}")

        skip_roms = await self._try_unit_incremental_skip(unit)
        if skip_roms is not None:
            return skip_roms, True

        platform_id = int(unit.id)
        platform_name = unit.name
        platform_slug = unit.slug

        unit_roms: list[dict] = []
        offset = 0
        limit = 50
        while True:
            self._check_cancelling()
            try:
                page = await self._loop.run_in_executor(
                    None,
                    self._romm_api.list_roms,
                    platform_id,
                    limit,
                    offset,
                )
            except Exception as e:
                self._logger.error(f"Failed to fetch ROMs for platform {platform_name}: {e}")
                break

            rom_list = page.get("items", []) if isinstance(page, dict) else page
            for rom in rom_list:
                rom.pop("files", None)
                rom["platform_name"] = platform_name
                rom["platform_slug"] = platform_slug
            unit_roms.extend(rom_list)

            if len(rom_list) < limit:
                break
            offset += limit

        return unit_roms, False

    async def fetch_collection_unit(self, unit: WorkUnit, synced_rom_ids: set[int]) -> tuple[list[dict], list[int]]:
        """Fetch ROMs for a single collection unit.

        Mutates *synced_rom_ids* in place: every ROM seen via this
        collection is added so subsequent units (and the final stale
        cleanup) treat them as covered.

        Returns ``(new_roms, all_collection_rom_ids)``:
          * ``new_roms`` — ROMs not already present in *synced_rom_ids*,
            decorated with platform_name/platform_slug for shortcut
            construction.
          * ``all_collection_rom_ids`` — every rom_id in the collection
            (including those already synced via a platform unit), used
            to build Steam collection memberships at the final phase.
        """
        if unit.type != "collection":
            raise ValueError(f"fetch_collection_unit called with non-collection unit type={unit.type}")

        new_roms: list[dict] = []
        all_collection_rom_ids: list[int] = []

        offset = 0
        limit = 50
        while True:
            self._check_cancelling()
            if unit.is_virtual:
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_virtual_collection, str(unit.id), limit, offset
                )
            else:
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_collection, int(unit.id), limit, offset
                )

            items = page.get("items", []) if isinstance(page, dict) else page
            for rom in items:
                rid = rom["id"]
                all_collection_rom_ids.append(rid)
                if rid in synced_rom_ids:
                    continue
                synced_rom_ids.add(rid)
                rom["platform_name"] = rom.get("platform_name", rom.get("platform_display_name", "Unknown"))
                rom["platform_slug"] = rom.get("platform_slug", rom.get("platform_fs_slug", ""))
                rom.pop("files", None)
                new_roms.append(rom)

            if len(items) < limit:
                break
            offset += limit

        return new_roms, all_collection_rom_ids

    def cache_metadata_for_unit(self, unit_roms: list[dict]) -> None:
        """Stamp the metadata cache for one unit's ROMs and flush.

        Mirrors the cache-and-flush step ``_fetch_and_prepare`` runs
        once at the end of a monolithic sync. Per-unit pipelines call
        this after each unit so a mid-sync crash leaves cached
        metadata for every unit that completed.
        """
        if self._metadata_service is None or not unit_roms:
            return
        for rom in unit_roms:
            rom_id_str = str(rom["id"])
            self._metadata_cache[rom_id_str] = self._metadata_service.extract_metadata(rom)
            self._metadata_service.mark_metadata_dirty()
        self._metadata_service.flush_metadata_if_dirty()

    async def _fetch_and_prepare(self):
        """Fetch platforms + ROMs + collection ROMs, prepare shortcut data.

        Returns (all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids)
        or raises on cancel/error.
        Artwork download is deferred to the apply phase.
        Uses updated_after on subsequent syncs to skip unchanged platforms.
        Emits sync_progress events throughout.
        """

        # Phase 1: Fetch platforms
        await self._emit_progress("platforms", message="Fetching platforms...")
        platforms = await self._fetch_enabled_platforms()
        self._check_cancelling()

        # Phase 2: Fetch ROMs per platform (incremental if possible)
        await self._emit_progress("roms", message="Fetching ROMs...")
        last_sync = self._state.get("last_sync")
        registry = self._state.get("shortcut_registry", {})

        all_roms: list[dict] = []
        total_platforms = len(platforms)
        for pi, platform in enumerate(platforms, 1):
            self._check_cancelling()
            platform_name = platform.get("name", platform.get("display_name", "Unknown"))
            platform_slug = platform.get("slug", "")

            skipped = await self._try_incremental_skip(
                platform, registry, last_sync, platform_name, platform_slug, all_roms, pi, total_platforms
            )
            if not skipped:
                await self._full_fetch_platform_roms(
                    platform["id"], platform_name, platform_slug, all_roms, pi, total_platforms
                )

        self._check_cancelling()
        self._logger.info(f"Fetched {len(all_roms)} ROMs from {len(platforms)} platforms")

        # Record which rom_ids came from platforms
        platform_rom_ids: set[int] = {r["id"] for r in all_roms}

        # Phase 3: Fetch collection ROMs (adds ROMs not already in all_roms)
        collection_only_roms, collection_memberships = await self._fetch_collection_roms(platform_rom_ids)
        all_roms.extend(collection_only_roms)

        # Phase 4: Prepare shortcut data
        shortcuts_data = self._build_shortcuts_data(all_roms)
        self._check_cancelling()

        # Cache metadata from sync response
        if self._metadata_service is not None:
            for rom in all_roms:
                rom_id_str = str(rom["id"])
                self._metadata_cache[rom_id_str] = self._metadata_service.extract_metadata(rom)
                self._metadata_service.mark_metadata_dirty()
            self._metadata_service.flush_metadata_if_dirty()
        self._log_debug(f"Metadata cached for {len(all_roms)} ROMs")

        return all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids
