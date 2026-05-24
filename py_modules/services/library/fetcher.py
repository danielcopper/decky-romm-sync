"""Library fetch sub-service.

Owns every read-only roundtrip to the RomM library: listing platforms,
listing collections, the incremental/full ROM pagination loop, and the
per-unit work-queue construction. Settings reads/writes about which
platforms/collections are enabled live here too, since they shape the
fetch query. Anything that transforms fetched ROMs into Steam-shortcut
shape belongs on the façade or downstream sub-services; this file
stops at "we now have the ROM list". The metadata-cache is stamped
elsewhere (per applied unit) so a fetch never mutates the cache.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.state import MetadataCache, PluginState

from domain.sync_stage import SyncStage
from domain.sync_state import SyncState
from domain.work_unit import CollectionKind, WorkUnit
from lib.errors import classify_error
from services.library._state import LibrarySyncStateBox

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from services.protocols import (
        DebugLogger,
        RommLibraryApi,
        SettingsPersister,
    )

    # Orchestrator-supplied progress emitter. Matches the kw-only signature
    # of ``SyncOrchestrator._emit_progress``: stage positional, every other
    # field keyword. Sub-services consume this through the Config seam.
    EmitProgressFn = Callable[..., Awaitable[None]]


_SYNC_CANCELLED = "Sync cancelled"


def _collection_units(collections: list[dict], enabled_ids: set[str], kind: CollectionKind) -> list[WorkUnit]:
    """Build WorkUnits for collections whose id is in *enabled_ids*, tagged with *kind*."""
    units: list[WorkUnit] = []
    for c in collections:
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
                collection_kind=kind,
            )
        )
    return units


@dataclass(frozen=True)
class LibraryFetcherConfig:
    """Frozen wiring bundle handed to ``LibraryFetcher.__init__``.

    Holds the Protocol-typed RomM adapter, the live state/settings/
    metadata-cache dicts, runtime infrastructure (loop, logger),
    plugin-dir reference (used for shortcut-data path construction),
    settings persistence callback, debug-logger seam, the shared
    ``LibrarySyncStateBox`` (read for the cancel signal), and an
    ``_emit_progress`` callback the fetcher uses to surface long
    paginated fetches to the frontend.
    """

    romm_api: RommLibraryApi
    state: PluginState
    settings: dict
    metadata_cache: MetadataCache
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    settings_persister: SettingsPersister
    log_debug: DebugLogger
    sync_state_box: LibrarySyncStateBox
    emit_progress: EmitProgressFn


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
            smart_collections = await self._loop.run_in_executor(None, self._romm_api.list_smart_collections)
        except Exception as e:
            self._logger.warning(f"Failed to fetch smart collections, continuing without them: {e}")
            smart_collections = []
        try:
            franchise_collections = await self._loop.run_in_executor(
                None, self._romm_api.list_virtual_collections, "franchise"
            )
        except Exception as e:
            self._logger.warning(f"Failed to fetch franchise collections, continuing without them: {e}")
            franchise_collections = []

        enabled = self._get_enabled_collections_buckets()
        result = []
        for c in user_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled["user"].get(cid, False),
                    "kind": "user",
                    "is_favorite": bool(c.get("is_favorite", False)),
                }
            )
        for c in smart_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled["smart"].get(cid, False),
                    "kind": "smart",
                    "is_favorite": False,
                }
            )
        for c in franchise_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled["franchise"].get(cid, False),
                    "kind": "franchise",
                    "is_favorite": False,
                }
            )

        _kind_order = {"user": 0, "smart": 1, "franchise": 2}
        result.sort(key=lambda x: (_kind_order.get(x["kind"], 99), x["name"].lower()))
        return {"success": True, "collections": result}

    def save_collection_sync(self, collection_id, kind, enabled):
        if kind not in ("user", "smart", "franchise"):
            return {"success": False, "reason": "invalid_kind", "message": f"Invalid collection kind: {kind}"}
        buckets = self._get_enabled_collections_buckets()
        buckets[kind][str(collection_id)] = bool(enabled)
        self._settings["enabled_collections"] = buckets
        self._settings_persister.save_settings()
        return {"success": True}

    async def set_all_collections_sync(self, enabled, scope=None):
        enabled = bool(enabled)
        if scope not in (None, "my", "smart", "franchise"):
            return {"success": False, "reason": "invalid_scope", "message": f"Invalid scope: {scope}"}

        buckets = self._get_enabled_collections_buckets()

        for apply_bucket in (self._apply_user_bucket, self._apply_smart_bucket, self._apply_franchise_bucket):
            failure = await apply_bucket(buckets=buckets, enabled=enabled, scope=scope)
            if failure is not None:
                return failure

        self._settings["enabled_collections"] = buckets
        self._settings_persister.save_settings()
        return {"success": True}

    async def _apply_user_bucket(
        self, *, buckets: dict[str, dict[str, bool]], enabled: bool, scope: str | None
    ) -> dict | None:
        """Fetch user collections and stamp the ``user`` bucket. Returns failure dict or None."""
        if scope not in (None, "my"):
            return None
        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}
        for c in user_collections:
            if scope == "my" and bool(c.get("is_favorite", False)):
                continue
            buckets["user"][str(c["id"])] = enabled
        return None

    async def _apply_smart_bucket(
        self, *, buckets: dict[str, dict[str, bool]], enabled: bool, scope: str | None
    ) -> dict | None:
        """Fetch smart collections and stamp the ``smart`` bucket. Returns failure dict or None."""
        if scope not in (None, "smart"):
            return None
        try:
            smart_collections = await self._loop.run_in_executor(None, self._romm_api.list_smart_collections)
        except Exception as e:
            if scope == "smart":
                self._logger.error(f"Failed to fetch smart collections: {e}")
                _code, _msg = classify_error(e)
                return {"success": False, "message": _msg, "error_code": _code}
            self._logger.warning(f"Failed to fetch smart collections, continuing without them: {e}")
            return None
        for c in smart_collections:
            buckets["smart"][str(c["id"])] = enabled
        return None

    async def _apply_franchise_bucket(
        self, *, buckets: dict[str, dict[str, bool]], enabled: bool, scope: str | None
    ) -> dict | None:
        """Fetch franchise collections and stamp the ``franchise`` bucket. Returns failure dict or None."""
        if scope not in (None, "franchise"):
            return None
        try:
            franchise_collections = await self._loop.run_in_executor(
                None, self._romm_api.list_virtual_collections, "franchise"
            )
        except Exception as e:
            if scope == "franchise":
                self._logger.error(f"Failed to fetch franchise collections: {e}")
                _code, _msg = classify_error(e)
                return {"success": False, "message": _msg, "error_code": _code}
            self._logger.warning(f"Failed to fetch franchise collections, continuing without them: {e}")
            return None
        for c in franchise_collections:
            buckets["franchise"][str(c["id"])] = enabled
        return None

    def _get_enabled_collections_buckets(self) -> dict[str, dict[str, bool]]:
        """Return the ``enabled_collections`` setting in its nested-by-kind shape.

        Defensively coerces missing buckets to empty dicts so callers can
        always index by kind without re-checking presence. The migration
        layer is the source of truth for the on-disk shape; this guard
        protects against an in-memory ``settings`` dict that was seeded
        without going through ``load_settings`` (e.g. in tests).
        """
        raw = self._settings.get("enabled_collections", {})
        if not isinstance(raw, dict):
            raw = {}
        buckets: dict[str, dict[str, bool]] = {}
        for kind in ("user", "smart", "franchise"):
            bucket = raw.get(kind, {})
            buckets[kind] = bucket if isinstance(bucket, dict) else {}
        return buckets

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
                    SyncStage.FETCHING,
                    current=len(all_roms),
                    message=f"{platform_name} unchanged ({pi}/{total_platforms})",
                    step=pi,
                    total_steps=total_platforms,
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
            SyncStage.FETCHING,
            current=len(all_roms),
            message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
            step=pi,
            total_steps=total_platforms,
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
            except Exception:
                # Re-raise so the orchestrator aborts before the stale-cleanup
                # pass runs against a partial list. Swallowing here would
                # cause every ROM not yet paginated to be classified as
                # "stale" and removed from Steam.
                self._logger.exception(f"Failed to fetch ROMs for platform {platform_name}")
                raise

            rom_list = roms.get("items", []) if isinstance(roms, dict) else roms
            for rom in rom_list:
                rom.pop("files", None)
                rom["platform_name"] = platform_name
                rom["platform_slug"] = platform_slug

            all_roms.extend(rom_list)
            await self._emit_progress(
                SyncStage.FETCHING,
                current=len(all_roms),
                message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
                step=pi,
                total_steps=total_platforms,
            )
            if len(rom_list) < limit:
                break
            offset += limit

    def _check_cancelling(self):
        """Raise CancelledError if sync is being cancelled."""
        if self._sync_state.sync_state == SyncState.CANCELLING:
            raise asyncio.CancelledError(_SYNC_CANCELLED)

    # ── Per-unit work queue ──────────────────────────────────────

    async def build_work_queue(self) -> list[WorkUnit]:
        """Phase 0 of the per-unit pipeline: enumerate enabled platforms + collections.

        Returns an ordered list of :class:`WorkUnit` entries (platforms
        first, then user collections, then smart collections, then
        franchise collections) with ROM counts pulled from the listing
        endpoints. No ROMs are fetched here — the queue is a dispatch
        plan, not a payload.
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

        buckets = self._get_enabled_collections_buckets()
        enabled_user_ids = {k for k, v in buckets["user"].items() if v}
        enabled_smart_ids = {k for k, v in buckets["smart"].items() if v}
        enabled_franchise_ids = {k for k, v in buckets["franchise"].items() if v}
        if not (enabled_user_ids or enabled_smart_ids or enabled_franchise_ids):
            return units

        units.extend(await self._build_user_collection_units(enabled_user_ids))
        units.extend(await self._build_smart_collection_units(enabled_smart_ids))
        units.extend(await self._build_franchise_collection_units(enabled_franchise_ids))

        return units

    async def _build_user_collection_units(self, enabled_ids: set[str]) -> list[WorkUnit]:
        """Fetch user collections and emit work units for those whose id is in *enabled_ids*."""
        if not enabled_ids:
            return []
        try:
            collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.warning(f"Failed to fetch user collections for work queue: {e}")
            collections = []
        return _collection_units(collections, enabled_ids, "user")

    async def _build_smart_collection_units(self, enabled_ids: set[str]) -> list[WorkUnit]:
        """Fetch smart collections and emit work units for those whose id is in *enabled_ids*."""
        if not enabled_ids:
            return []
        try:
            collections = await self._loop.run_in_executor(None, self._romm_api.list_smart_collections)
        except Exception as e:
            self._logger.warning(f"Failed to fetch smart collections for work queue: {e}")
            collections = []
        return _collection_units(collections, enabled_ids, "smart")

    async def _build_franchise_collection_units(self, enabled_ids: set[str]) -> list[WorkUnit]:
        """Fetch franchise collections and emit work units for those whose id is in *enabled_ids*."""
        if not enabled_ids:
            return []
        try:
            collections = await self._loop.run_in_executor(None, self._romm_api.list_virtual_collections, "franchise")
        except Exception as e:
            self._logger.warning(f"Failed to fetch franchise collections for work queue: {e}")
            collections = []
        return _collection_units(collections, enabled_ids, "franchise")

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
        the incremental check succeeded. Callers use ``skipped=True`` as
        the signal to short-circuit the entire per-unit apply + commit
        branch — no ``sync_apply_unit`` emit, no frontend roundtrip, no
        registry commit. The reconstructed ``unit_roms`` still flow back
        so the caller can keep its synced-rom accounting accurate.
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
            except Exception:
                # Re-raise so the orchestrator aborts before the stale-cleanup
                # pass runs against a partial list. Swallowing here would
                # cause every ROM not yet paginated to be classified as
                # "stale" and removed from Steam.
                self._logger.exception(f"Failed to fetch ROMs for platform {platform_name}")
                raise

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
            if unit.collection_kind == "franchise":
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_virtual_collection, str(unit.id), limit, offset
                )
            elif unit.collection_kind == "smart":
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_smart_collection, int(unit.id), limit, offset
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
