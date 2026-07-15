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
from typing import TYPE_CHECKING, Any

from domain.platform_prefs import materialize_enabled_platforms, resolve_sync_enabled
from domain.skip_prediction import collapsed_shortcut_count, predict_unit_skip
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

# Emit a ``fetching`` progress frame on the first page and every Nth page of a
# paginated unit fetch. At the 500-ROM page size a large platform paginates in
# only a handful of pages (a ~3000-ROM platform is 7), so every page is narrated
# (interval 1) — a "page 3/7" update every few seconds — rather than throttled.
# The interval knob stays so a future larger page count can throttle again.
_FETCH_PROGRESS_PAGE_INTERVAL = 1


def _collection_units(collections: list[dict[str, Any]], enabled_ids: set[str], kind: CollectionKind) -> list[WorkUnit]:
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
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _reason, _msg = classify_error(e)
            return {"success": False, "reason": _reason, "message": _msg}
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
    ) -> dict[str, Any] | None:
        """Fetch user collections and stamp the ``user`` bucket. Returns failure dict or None."""
        if scope not in (None, "my"):
            return None
        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _reason, _msg = classify_error(e)
            return {"success": False, "reason": _reason, "message": _msg}
        for c in user_collections:
            if scope == "my" and bool(c.get("is_favorite", False)):
                continue
            buckets["user"][str(c["id"])] = enabled
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

    async def _apply_franchise_bucket(
        self, *, buckets: dict[str, dict[str, bool]], enabled: bool, scope: str | None
    ) -> dict[str, Any] | None:
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
                _reason, _msg = classify_error(e)
                return {"success": False, "reason": _reason, "message": _msg}
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
        first, then user collections, then smart collections, then
        franchise collections) with ROM counts pulled from the listing
        endpoints. No ROMs are fetched here — the queue is a dispatch
        plan, not a payload. Platform units additionally carry the
        plan-time estimate riders (``predicted_skip`` / ``collapsed_count``,
        #1382) — estimate-only fields for the ``sync_plan`` payload that
        never feed the actual skip decision (ADR-0023).
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
            replace(unit, predicted_skip=estimates[unit.slug][0], collapsed_count=estimates[unit.slug][1])
            if unit.slug in estimates
            else unit
            for unit in platform_units
        ]

    def _read_plan_estimates(self, units: list[WorkUnit]) -> dict[str, tuple[bool, int | None]]:
        """Read the plan-time estimate baseline for platform units (#1382).

        Per unit slug: replay the wholesale-skip gate's LOCAL conditions
        (``predict_unit_skip`` — stamp present, stamped/persisted counts match
        the server count, bound rows exist, no group-key backfill pending) and
        derive the persisted post-collapse shortcut count
        (``collapsed_shortcut_count`` over the rows' sibling-group keys +
        bound flags; ``None`` when the platform has no persisted rows). The gate's
        server-delta check (``list_roms_updated_after``) is deliberately NOT
        replayed — no network at plan time. A Force Full Sync clears every
        stamp before the run, so its plan predicts no skips without a special
        case. One short read UoW for the whole plan.

        Estimate-ONLY (ADR-0023): the result rides the ``sync_plan`` payload
        and must never feed the actual skip decision —
        ``_try_unit_incremental_skip`` at fetch time remains the sole skip
        authority.
        """
        estimates: dict[str, tuple[bool, int | None]] = {}
        with self._uow_factory() as uow:
            for unit in units:
                stamp = uow.platform_sync_state.get(unit.slug)
                all_rows = list(uow.roms.iter_by_platform(unit.slug))
                predicted = predict_unit_skip(
                    stamp_completed_at=stamp.completed_at if stamp is not None else None,
                    stamp_rom_count=stamp.rom_count if stamp is not None else None,
                    unit_rom_count=unit.rom_count,
                    persisted_count=len(all_rows),
                    registry_count=sum(1 for rom in all_rows if rom.shortcut_app_id is not None),
                    needs_backfill=any(rom.sibling_group_key is None for rom in all_rows),
                )
                collapsed = (
                    collapsed_shortcut_count(
                        (rom.sibling_group_key, rom.shortcut_app_id is not None) for rom in all_rows
                    )
                    if all_rows
                    else None
                )
                estimates[unit.slug] = (predicted, collapsed)
        return estimates

    def _read_collapsed_counts(self) -> dict[str, int]:
        """Persisted post-collapse shortcut count per platform slug (#1382).

        Groups every persisted ``roms`` row by ``platform_slug`` and collapses
        each platform's sibling-group keys + bound flags
        (``collapsed_shortcut_count``). Slugs with no persisted rows are
        absent, so the caller leaves the field off and the frontend falls
        back to the raw server count. One short read UoW.
        """
        rows_by_slug: dict[str, list[tuple[str | None, bool]]] = {}
        with self._uow_factory() as uow:
            for rom in uow.roms.iter_all():
                rows_by_slug.setdefault(rom.platform_slug, []).append(
                    (rom.sibling_group_key, rom.shortcut_app_id is not None)
                )
        return {slug: collapsed_shortcut_count(rows) for slug, rows in rows_by_slug.items()}

    def _read_incremental_baseline(
        self, platform_slug: str
    ) -> tuple[str | None, int | None, list[dict[str, Any]], int, bool]:
        """Read the incremental-skip baseline for *platform_slug* from SQLite.

        Returns ``(stamp_completed_at, stamp_rom_count, reconstructed_roms,
        persisted_count, needs_backfill)``:

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
        * ``persisted_count`` — **all** persisted rows for the platform (bound +
          unbound siblings). Group-aware sync persists every sibling (ADR-0021),
          so this is what RomM's platform ``rom_count`` is compared against.
        * ``needs_backfill`` — any persisted row still carries a NULL
          ``sibling_group_key`` (predates the version-metadata capture), so the
          platform must full-fetch to fill it in.

        Only one short read UoW is opened.
        """
        with self._uow_factory() as uow:
            stamp = uow.platform_sync_state.get(platform_slug)
            all_rows = list(uow.roms.iter_by_platform(platform_slug))
        stamp_completed_at = stamp.completed_at if stamp is not None else None
        stamp_rom_count = stamp.rom_count if stamp is not None else None
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
        needs_backfill = any(rom.sibling_group_key is None for rom in all_rows)
        return stamp_completed_at, stamp_rom_count, reconstructed, len(all_rows), needs_backfill

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
        ``rom_count`` matches the count of ALL persisted rows for the platform.
        The stamp (``PlatformSyncState``) is the **sole** skip authority — it
        exists iff the platform's most recent apply attempt ran to completion
        (cleared at apply start and by local removals, rewritten by the final
        chunk; ADR-0023). A completed-run ``last_sync`` is deliberately NOT a
        fallback: it cannot see a locally-removed-then-partially-reapplied
        platform, so trusting it can skip a platform with missing shortcuts.
        Group-aware sync persists every sibling (ADR-0021), so the count compares
        against all persisted rows — not the bound representatives — restoring
        skip parity on platforms that hold sibling groups. Returns ``None`` to
        fall through to a full paginated fetch — no stamp (including every
        platform's first sync after this contract shipped — a one-time re-walk),
        no persisted rows, an un-backfilled row, a stamped ROM count that no
        longer matches the server, the delta check raised, or the server reports
        changes.

        This gate is the SOLE skip authority (ADR-0023). The plan-time
        ``predicted_skip`` rider (``_read_plan_estimates`` /
        ``domain/skip_prediction.py``) replays this gate's local conditions
        for the ``sync_plan`` estimate only and must never feed — and is
        never read by — this decision.
        """
        platform_name = unit.name
        platform_slug = unit.slug

        (
            stamp_completed_at,
            stamp_rom_count,
            reconstructed,
            persisted_count,
            needs_backfill,
        ) = await self._loop.run_in_executor(None, self._read_incremental_baseline, platform_slug)
        registry_count = len(reconstructed)

        if not stamp_completed_at or stamp_rom_count is None:
            self._logger.info(f"Per-unit fetch {platform_name}: no completion stamp — full fetch")
            return None
        if persisted_count == 0:
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

        server_total = delta_resp.get("total", 0) if isinstance(delta_resp, dict) else 0
        if server_total == 0 and unit.rom_count == persisted_count:
            self._logger.info(
                f"Per-unit skip: {platform_name} unchanged "
                f"({persisted_count} ROMs persisted, {registry_count} shortcuts)"
            )
            return self._decorate_reconstructed(reconstructed, platform_name, platform_slug, int(unit.id))

        self._logger.info(
            f"Per-unit fetch {platform_name}: {server_total} updated, "
            f"server={unit.rom_count} persisted={persisted_count} shortcuts={registry_count} — full fetch"
        )
        return None

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
        The displayed total is clamped to at least ``page`` so a server that
        grew since the listing never shows ``page 63/62``.
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

    async def _fetch_collection_page(
        self, unit: WorkUnit, limit: int, offset: int
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Fetch one page of a collection unit's ROMs via its kind-specific endpoint.

        A ``franchise`` collection is a RomM *virtual* collection (string id), a
        ``smart`` collection a saved-search (int id), and the default a regular
        user collection (int id) — each has its own list endpoint. ``dict | list``
        keeps the caller's isinstance guard genuine: the paginated endpoints return
        ``{"items": [...]}`` but a bare-list response shape is tolerated.
        """
        if unit.collection_kind == "franchise":
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
    ) -> tuple[list[dict[str, Any]], list[int]]:
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

        ``progress_step`` / ``progress_total_steps`` are the run's coarse unit
        index / total, threaded through to the throttled per-page ``fetching``
        frames so a large collection fetch narrates its progress like a
        platform fetch does.
        """
        if unit.type != "collection":
            raise ValueError(f"fetch_collection_unit called with non-collection unit type={unit.type}")

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

        return new_roms, all_collection_rom_ids
