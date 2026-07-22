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

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NamedTuple

from domain.collection_owner import is_own_collection
from domain.fetch_generation import count_rows_for_skip
from domain.platform_prefs import materialize_enabled_platforms, resolve_sync_enabled
from domain.skip_prediction import collapsed_shortcut_count, new_shortcut_count, predict_unit_skip
from domain.sync_stage import SyncStage
from domain.sync_state import SyncCancelled, SyncState
from domain.work_unit import CollectionKind, WorkUnit
from lib.errors import classify_error
from lib.list_result import ErrorCode
from lib.romm_paging import LIST_PAGE_SIZE

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Awaitable, Callable

    from domain.collection_sync_state import CollectionSyncState
    from services.library._state import LibrarySyncStateBox
    from services.protocols import (
        DebugLogger,
        RommLibraryApi,
        SettingsPersister,
        UnitOfWorkFactory,
    )

    # Orchestrator-supplied progress emitter. Matches the kw-only signature
    # of ``SyncOrchestrator.emit_progress``: stage positional, every other
    # field keyword. Sub-services consume this through the Config seam.
    EmitProgressFn = Callable[..., Awaitable[None]]


_SYNC_CANCELLED = "Sync cancelled"

# The RomM virtual-collection types the plugin syncs as browsable collections.
# RomM's ``VirtualCollection`` model has five ``type`` values, but only these two
# are surfaced as browsable collections in RomM's own Collections view: IGDB
# ``franchise`` groupings and the default IGDB ``collection`` (series) groupings.
# ``genre`` / ``company`` / ``mode`` are deliberately excluded — RomM treats them
# as ROM filter facets, not collections. Both types share the single internal
# ``"virtual"`` kind; the per-item ``virtual_type`` sub-field carries which one.
# Adding a type here is the only change needed to sync it.
_SUPPORTED_VIRTUAL_TYPES: tuple[str, ...] = ("franchise", "collection")


class _PlanEstimate(NamedTuple):
    """One platform's plan-time estimate riders for the ``sync_plan`` payload.

    Estimate-ONLY (ADR-0023) — these price the frontend's time readout and
    never feed the actual skip decision. ``None`` counts mean "unknown" and
    ride the payload absent.
    """

    predicted_skip: bool
    collapsed_count: int | None
    bound_count: int | None
    new_shortcut_count: int | None


class _SkipBaseline(NamedTuple):
    """One platform's locally persisted inputs to the incremental-skip gate."""

    stamp_completed_at: str | None
    stamp_rom_count: int | None
    reconstructed_roms: list[dict[str, Any]]
    fetched_count: int
    persisted_count: int
    needs_backfill: bool


# Emit a ``fetching`` progress frame on the first page and every Nth page of a
# paginated unit fetch. At the 500-ROM page size a large platform paginates in
# only a handful of pages (a ~3000-ROM platform is 7), so every page is narrated
# (interval 1) — a "page 3/7" update every few seconds — rather than throttled.
# The interval knob stays so a future larger page count can throttle again.
_FETCH_PROGRESS_PAGE_INTERVAL = 1


def _collection_units(
    collections: list[dict[str, Any]],
    enabled_ids: set[str],
    kind: CollectionKind,
    *,
    virtual_type: str | None = None,
    own_user_id: int | None = None,
    filter_to_own: bool = False,
) -> list[WorkUnit]:
    """Build WorkUnits for collections whose id is in *enabled_ids*, tagged with *kind*.

    When *filter_to_own* is set (the "Mine" owner-scope), a foreign collection —
    one owned by a known user id other than *own_user_id* — is dropped from the
    queue even if it is enabled, so a scope selected over an earlier enable never
    syncs someone else's collection. Virtual collections have no owner and
    always survive (:func:`is_own_collection`).

    *virtual_type* stamps the unit's virtual sub-type (``"franchise"`` /
    ``"collection"``) for the ``kind == "virtual"`` caller, which fetches one
    type at a time and so knows it authoritatively — the same source the QAM
    listing uses. ``None`` for standard/smart callers (their kind alone labels
    them).
    """
    units: list[WorkUnit] = []
    for c in collections:
        cid = str(c.get("id", ""))
        if cid not in enabled_ids:
            continue
        if filter_to_own and not is_own_collection(c.get("user_id"), own_user_id, kind=kind):
            continue
        units.append(
            WorkUnit(
                type="collection",
                id=cid,
                name=c.get("name", cid),
                slug=c.get("slug", ""),
                rom_count=int(c.get("rom_count", len(c.get("rom_ids", [])))),
                collection_kind=kind,
                virtual_type=virtual_type,
                # RomM bumps the collection's updated_at on any membership change
                # (#742). Threaded so the skip gate compares it against the stamp;
                # ``None`` for a listing that omits it (e.g. virtual, never
                # stamped).
                collection_updated_at=c.get("updated_at"),
            )
        )
    return units


@dataclass(frozen=True)
class LibraryFetcherConfig:
    """Frozen wiring bundle handed to ``LibraryFetcher.__init__``.

    Holds the Protocol-typed RomM adapter, the live settings dict,
    runtime infrastructure (loop, logger), plugin-dir reference (used
    for shortcut-data path construction), settings persistence callback,
    debug-logger seam, the shared ``LibrarySyncStateBox`` (read for the
    cancel signal), and an ``_emit_progress`` callback the fetcher uses
    to surface long paginated fetches to the frontend.
    """

    romm_api: RommLibraryApi
    settings: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    settings_persister: SettingsPersister
    log_debug: DebugLogger
    uow_factory: UnitOfWorkFactory
    sync_state_box: LibrarySyncStateBox
    emit_progress: EmitProgressFn


class LibraryFetcher:
    """Library fetch sub-service: platform/collection metadata + ROM pagination."""

    def __init__(self, *, config: LibraryFetcherConfig) -> None:
        self._romm_api = config.romm_api
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._plugin_dir = config.plugin_dir
        self._settings_persister = config.settings_persister
        self._log_debug = config.log_debug
        self._uow_factory = config.uow_factory
        self._sync_state = config.sync_state_box
        self._emit_progress = config.emit_progress

    # ── Platform metadata callables ──────────────────────────────

    async def get_platforms(self):
        try:
            # Typed ``object`` so the isinstance guard below is genuine
            # narrowing — the RomM API return type is a JSON-shape promise
            # the server can break (malformed payload, schema drift).
            platforms: object = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            self._logger.error(f"Failed to fetch platforms: {e}")
            _reason, _msg = classify_error(e)
            return {"success": False, "reason": _reason, "message": _msg}

        if not isinstance(platforms, list):
            self._logger.error(f"Unexpected platforms response type: {type(platforms).__name__}")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Invalid server response",
            }

        # Only platforms with ROMs are shown (and thus toggleable), so the
        # materialized map covers exactly the set the user can act on.
        shown_ids = [str(p["id"]) for p in platforms if p.get("rom_count", 0) > 0]

        # Self-heal the empty-map sentinel into an explicit all-True map at the
        # first read with a live platform list, so every later single-key
        # ``save_platform_sync`` write is a partial update of a complete map —
        # a one-platform toggle can never again be misread as "rest disabled"
        # (#1007). Idempotent: only fires while the map is the empty sentinel.
        enabled = self._settings.get("enabled_platforms", {})
        materialized = materialize_enabled_platforms(enabled, shown_ids)
        if materialized is not None:
            self._settings["enabled_platforms"] = materialized
            self._settings_persister.save_settings()
            enabled = materialized

        # Persisted post-collapse shortcut counts for the toggle labels
        # (#1382). Display garnish only, so a failed read degrades to the raw
        # server counts instead of failing the platform list.
        try:
            collapsed_counts = await self._loop.run_in_executor(None, self._read_collapsed_counts)
        except Exception as e:
            self._logger.warning(f"Collapsed-count read failed, falling back to raw ROM counts: {e}")
            collapsed_counts = {}

        result = []
        for p in platforms:
            rom_count = p.get("rom_count", 0)
            if rom_count == 0:
                continue
            pid = str(p["id"])
            entry = {
                "id": p["id"],
                "name": p.get("name", ""),
                "slug": p.get("slug", ""),
                "rom_count": rom_count,
                "sync_enabled": resolve_sync_enabled(enabled, pid),
            }
            collapsed = collapsed_counts.get(entry["slug"])
            if collapsed is not None:
                entry["collapsed_count"] = collapsed
            result.append(entry)
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
            _reason, _msg = classify_error(e)
            return {"success": False, "reason": _reason, "message": _msg}

        ep = {}
        for p in platforms:
            ep[str(p["id"])] = enabled
        self._settings["enabled_platforms"] = ep
        self._settings_persister.save_settings()
        return {"success": True}

    # ── Collection metadata callables ────────────────────────────

    async def get_collections(self):
        try:
            standard_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _reason, _msg = classify_error(e)
            return {"success": False, "reason": _reason, "message": _msg}
        try:
            smart_collections = await self._loop.run_in_executor(None, self._romm_api.list_smart_collections)
        except Exception as e:
            self._logger.warning(f"Failed to fetch smart collections, continuing without them: {e}")
            smart_collections = []
        # One flat "virtual" kind, fetched per supported type so each item can be
        # tagged with its ``virtual_type`` for display. A failed type-fetch is
        # fail-open (warn + skip that type) so the rest of the list still returns.
        virtual_by_type: list[tuple[str, list[dict[str, Any]]]] = []
        for virtual_type in _SUPPORTED_VIRTUAL_TYPES:
            try:
                items = await self._loop.run_in_executor(None, self._romm_api.list_virtual_collections, virtual_type)
            except Exception as e:
                self._logger.warning(f"Failed to fetch {virtual_type} collections, continuing without them: {e}")
                continue
            virtual_by_type.append((virtual_type, items))

        enabled = self._get_enabled_collections_buckets()
        # Own identity for the owner-scope tag. ``None`` (never fetched / offline)
        # tags every collection ``is_own=True`` so the frontend "Mine" filter
        # degrades to "All" rather than filtering wrongly (the non-breaking
        # fallback).
        own_user_id = self._settings.get("romm_user_id")
        result = []
        for c in standard_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled["standard"].get(cid, False),
                    "kind": "standard",
                    "is_favorite": bool(c.get("is_favorite", False)),
                    "is_own": is_own_collection(c.get("user_id"), own_user_id, kind="standard"),
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
                    "is_own": is_own_collection(c.get("user_id"), own_user_id, kind="smart"),
                }
            )
        for virtual_type, items in virtual_by_type:
            for c in items:
                cid = str(c["id"])
                result.append(
                    {
                        "id": cid,
                        "name": c.get("name", ""),
                        "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                        "sync_enabled": enabled["virtual"].get(cid, False),
                        "kind": "virtual",
                        "virtual_type": virtual_type,
                        "is_favorite": False,
                        # Virtual collections have no owner — always own.
                        "is_own": True,
                    }
                )

        _kind_order = {"standard": 0, "smart": 1, "virtual": 2}
        result.sort(key=lambda x: (_kind_order.get(x["kind"], 99), x["name"].lower()))
        return {"success": True, "collections": result}

    def save_collection_sync(self, collection_id, kind, enabled):
        if kind not in ("standard", "smart", "virtual"):
            return {"success": False, "reason": "invalid_kind", "message": f"Invalid collection kind: {kind}"}
        buckets = self._get_enabled_collections_buckets()
        buckets[kind][str(collection_id)] = bool(enabled)
        self._settings["enabled_collections"] = buckets
        self._settings_persister.save_settings()
        return {"success": True}

    def save_collections_sync(self, collection_ids, kind, enabled):
        """Batch-stamp a bounded set of collection ids into one kind's bucket.

        The frontend uses this for the filtered-subset Enable/Disable All (a
        search or per-type filter is active) so the whole-kind
        ``set_all_collections_sync`` — which re-fetches every collection from the
        server — stays reserved for the unfiltered case. One settings write
        stamps every id in ``collection_ids`` to ``enabled`` in the ``kind``
        bucket. An unknown kind or a non-list id argument is rejected with the
        canonical failure shape; an empty id list is a success no-op (nothing to
        stamp, no write).
        """
        if kind not in ("standard", "smart", "virtual"):
            return {"success": False, "reason": "invalid_kind", "message": f"Invalid collection kind: {kind}"}
        if not isinstance(collection_ids, list):
            return {"success": False, "reason": "invalid_ids", "message": "collection_ids must be a list"}
        if not collection_ids:
            return {"success": True}
        buckets = self._get_enabled_collections_buckets()
        for cid in collection_ids:
            buckets[kind][str(cid)] = bool(enabled)
        self._settings["enabled_collections"] = buckets
        self._settings_persister.save_settings()
        return {"success": True}

    async def set_all_collections_sync(self, enabled, scope=None):
        enabled = bool(enabled)
        if scope not in (None, "standard", "smart", "virtual"):
            return {"success": False, "reason": "invalid_scope", "message": f"Invalid scope: {scope}"}

        buckets = self._get_enabled_collections_buckets()

        for apply_bucket in (self._apply_standard_bucket, self._apply_smart_bucket, self._apply_virtual_bucket):
            failure = await apply_bucket(buckets=buckets, enabled=enabled, scope=scope)
            if failure is not None:
                return failure

        self._settings["enabled_collections"] = buckets
        self._settings_persister.save_settings()
        return {"success": True}

    async def _apply_standard_bucket(
        self, *, buckets: dict[str, dict[str, bool]], enabled: bool, scope: str | None
    ) -> dict[str, Any] | None:
        """Fetch standard collections and stamp the ``standard`` bucket. Returns failure dict or None."""
        if scope not in (None, "standard"):
            return None
        try:
            standard_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _reason, _msg = classify_error(e)
            return {"success": False, "reason": _reason, "message": _msg}
        for c in standard_collections:
            if scope == "standard" and bool(c.get("is_favorite", False)):
                continue
            buckets["standard"][str(c["id"])] = enabled
        return None

    async def _apply_smart_bucket(
        self, *, buckets: dict[str, dict[str, bool]], enabled: bool, scope: str | None
    ) -> dict[str, Any] | None:
        """Fetch smart collections and stamp the ``smart`` bucket. Returns failure dict or None."""
        if scope not in (None, "smart"):
            return None
        try:
            smart_collections = await self._loop.run_in_executor(None, self._romm_api.list_smart_collections)
        except Exception as e:
            if scope == "smart":
                self._logger.error(f"Failed to fetch smart collections: {e}")
                _reason, _msg = classify_error(e)
                return {"success": False, "reason": _reason, "message": _msg}
            self._logger.warning(f"Failed to fetch smart collections, continuing without them: {e}")
            return None
        for c in smart_collections:
            buckets["smart"][str(c["id"])] = enabled
        return None

    async def _apply_virtual_bucket(
        self, *, buckets: dict[str, dict[str, bool]], enabled: bool, scope: str | None
    ) -> dict[str, Any] | None:
        """Fetch every supported virtual type and stamp the ``virtual`` bucket.

        Returns a failure dict or None. Under the ``virtual`` scope a failed
        type-fetch surfaces the error (the user targeted virtual collections);
        under the all-buckets scope (``None``) it warns and continues so a single
        unavailable type never fails the whole Enable/Disable All.
        """
        if scope not in (None, "virtual"):
            return None
        for virtual_type in _SUPPORTED_VIRTUAL_TYPES:
            try:
                virtual_collections = await self._loop.run_in_executor(
                    None, self._romm_api.list_virtual_collections, virtual_type
                )
            except Exception as e:
                if scope == "virtual":
                    self._logger.error(f"Failed to fetch {virtual_type} collections: {e}")
                    _reason, _msg = classify_error(e)
                    return {"success": False, "reason": _reason, "message": _msg}
                self._logger.warning(f"Failed to fetch {virtual_type} collections, continuing without them: {e}")
                continue
            for c in virtual_collections:
                buckets["virtual"][str(c["id"])] = enabled
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
        for kind in ("standard", "smart", "virtual"):
            bucket = raw.get(kind, {})
            buckets[kind] = bucket if isinstance(bucket, dict) else {}
        return buckets

    # ── ROM fetch pipeline ───────────────────────────────────────

    async def _fetch_enabled_platforms(self):
        """Fetch and filter platforms by enabled_platforms setting."""
        # Typed ``object`` so the isinstance guard below is genuine narrowing —
        # the RomM API return type is a JSON-shape promise the server can break.
        platforms: object = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        if not isinstance(platforms, list):
            self._logger.error(f"Unexpected platforms response type: {type(platforms).__name__}")
            return []

        # Empty map = "all platforms enabled" (the safety floor for a user who
        # syncs without ever opening the Platforms page). ``get_platforms``
        # materializes the full map on first view, so a partial map here always
        # reflects explicit per-platform choices, never the sentinel (#1007).
        enabled = self._settings.get("enabled_platforms", {})
        self._logger.info(f"Platform filter: {len(enabled)} prefs saved, no_prefs={not enabled}")
        self._logger.info(f"Enabled platforms: {[k for k, v in enabled.items() if v]}")
        platforms = [p for p in platforms if resolve_sync_enabled(enabled, str(p["id"]))]
        self._logger.info(f"Syncing {len(platforms)} platforms: {[p['name'] for p in platforms]}")
        return platforms

    def _check_cancelling(self):
        """Raise SyncCancelled if sync is being cancelled."""
        if self._sync_state.sync_state == SyncState.CANCELLING:
            raise SyncCancelled(_SYNC_CANCELLED)

    # ── Per-unit work queue ──────────────────────────────────────

    async def build_work_queue(self) -> list[WorkUnit]:
        """Phase 0 of the per-unit pipeline: enumerate enabled platforms + collections.

        Returns an ordered list of :class:`WorkUnit` entries (platforms
        first, then standard collections, then smart collections, then
        virtual collections) with ROM counts pulled from the listing
        endpoints. No ROMs are fetched here — the queue is a dispatch
        plan, not a payload. Units additionally carry the plan-time estimate
        riders for the ``sync_plan`` payload — ``predicted_skip`` /
        ``collapsed_count`` (#1382) and ``new_shortcut_count`` (#1517) on
        platform units, ``bound_count`` on both kinds (#1511). Estimate-only:
        they never feed the actual skip decision (ADR-0023).
        """
        units: list[WorkUnit] = []

        platforms = await self._fetch_enabled_platforms()
        platform_units = [
            WorkUnit(
                type="platform",
                id=int(platform["id"]),
                name=platform.get("name", platform.get("display_name", "Unknown")),
                slug=platform.get("slug", ""),
                rom_count=int(platform.get("rom_count", 0)),
            )
            for platform in platforms
        ]
        units.extend(await self._attach_plan_estimates(platform_units))

        buckets = self._get_enabled_collections_buckets()
        enabled_standard_ids = {k for k, v in buckets["standard"].items() if v}
        enabled_smart_ids = {k for k, v in buckets["smart"].items() if v}
        enabled_virtual_ids = {k for k, v in buckets["virtual"].items() if v}
        if not (enabled_standard_ids or enabled_smart_ids or enabled_virtual_ids):
            return units

        # Owner-scope filter (#1532): when "Mine" is selected AND our identity is
        # known, foreign standard/smart collections are dropped from the queue — a
        # sync scope that filters OVER the enabled ids without mutating them.
        # Unknown identity (own_user_id is None) never filters, so "Mine" is a
        # no-op until identity is available (non-breaking). Virtual collections
        # have no owner and are never filtered.
        own_user_id = self._settings.get("romm_user_id")
        filter_to_own = self._settings.get("collection_owner_scope") == "own" and own_user_id is not None

        collection_units: list[WorkUnit] = []
        collection_units.extend(
            await self._build_standard_collection_units(
                enabled_standard_ids, own_user_id=own_user_id, filter_to_own=filter_to_own
            )
        )
        collection_units.extend(
            await self._build_smart_collection_units(
                enabled_smart_ids, own_user_id=own_user_id, filter_to_own=filter_to_own
            )
        )
        collection_units.extend(await self._build_virtual_collection_units(enabled_virtual_ids))
        # One short read UoW for every collection at once — after the listing
        # fetches, never across them.
        units.extend(await self._attach_collection_bound_counts(collection_units))

        return units

    async def _build_standard_collection_units(
        self, enabled_ids: set[str], *, own_user_id: int | None = None, filter_to_own: bool = False
    ) -> list[WorkUnit]:
        """Fetch standard collections and emit work units for those whose id is in *enabled_ids*.

        Under the "Mine" owner-scope (*filter_to_own*), foreign collections are
        dropped even when enabled (see :func:`_collection_units`).
        """
        if not enabled_ids:
            return []
        try:
            collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.warning(f"Failed to fetch standard collections for work queue: {e}")
            collections = []
        return _collection_units(
            collections, enabled_ids, "standard", own_user_id=own_user_id, filter_to_own=filter_to_own
        )

    async def _build_smart_collection_units(
        self, enabled_ids: set[str], *, own_user_id: int | None = None, filter_to_own: bool = False
    ) -> list[WorkUnit]:
        """Fetch smart collections and emit work units for those whose id is in *enabled_ids*.

        Under the "Mine" owner-scope (*filter_to_own*), foreign collections are
        dropped even when enabled (see :func:`_collection_units`).
        """
        if not enabled_ids:
            return []
        try:
            collections = await self._loop.run_in_executor(None, self._romm_api.list_smart_collections)
        except Exception as e:
            self._logger.warning(f"Failed to fetch smart collections for work queue: {e}")
            collections = []
        return _collection_units(
            collections, enabled_ids, "smart", own_user_id=own_user_id, filter_to_own=filter_to_own
        )

    async def _build_virtual_collection_units(self, enabled_ids: set[str]) -> list[WorkUnit]:
        """Fetch every supported virtual type and emit units for those whose id is in *enabled_ids*.

        The ids are globally unique across virtual types (RomM bakes the type
        into the base64 id), so a single enabled-id set selects across the merged
        listing. A failed type-fetch is fail-open (warn + skip that type).
        """
        if not enabled_ids:
            return []
        units: list[WorkUnit] = []
        for virtual_type in _SUPPORTED_VIRTUAL_TYPES:
            try:
                collections = await self._loop.run_in_executor(
                    None, self._romm_api.list_virtual_collections, virtual_type
                )
            except Exception as e:
                self._logger.warning(f"Failed to fetch {virtual_type} collections for work queue: {e}")
                continue
            units.extend(_collection_units(collections, enabled_ids, "virtual", virtual_type=virtual_type))
        return units

    async def _attach_plan_estimates(self, platform_units: list[WorkUnit]) -> list[WorkUnit]:
        """Stamp each platform unit with its plan-time estimate riders (#1382).

        Fail-open: the riders only price the ``sync_plan`` estimate, so a
        failed read leaves every unit's fields ``None`` (the frontend falls
        back to raw ``rom_count`` weights) rather than failing the plan.
        """
        if not platform_units:
            return platform_units
        try:
            estimates = await self._loop.run_in_executor(None, self._read_plan_estimates, platform_units)
        except Exception as e:
            self._logger.warning(f"Plan-time skip-estimate read failed, using raw ROM counts: {e}")
            return platform_units
        return [
            replace(
                unit,
                predicted_skip=estimates[unit.slug].predicted_skip,
                collapsed_count=estimates[unit.slug].collapsed_count,
                bound_count=estimates[unit.slug].bound_count,
                new_shortcut_count=estimates[unit.slug].new_shortcut_count,
            )
            if unit.slug in estimates
            else unit
            for unit in platform_units
        ]

    async def _attach_collection_bound_counts(self, collection_units: list[WorkUnit]) -> list[WorkUnit]:
        """Stamp each collection unit with its ``bound_count`` estimate rider (#1511).

        Fail-open like the platform sibling: a failed read leaves the field
        ``None``, so the frontend prices the unit exactly as it did before the
        rider existed rather than the plan failing.
        """
        if not collection_units:
            return collection_units
        try:
            counts = await self._loop.run_in_executor(None, self._read_collection_bound_counts, collection_units)
        except Exception as e:
            self._logger.warning(f"Plan-time collection bound-count read failed, pricing as creates: {e}")
            return collection_units
        return [
            replace(unit, bound_count=counts[key]) if (key := (str(unit.id), unit.collection_kind)) in counts else unit
            for unit in collection_units
        ]

    def _read_collection_bound_counts(self, units: list[WorkUnit]) -> dict[tuple[str, str | None], int]:
        """Bound-member count per stamped collection, keyed ``(id, kind)`` (#1511).

        A collection has no local membership column — membership lives on the
        server — so the count comes from the completion stamp's
        ``member_rom_ids`` (the same stored member set the skip replays),
        counting those whose ``roms`` row carries a ``shortcut_app_id``. No ROM
        fetch, one short read UoW for every collection unit at once.

        Deliberately ASYMMETRIC with the platform rider: a platform with no
        persisted rows reports ``0`` (real knowledge — nothing is mirrored, so
        every item is a create), whereas an unstamped collection is omitted
        entirely. A collection's member set exists ONLY in its stamp, so without
        one there is no membership to count; virtual collections are never
        stampable at all (``CollectionSyncState.stamp`` accepts only
        ``user``/``smart``). Reporting ``0`` there would claim knowledge we do
        not have. Absent and ``0`` price identically today — both read as
        all-creates — but the distinction keeps the field honest for any later
        consumer. Do not "simplify" this into consistency with the platform side.

        Consequence worth knowing before re-tuning the estimate: ``clear_sync_cache``
        clears ``collection_sync_state`` wholesale, so a **Force Full Sync** leaves
        every collection unstamped and its units revert to create pricing for that
        run — even though the shortcuts themselves survive. Platform units are
        unaffected (their bound count reads the rows directly, no stamp gate). The
        estimate reads long, never short, which is the safe direction.

        The member set may be STALE (membership can have changed since the
        stamp). That is accepted and bounded: estimate-only (ADR-0023), and a
        freshness probe would mean network I/O at plan time.
        """
        counts: dict[tuple[str, str | None], int] = {}
        with self._uow_factory() as uow:
            for unit in units:
                if unit.collection_kind is None:
                    continue
                stamp = uow.collection_sync_state.get(str(unit.id), unit.collection_kind)
                if stamp is None:
                    continue
                counts[(str(unit.id), unit.collection_kind)] = sum(
                    1
                    for rom_id in stamp.member_rom_ids
                    if (rom := uow.roms.get(rom_id)) is not None and rom.shortcut_app_id is not None
                )
        return counts

    def _read_plan_estimates(self, units: list[WorkUnit]) -> dict[str, _PlanEstimate]:
        """Read the plan-time estimate baseline for platform units (#1382).

        Per unit slug: replay the wholesale-skip gate's LOCAL conditions
        (``predict_unit_skip`` — stamp present, the stamped count and the count
        of rows carrying the stamp's fetch generation both match the server
        count, bound rows exist, no group-key backfill pending) and
        derive the persisted post-collapse shortcut count
        (``collapsed_shortcut_count`` over the rows' sibling-group keys +
        bound flags). The collapsed count is emitted ONLY for slugs that carry
        a ``PlatformSyncState`` completion stamp (#1412) — the same gate as
        ``_read_collapsed_counts``: the stamp exists iff the local mirror is
        complete, so without it a never-synced platform's PARTIAL rows
        (cross-platform collection siblings, ADR-0021) would mis-weight the ETA
        below the true work. ``None`` (no stamp, or no persisted rows) rides the
        payload absent, so the frontend weights the unit at its raw ``rom_count``
        (``predicted_skip ? 0 : collapsed_count ?? rom_count``). Also split the
        unit by what the apply will actually do to it: count its BOUND rows —
        those already carrying a ``shortcut_app_id`` — which the frontend
        prices at the cheap update rate rather than the create rate,
        so a re-sync of an already-mirrored platform stops reading as a fresh
        import, and count the shortcuts still to be MINTED
        (``new_shortcut_count``), which the frontend takes as its create term
        directly instead of subtracting the bound rows from the unit's weight.
        Neither is stamp-gated, unlike the collapsed count: both describe the
        rows and the server count as they stand, not the completeness of the
        mirror: a bound row genuinely has a Steam shortcut whether or not the
        platform's mirror is complete, and a ROM the mirror holds no row for
        genuinely has to be created. The gate's server-delta check (``list_roms_updated_after``) is
        deliberately NOT replayed — no network at plan time.

        A Force Full Sync clears every stamp before the run, so its plan predicts
        no skips AND drops every collapsed count, leaving the unit WEIGHED at the
        full pre-collapse ``rom_count``. Both composition counts survive that
        clear, which is what keeps the forced re-apply priced honestly: on a
        platform carrying sibling groups (ADR-0021) the raw ``rom_count`` exceeds
        the real shortcut count, so deriving creates by subtracting the bound rows
        from it would price every collapsed duplicate as a phantom new shortcut —
        at the dear create rate, plus a cover download it will never perform.
        ``new_shortcut_count`` reports no creates there instead, because the clear
        takes away the stamps and not the bindings, so no sibling group is left
        without one (#1517). One short read UoW for the whole plan.

        Estimate-ONLY (ADR-0023): the result rides the ``sync_plan`` payload
        and must never feed the actual skip decision —
        ``_try_unit_incremental_skip`` at fetch time remains the sole skip
        authority.
        """
        estimates: dict[str, _PlanEstimate] = {}
        with self._uow_factory() as uow:
            for unit in units:
                stamp = uow.platform_sync_state.get(unit.slug)
                all_rows = list(uow.roms.iter_by_platform(unit.slug))
                bound_count = sum(1 for rom in all_rows if rom.shortcut_app_id is not None)
                predicted = predict_unit_skip(
                    stamp_completed_at=stamp.completed_at if stamp is not None else None,
                    stamp_rom_count=stamp.rom_count if stamp is not None else None,
                    unit_rom_count=unit.rom_count,
                    # The same fetch-generation count the real gate uses (#1504),
                    # so the estimate keeps replaying the gate's local conditions.
                    fetched_count=count_rows_for_skip(all_rows, stamp.fetch_id if stamp is not None else None),
                    registry_count=bound_count,
                    needs_backfill=any(rom.sibling_group_key is None for rom in all_rows),
                )
                collapsed = (
                    collapsed_shortcut_count(
                        (rom.sibling_group_key, rom.shortcut_app_id is not None) for rom in all_rows
                    )
                    if stamp is not None and all_rows
                    else None
                )
                new_shortcuts = new_shortcut_count(
                    ((rom.sibling_group_key, rom.shortcut_app_id is not None) for rom in all_rows),
                    unit_rom_count=unit.rom_count,
                )
                estimates[unit.slug] = _PlanEstimate(predicted, collapsed, bound_count, new_shortcuts)
        return estimates

    def _read_collapsed_counts(self) -> dict[str, int]:
        """Persisted post-collapse shortcut count per platform slug, gated on the
        platform's completion stamp (#1382 / #1412).

        Groups every persisted ``roms`` row by ``platform_slug`` and collapses
        each platform's sibling-group keys + bound flags
        (``collapsed_shortcut_count``), but emits a count ONLY for slugs that
        currently carry a ``PlatformSyncState`` completion stamp (ADR-0023). The
        stamp exists iff the platform's local mirror is complete, which is
        exactly the condition under which a post-collapse count is meaningful:
        a never-synced platform legitimately holds PARTIAL rows — cross-platform
        collection siblings persist per ADR-0021 (e.g. favorited games leave
        their platform's rows behind without the platform ever being fetched) —
        so an ungated count would shadow the true server total (#1412). A slug
        with no stamp (or no persisted rows) is absent, so the caller leaves the
        field off and the frontend falls back to the raw server count. The stamp
        lookup shares the one short read UoW.
        """
        rows_by_slug: dict[str, list[tuple[str | None, bool]]] = {}
        with self._uow_factory() as uow:
            for rom in uow.roms.iter_all():
                rows_by_slug.setdefault(rom.platform_slug, []).append(
                    (rom.sibling_group_key, rom.shortcut_app_id is not None)
                )
            stamped = {slug for slug in rows_by_slug if uow.platform_sync_state.get(slug) is not None}
        return {slug: collapsed_shortcut_count(rows) for slug, rows in rows_by_slug.items() if slug in stamped}

    def _read_incremental_baseline(self, platform_slug: str) -> _SkipBaseline:
        """Read the incremental-skip baseline for *platform_slug* from SQLite.

        * ``stamp_completed_at`` / ``stamp_rom_count`` — the platform's
          completion stamp (``PlatformSyncState``), or ``None``/``None`` when
          there is no stamp. The stamp is the **sole** skip authority
          (ADR-0023): it exists iff the platform's most recent apply attempt
          ran to completion, a property no run-scoped ``last_sync`` can carry —
          a completed run says nothing about a platform whose shortcuts were
          later removed locally and only partially re-applied before a crash.
          No stamp means no skip, whatever the run history says.
        * ``reconstructed_roms`` — the platform's **bound** ``roms`` rows shaped
          like a RomM list response (thin — no ``metadatum``, so the skip-guard
          keeps them out of the metadata stamp). This is the shortcut set the
          skip reconstructs as the unit's ROMs.
        * ``fetched_count`` — the rows the skip may count against RomM's
          platform ``rom_count`` (``count_rows_for_skip``, #1504): those carrying
          the stamp's ``fetch_id`` generation, so a row for a rom_id the server
          dropped stays on disk (ADR-0007) without holding the platform below
          its server count forever. Bound and unbound rows count alike — only
          the generation decides. Falls back to every row for a stamp written
          before the generation contract.
        * ``persisted_count`` — every persisted row for the platform,
          superseded ones included. Reporting only: it makes the "N persisted,
          M from the last fetch" divergence visible in the log.
        * ``needs_backfill`` — any persisted row still carries a NULL
          ``sibling_group_key`` (predates the version-metadata capture), so the
          platform must full-fetch to fill it in.

        Only one short read UoW is opened.
        """
        with self._uow_factory() as uow:
            stamp = uow.platform_sync_state.get(platform_slug)
            all_rows = list(uow.roms.iter_by_platform(platform_slug))
        reconstructed = [
            {
                "id": rom.rom_id,
                "name": rom.name,
                "fs_name": rom.fs_name,
                "platform_slug": rom.platform_slug,
                "igdb_id": rom.igdb_id,
                "sgdb_id": rom.sgdb_id,
                "ra_id": rom.ra_id,
                "sibling_group_key": rom.sibling_group_key,
            }
            for rom in all_rows
            if rom.shortcut_app_id is not None
        ]
        return _SkipBaseline(
            stamp_completed_at=stamp.completed_at if stamp is not None else None,
            stamp_rom_count=stamp.rom_count if stamp is not None else None,
            reconstructed_roms=reconstructed,
            fetched_count=count_rows_for_skip(all_rows, stamp.fetch_id if stamp is not None else None),
            persisted_count=len(all_rows),
            needs_backfill=any(rom.sibling_group_key is None for rom in all_rows),
        )

    @staticmethod
    def _decorate_reconstructed(
        roms: list[dict[str, Any]], platform_name: str, platform_slug: str, platform_id: int
    ) -> list[dict[str, Any]]:
        """Stamp the live platform display name/slug/id onto reconstructed ROM dicts.

        ``platform_id`` is the unit's own platform id (every reconstructed row is
        this one platform's), stamped so a reconstructed dict is shaped like a live
        RomM fetch — the persisted ``sibling_group_key`` is already authoritative,
        but stamping the id keeps ``compute_sibling_group_key`` correct as a
        fallback rather than yielding ``…:None`` (#1296).
        """
        for rom in roms:
            rom["platform_name"] = platform_name
            rom["platform_slug"] = platform_slug
            rom["platform_display_name"] = platform_name
            rom["platform_id"] = platform_id
        return roms

    async def _try_unit_incremental_skip(self, unit: WorkUnit) -> list[dict[str, Any]] | None:
        """Per-unit incremental-skip pre-check for a platform unit.

        Returns the roms-reconstructed ROM list (the platform's bound rows =
        its shortcuts) when the platform is unchanged: the server reports zero
        rows updated after the platform's completion stamp AND the unit's
        ``rom_count`` matches the count of persisted rows carrying the stamp's
        fetch generation. The stamp (``PlatformSyncState``) is the **sole** skip
        authority — it exists iff the platform's most recent apply attempt ran to
        completion (cleared at apply start and by local removals, rewritten by the
        final chunk; ADR-0023). A completed-run ``last_sync`` is deliberately NOT
        a fallback: it cannot see a locally-removed-then-partially-reapplied
        platform, so trusting it can skip a platform with missing shortcuts.
        Group-aware sync persists every sibling (ADR-0021), so bound and unbound
        rows count alike — only the generation decides, which keeps skip parity on
        platforms holding sibling groups while excluding a row for a rom_id the
        server has since dropped (#1504; such a row is retained per ADR-0007 and
        would otherwise inflate the count forever). Returns ``None`` to fall
        through to a full paginated fetch — no stamp (including every platform's
        first sync after this contract shipped — a one-time re-walk), no rows
        carrying the stamp's generation, an un-backfilled row, a stamped ROM count
        that no longer matches the server, the delta check raised, or the server
        reports changes.

        This gate is the SOLE skip authority (ADR-0023). The plan-time
        ``predicted_skip`` rider (``_read_plan_estimates`` /
        ``domain/skip_prediction.py``) replays this gate's local conditions
        for the ``sync_plan`` estimate only and must never feed — and is
        never read by — this decision.
        """
        platform_name = unit.name
        platform_slug = unit.slug

        baseline = await self._loop.run_in_executor(None, self._read_incremental_baseline, platform_slug)
        stamp_completed_at = baseline.stamp_completed_at
        stamp_rom_count = baseline.stamp_rom_count
        reconstructed = baseline.reconstructed_roms
        fetched_count = baseline.fetched_count
        persisted_count = baseline.persisted_count
        needs_backfill = baseline.needs_backfill
        registry_count = len(reconstructed)

        if not stamp_completed_at or stamp_rom_count is None:
            self._logger.info(f"Per-unit fetch {platform_name}: no completion stamp — full fetch")
            return None
        if fetched_count == 0:
            return None

        # A skip's contract is "the local mirror already matches the server", so
        # it reconstructs the unit's ROMs from the bound rows. Zero bound rows
        # while rows persist means nothing is mirrored in Steam — e.g. after a
        # mass delete leaves unbind-only rows (ADR-0007) — so the reconstructed
        # list is empty and the diff sees nothing to re-add. Fall through to a
        # full fetch so the re-add path is fed the platform's ROMs again.
        if registry_count == 0:
            self._logger.info(
                f"Per-unit fetch {platform_name}: no bound shortcuts "
                f"({persisted_count} rows persisted, 0 bound) — full fetch"
            )
            return None

        # Version-metadata backfill (#1295 / #1296 / ADR-0021): a persisted ROM
        # whose sibling_group_key is still NULL predates the version-metadata
        # capture and must be re-fetched to fill it in (and to persist its
        # siblings). Skipping would leave it NULL forever, so any un-backfilled
        # ROM forces a full fetch — the commit then persists every sibling's
        # group key + version dimensions. Once every row carries a key this is a
        # no-op and the skip resumes.
        if needs_backfill:
            self._logger.info(f"Per-unit fetch {platform_name}: version-metadata backfill needed — full fetch")
            return None

        # Stamp-count guard (ADR-0023): the server ROM count captured at stamp
        # time must still equal the unit's current ``rom_count``. A server-side
        # count change since the stamp invalidates it — the platform must
        # re-fetch to reconcile.
        if stamp_rom_count != unit.rom_count:
            self._logger.info(
                f"Per-unit fetch {platform_name}: stamped rom_count {stamp_rom_count} "
                f"!= server {unit.rom_count} — full fetch"
            )
            return None

        try:
            # Typed ``object`` so the isinstance guard below is genuine
            # narrowing — the RomM API return type is a JSON-shape promise
            # the server can break.
            delta_resp: object = await self._loop.run_in_executor(
                None,
                self._romm_api.list_roms_updated_after,
                int(unit.id),
                stamp_completed_at,
                1,
                0,
            )
        except Exception as e:
            self._logger.warning(
                f"Per-unit incremental check failed for {platform_name}, falling back to full fetch: {e}"
            )
            return None

        # The row-count condition counts only the rows the last COMPLETE fetch
        # returned (#1504). Rows for rom_ids the server has since dropped stay on
        # disk as identity anchors (ADR-0007) but carry an older fetch generation,
        # so they no longer hold the platform below its server count forever.
        server_total = delta_resp.get("total", 0) if isinstance(delta_resp, dict) else 0
        if server_total == 0 and unit.rom_count == fetched_count:
            self._logger.info(
                f"Per-unit skip: {platform_name} unchanged "
                f"({fetched_count} ROMs from the last fetch, {registry_count} shortcuts"
                f"{self._superseded_note(persisted_count, fetched_count)})"
            )
            return self._decorate_reconstructed(reconstructed, platform_name, platform_slug, int(unit.id))

        self._logger.info(
            f"Per-unit fetch {platform_name}: {server_total} updated, "
            f"server={unit.rom_count} from-last-fetch={fetched_count} shortcuts={registry_count}"
            f"{self._superseded_note(persisted_count, fetched_count)} — full fetch"
        )
        return None

    @staticmethod
    def _superseded_note(persisted_count: int, fetched_count: int) -> str:
        """Name the rows the last fetch did not return, so the gap is visible in the log.

        Empty when every persisted row rode the last fetch; otherwise reports the
        rows the server has dropped, which are retained (ADR-0007) and no longer
        counted (#1504).
        """
        superseded = persisted_count - fetched_count
        return f", {superseded} superseded row(s) retained but not counted" if superseded > 0 else ""

    async def _emit_fetch_page_progress(
        self,
        *,
        unit_name: str,
        page: int,
        total_pages: int,
        progress_step: int,
        progress_total_steps: int,
    ) -> None:
        """Emit a throttled ``fetching`` progress frame for a paginated fetch.

        Called once per page; emits only on the first page and every
        ``_FETCH_PROGRESS_PAGE_INTERVAL``-th page so the frame rate stays
        bounded on a large multi-page fetch. ``progress_step`` /
        ``progress_total_steps`` are the run's coarse unit index / total so the
        main bar holds its position while the fine line advances by page; a
        falsy pair (the preview loop already emits its own per-unit frame,
        standalone callers) leaves the coarse bar indeterminate, as before.
        The frame carries the ``fetch`` sub-stage so the frontend fills the
        unit's fetch sub-slice of the bar (#1407). The displayed total is
        clamped to at least ``page`` so a server that grew since the listing
        never shows ``page 63/62``.
        """
        if page != 1 and page % _FETCH_PROGRESS_PAGE_INTERVAL != 0:
            return
        shown_total = max(total_pages, page)
        await self._emit_progress(
            SyncStage.FETCHING,
            current=page,
            total=shown_total,
            message=f"Fetching {unit_name} (page {page}/{shown_total})",
            step=progress_step,
            total_steps=progress_total_steps,
            sub_stage="fetch",
        )

    async def fetch_platform_unit(
        self, unit: WorkUnit, *, progress_step: int = 0, progress_total_steps: int = 0
    ) -> tuple[list[dict[str, Any]], bool]:
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

        ``progress_step`` / ``progress_total_steps`` are the run's coarse unit
        index / total, threaded through to the throttled per-page ``fetching``
        frames so the QAM bar keeps its position and narrates the paginated
        fetch (an incremental skip returns before any page, so it emits none).
        """
        if unit.type != "platform":
            raise ValueError(f"fetch_platform_unit called with non-platform unit type={unit.type}")

        skip_roms = await self._try_unit_incremental_skip(unit)
        if skip_roms is not None:
            return skip_roms, True

        platform_id = int(unit.id)
        platform_name = unit.name
        platform_slug = unit.slug

        unit_roms: list[dict[str, Any]] = []
        offset = 0
        limit = LIST_PAGE_SIZE
        total_pages = (unit.rom_count + limit - 1) // limit if unit.rom_count else 0
        page_num = 0
        while True:
            self._check_cancelling()
            page_num += 1
            await self._emit_fetch_page_progress(
                unit_name=platform_name,
                page=page_num,
                total_pages=total_pages,
                progress_step=progress_step,
                progress_total_steps=progress_total_steps,
            )
            try:
                # ``dict | list`` keeps the isinstance guard below genuine:
                # the paginated endpoint returns ``{"items": [...]}`` but the
                # else-branch tolerates a bare-list response shape.
                page: dict[str, Any] | list[dict[str, Any]] = await self._loop.run_in_executor(
                    None,
                    self._romm_api.list_roms,
                    platform_id,
                    limit,
                    offset,
                )
            except SyncCancelled:
                # A cooperative cancel signalled mid-pagination is NOT a fetch
                # failure — let it reach the orchestrator's ``except SyncCancelled``
                # untouched, never logged as an error. (In production the signal
                # is raised by ``_check_cancelling`` above, outside this ``try``;
                # this guard also covers a cancel raised from within ``list_roms``.)
                raise
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

    def _read_collection_stamp(self, collection_id: str, collection_kind: str) -> CollectionSyncState | None:
        """Read one collection's completion stamp in a short read UoW."""
        with self._uow_factory() as uow:
            return uow.collection_sync_state.get(collection_id, collection_kind)

    def _reconstruct_collection_members(
        self, member_rom_ids: list[int], already_synced: set[int]
    ) -> list[dict[str, Any]]:
        """Reconstruct the bound rows of a skipped collection's not-yet-covered members.

        A collection unit ordinarily returns as ``new_roms`` only its members NOT
        already in ``synced_rom_ids`` — its members on platforms this run did not
        fetch (a disabled platform, or a group edge). On a skip those rows come
        from the registry instead of a fetch, mirroring the platform skip's
        reconstruction (``_read_incremental_baseline``), so the preview union
        stays complete: a member bound in the registry but absent from the fetch
        would otherwise read as stale. A member already synced (its platform unit
        covered it) is skipped; an unbound / absent member is skipped too — it is
        not a shortcut to reconstruct (an unbound sibling maps to its group's
        bound representative at finalize, ADR-0021). One short read UoW.
        """
        with self._uow_factory() as uow:
            reconstructed: list[dict[str, Any]] = []
            for rid in member_rom_ids:
                if rid in already_synced:
                    continue
                rom = uow.roms.get(rid)
                if rom is None or rom.shortcut_app_id is None:
                    continue
                reconstructed.append(
                    {
                        "id": rom.rom_id,
                        "name": rom.name,
                        "fs_name": rom.fs_name,
                        "platform_slug": rom.platform_slug,
                        "platform_name": rom.platform_slug,
                        "igdb_id": rom.igdb_id,
                        "sgdb_id": rom.sgdb_id,
                        "ra_id": rom.ra_id,
                        "sibling_group_key": rom.sibling_group_key,
                    }
                )
            return reconstructed

    async def _try_collection_incremental_skip(self, unit: WorkUnit) -> list[int] | None:
        """Per-unit incremental-skip pre-check for a standard/smart collection unit.

        Returns the stamped member rom-id list when the collection is unchanged —
        the caller replays it into ``synced_rom_ids`` + the Steam-collection
        membership map without paginating. Returns ``None`` to fall through to a
        full paginated fetch. The collection sibling of
        :meth:`_try_unit_incremental_skip` (#742), gated on three verified RomM
        signals, ALL of which must agree with the ``CollectionSyncState`` stamp
        (ADR-0023):

        1. the collection's server ``updated_at`` still equals the stamp — RomM
           bumps it on any membership add/remove (and a smart-criteria edit), so
           an equal value is the membership-stable signal;
        2. a scoped ``updated_after`` probe (keyed off the stamp's ``completed_at``,
           our last sync time) reports zero rows — catches a member ROM's content
           change and a ROM entering a smart collection via its own metadata; and
        3. the stamp's ``rom_count`` still matches both the live listing count and
           the stored member set (a stamp written from a partial fetch is not
           trusted to reconstruct the whole membership).

        Only ``standard`` / ``smart`` collections are stampable — a virtual
        collection has no stable ``updated_at`` and always full-fetches. The probe
        exception (server error mid-check) falls open to a full fetch, mirroring
        the platform gate.
        """
        kind = unit.collection_kind
        if kind not in ("standard", "smart"):
            # Virtual collections carry no stamp — always full-fetch.
            return None
        if not unit.collection_updated_at:
            self._logger.info(f"Per-unit fetch {unit.name}: no collection updated_at — full fetch")
            return None

        collection_id = str(unit.id)
        stamp = await self._loop.run_in_executor(None, self._read_collection_stamp, collection_id, kind)
        if stamp is None:
            self._logger.info(f"Per-unit fetch {unit.name}: no completion stamp — full fetch")
            return None
        if stamp.updated_at != unit.collection_updated_at:
            self._logger.info(
                f"Per-unit fetch {unit.name}: collection updated_at changed "
                f"({stamp.updated_at!r} -> {unit.collection_updated_at!r}) — full fetch"
            )
            return None
        if stamp.rom_count != unit.rom_count:
            self._logger.info(
                f"Per-unit fetch {unit.name}: stamped rom_count {stamp.rom_count} "
                f"!= server {unit.rom_count} — full fetch"
            )
            return None
        if len(stamp.member_rom_ids) != unit.rom_count:
            # A stamp whose stored member set no longer matches the server count
            # can't be trusted to reconstruct the whole membership — full-fetch.
            self._logger.info(
                f"Per-unit fetch {unit.name}: stamped members {len(stamp.member_rom_ids)} "
                f"!= server {unit.rom_count} — full fetch"
            )
            return None

        try:
            # Typed ``object`` so the isinstance guard below is genuine narrowing —
            # the RomM API return type is a JSON-shape promise the server can break.
            #
            # Known limitation (rommapp/romm#3836): a RomM filesystem scan re-stamps
            # every member ROM's updated_at, so this probe reports > 0 and the skip
            # yields a full fetch after each nightly scan — the same limitation the
            # platform skip has. The design is correct regardless and becomes fully
            # effective once that upstream fix lands.
            delta_resp: object = await self._loop.run_in_executor(
                None,
                self._romm_api.list_collection_roms_updated_after,
                int(unit.id),
                kind,
                stamp.completed_at,
                1,
                0,
            )
        except Exception as e:
            self._logger.warning(
                f"Per-unit collection incremental check failed for {unit.name}, falling back to full fetch: {e}"
            )
            return None

        server_total = delta_resp.get("total", 0) if isinstance(delta_resp, dict) else 0
        if server_total == 0:
            self._logger.info(f"Per-unit skip: {unit.name} unchanged ({len(stamp.member_rom_ids)} members)")
            return list(stamp.member_rom_ids)

        self._logger.info(
            f"Per-unit fetch {unit.name}: {server_total} member(s) updated, "
            f"server={unit.rom_count} members={len(stamp.member_rom_ids)} — full fetch"
        )
        return None

    async def _fetch_collection_page(
        self, unit: WorkUnit, limit: int, offset: int
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Fetch one page of a collection unit's ROMs via its kind-specific endpoint.

        A ``virtual`` collection is a RomM virtual collection (string base64 id,
        the same endpoint for every virtual type), a ``smart`` collection a
        saved-search (int id), and the default a regular standard collection (int id) —
        each has its own list endpoint. ``dict | list`` keeps the caller's
        isinstance guard genuine: the paginated endpoints return ``{"items": [...]}``
        but a bare-list response shape is tolerated.
        """
        if unit.collection_kind == "virtual":
            return await self._loop.run_in_executor(
                None, self._romm_api.list_roms_by_virtual_collection, str(unit.id), limit, offset
            )
        if unit.collection_kind == "smart":
            return await self._loop.run_in_executor(
                None, self._romm_api.list_roms_by_smart_collection, int(unit.id), limit, offset
            )
        return await self._loop.run_in_executor(
            None, self._romm_api.list_roms_by_collection, int(unit.id), limit, offset
        )

    async def fetch_collection_unit(
        self, unit: WorkUnit, synced_rom_ids: set[int], *, progress_step: int = 0, progress_total_steps: int = 0
    ) -> tuple[list[dict[str, Any]], list[int], bool]:
        """Fetch ROMs for a single collection unit.

        Tries the incremental-skip path first: when the collection is unchanged
        (:meth:`_try_collection_incremental_skip`, #742), its membership is
        reconstructed from the stamp instead of paginated — no pages fetched
        beyond the ``limit=1`` probe.

        Mutates *synced_rom_ids* in place: every ROM seen via this
        collection (fetched or reconstructed) is added so subsequent units
        (and the final stale cleanup) treat them as covered.

        Returns ``(new_roms, all_collection_rom_ids, skipped)``:
          * ``new_roms`` — ROMs not already present in *synced_rom_ids*,
            decorated with platform_name/platform_slug for shortcut
            construction. On a skip these are reconstructed from the registry
            (the collection's members on platforms this run did not fetch), so
            the caller's union stays complete.
          * ``all_collection_rom_ids`` — every rom_id in the collection
            (including those already synced via a platform unit), used
            to build Steam collection memberships at the final phase.
          * ``skipped`` — True when the incremental check succeeded. The caller
            short-circuits the per-unit apply + commit branch (like the platform
            skip), keeping the reconstructed rows and membership for accounting.

        ``progress_step`` / ``progress_total_steps`` are the run's coarse unit
        index / total, threaded through to the throttled per-page ``fetching``
        frames so a large collection fetch narrates its progress like a
        platform fetch does.
        """
        if unit.type != "collection":
            raise ValueError(f"fetch_collection_unit called with non-collection unit type={unit.type}")

        skip_member_ids = await self._try_collection_incremental_skip(unit)
        if skip_member_ids is not None:
            # Reconstruct the not-yet-covered members from the registry BEFORE
            # marking them synced, so the preview union stays complete, then add
            # every member to synced_rom_ids so the stale cleanup treats them as
            # covered (a skipped collection applies nothing new).
            reconstructed = await self._loop.run_in_executor(
                None, self._reconstruct_collection_members, skip_member_ids, set(synced_rom_ids)
            )
            synced_rom_ids.update(skip_member_ids)
            return reconstructed, list(skip_member_ids), True

        new_roms: list[dict[str, Any]] = []
        all_collection_rom_ids: list[int] = []

        offset = 0
        limit = LIST_PAGE_SIZE
        total_pages = (unit.rom_count + limit - 1) // limit if unit.rom_count else 0
        page_num = 0
        while True:
            self._check_cancelling()
            page_num += 1
            await self._emit_fetch_page_progress(
                unit_name=unit.name,
                page=page_num,
                total_pages=total_pages,
                progress_step=progress_step,
                progress_total_steps=progress_total_steps,
            )
            page = await self._fetch_collection_page(unit, limit, offset)

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

        return new_roms, all_collection_rom_ids, False
