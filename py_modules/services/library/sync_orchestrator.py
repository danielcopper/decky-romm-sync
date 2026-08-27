"""Preview / apply / per-unit sync lifecycle and the heartbeat clock.

Owns every async path the user triggers from the QAM that mutates in-flight
sync state: starting and cancelling syncs, computing a preview (read-only),
and dispatching the per-unit sync pipeline on apply. The heartbeat clock —
refreshed on every progress emission and inspected by per-unit waits — lives
here too. Progress emission also lives here — sub-services that need to
surface progress receive the orchestrator's ``emit_progress`` callback
through their config. Anything that fetches ROMs belongs in
:class:`LibraryFetcher`; anything that finalises shortcuts after the apply
completes belongs in :class:`SyncReporter`; Steam's renderer memory belongs
in :class:`SessionBudgetMonitor`. Cached ``rom_metadata`` is written by the
reporter's per-unit commit (the same write UoW as the ``roms`` upsert), so
preview never persists metadata and an interrupted apply leaves only
already-committed units' metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.collection_sync_state import CollectionSyncState
from domain.cover_refresh import count_cover_refreshes
from domain.platform_sync_state import PlatformSyncState
from domain.preview_delta import PreviewDelta
from domain.session_budget import (
    CLIFF_KB,
    COVER_TRANSIENT_KB,
    EFFECTIVE_CEILING_KB,
    chunk_worst_cost_kb,
    post_run_advisory,
    session_memory_delta,
)
from domain.shortcut_data import EmulatorInvocation, build_shortcuts_data
from domain.sibling_group import compute_component_group_keys
from domain.sibling_resolution import AUTO_REGION
from domain.sync_chunking import build_unit_chunks, wire_shortcuts
from domain.sync_diff import (
    BIND_ROM_ID_KEY,
    classify_roms,
    collapse_sibling_groups,
    compute_collection_diff,
    compute_platform_collection_diff,
    select_stale_removals,
)
from domain.sync_run import SyncRun
from domain.sync_stage import SyncStage
from domain.sync_state import SyncCancelled
from lib.errors import classify_error
from lib.list_result import ErrorCode
from services.library._state import CollectionMembership
from services.library.session_budget import SYNC_PAUSED_BUDGET, SessionBudgetMonitor

if TYPE_CHECKING:
    import logging

    from domain.work_unit import WorkUnit
    from lib.late_binding import LateBinding
    from services.library._state import LibrarySyncStateBox
    from services.library.fetcher import LibraryFetcher
    from services.library.reporter import SyncReporter
    from services.protocols import (
        ActiveCoreReader,
        ArtworkManager,
        Clock,
        DiscResolver,
        EventEmitter,
        Sleeper,
        UnitOfWorkFactory,
        UuidGen,
    )


_SYNC_CANCELLED = "Sync cancelled"
# Terminal reason when the run died externally (heartbeat timeout — the
# frontend crashed or reloaded) rather than by the user's Cancel. Stored in
# ``sync_runs.error`` via ``mark_interrupted``; the status split lets the UI
# report "(interrupted)" instead of "(cancelled)" for a crash.
_SYNC_INTERRUPTED = "Sync interrupted (Steam UI stopped responding)"
_PREVIEW_MAX_AGE_SECONDS = 1800  # 30 minutes — preview snapshots stale beyond this

# Per-unit heartbeat-based timeout. If the frontend stops calling
# ``sync_heartbeat`` for this many seconds while the orchestrator is
# waiting for ``report_unit_results``, the wait is treated as a
# recoverable cancellation — the in-flight unit is dropped and the
# next sync resumes via the incremental-skip path.
_UNIT_HEARTBEAT_TIMEOUT_SEC = 60.0
# Polling cadence the wait loop uses while watching the heartbeat
# clock. Kept short so cancel propagation feels responsive without
# burning CPU.
_UNIT_WAIT_POLL_SEC = 1.0
# Emitted-shortcut count per apply chunk. A unit's emitted shortcuts are split
# into chunks of about this many entries, each emitted → acked → committed
# durably before the next, so a mid-unit CEF crash forfeits only the in-flight
# chunk. A chunk may overflow this to keep a sibling group whole (see
# :func:`domain.sync_chunking.build_unit_chunks`).
_APPLY_CHUNK_SIZE = 200


def _collection_membership_key(unit: WorkUnit) -> tuple[str, str]:
    """The collision-free accumulator key for a collection unit.

    ``(collection_kind, collection_id)`` — the same per-collection identity the
    #742 ``CollectionSyncState`` stamp keys off — so two collections that share a
    display NAME never collide in the finalize accumulator. The single builder
    keeps the real-sync and preview write paths (and the stamp read-back) on one
    key so they cannot drift (#1503).
    """
    return (str(unit.collection_kind), str(unit.id))


@dataclass(frozen=True)
class SyncOrchestratorConfig:
    """Frozen wiring bundle handed to ``SyncOrchestrator.__init__``.

    Holds runtime infrastructure (loop, logger), event emitter, the
    Clock/UuidGen/Sleeper test seams, the SQLite Unit-of-Work factory
    (the transactional seam over the ``roms`` / ``sync_runs`` repositories
    the lifecycle writes through), the plugin-dir reference for shortcut
    data construction, the shared
    :class:`LibrarySyncStateBox`, and two peer references the
    orchestrator drives at runtime: the :class:`LibraryFetcher` it
    delegates per-unit fetches to and an :class:`ArtworkManager` for the
    apply-phase artwork download. The ``reporter``
    field is a :class:`LateBinding` because :class:`LibraryService`
    constructs the orchestrator before the reporter exists; the façade
    plugs the reader in via ``set()`` once the reporter is built. The
    shared ``active_core`` resolver bakes each ROM's full active core (the
    per-game/per-platform deviation folded over the es_systems default)
    into ``launch_options`` at sync time, and the shared ``disc_resolver`` bakes
    each multi-disc ROM's selected disc (the persisted ``selected_disc`` pin) into
    the installed-launch path at sync time. The ``session_budget`` seam owns every
    renderer-heap reading and verdict: at each chunk boundary the orchestrator asks
    it whether applying the chunk would exhaust Steam's per-session heap budget, and
    the run pauses itself when it would (#1383).
    """

    settings: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    uow_factory: UnitOfWorkFactory
    sync_state_box: LibrarySyncStateBox
    fetcher: LibraryFetcher
    reporter: LateBinding[SyncReporter]
    artwork: ArtworkManager
    active_core: ActiveCoreReader
    disc_resolver: DiscResolver
    session_budget: SessionBudgetMonitor


@dataclass(frozen=True)
class FinalizeOutcome:
    """What ``_finalize_per_unit`` produces for the run's terminal phase.

    Carries the reporter's collection maps (the completed-run ``SyncRun`` write
    needs their keys) plus the two session-budget surfacing values computed at
    finalize (``interrupt_reason`` / ``restart_recommended``), so the orchestrator
    can persist the terminal ``SyncRun`` status FIRST and only THEN emit
    ``sync_complete`` from these — the "emit last" ordering that closes the
    emit-before-persist race (#39).
    """

    platform_app_ids: dict[str, list[int]]
    romm_collection_app_ids: dict[str, list[int]]
    interrupt_reason: str | None
    restart_recommended: bool


class SyncOrchestrator:
    """Preview/apply/full-sync lifecycle with cancellation + heartbeat safety."""

    def __init__(self, *, config: SyncOrchestratorConfig) -> None:
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._plugin_dir = config.plugin_dir
        self._emit = config.emit
        self._clock = config.clock
        self._uuid_gen = config.uuid_gen
        self._sleeper = config.sleeper
        self._uow_factory = config.uow_factory
        self._sync_state = config.sync_state_box
        self._fetcher = config.fetcher
        self._artwork = config.artwork
        self._reporter = config.reporter
        self._active_core = config.active_core
        self._disc_resolver = config.disc_resolver
        self._session_budget = config.session_budget

    # ── Sync control ─────────────────────────────────────────────

    def start_sync(self):
        box = self._sync_state
        run_id = self._uuid_gen.uuid4()
        if not box.try_begin_run(run_id):
            return {"success": False, "reason": "sync_in_progress", "message": "Sync already in progress"}
        box.sync_last_heartbeat = self._clock.monotonic()
        self._loop.create_task(self._do_sync_per_unit())
        return {"success": True, "message": "Sync started"}

    def cancel_sync(self, run_id=None):
        """Request cancellation of the active sync, scoped to *run_id*.

        A truthy *run_id* must match the active run's ``current_sync_id`` for
        the cancel to take effect — a cancel click meant for run N can land
        after run N finalized to IDLE and run N+1 started fresh, and an
        unscoped cancel would wrongly abort run N+1 (the bug #1198 fixes,
        mirroring the ack-path identity check ``_ack_matches_active_unit``).
        A falsy/``None`` *run_id* cancels **unconditionally** — legacy callers
        that pass no id, and the "no active run id yet" safety case — so cancel
        is never made less reliable.
        """
        box = self._sync_state
        self._logger.info(
            f"cancel_sync: run_id={run_id!r} active run={box.current_sync_id!r} state={box.sync_state.value}"
        )
        outcome = box.request_cancel(run_id)
        if outcome == "no_sync":
            return {"success": True, "message": "No sync in progress"}
        if outcome == "stale":
            self._logger.info(f"Ignoring stale cancel for run={run_id!r}; active run={box.current_sync_id!r}")
            return {"success": True, "message": "Cancel ignored (stale run)"}
        return {"success": True, "message": "Sync cancelling..."}

    def sync_heartbeat(self):
        """Called by frontend during shortcut application to refresh the per-unit heartbeat clock."""
        self._sync_state.sync_last_heartbeat = self._clock.monotonic()
        return {"success": True}

    def shutdown(self) -> None:
        """Request graceful shutdown — cancels sync if running."""
        self._sync_state.request_cancel()

    # ── Preview / Apply ──────────────────────────────────────────

    async def sync_preview(self):
        """Read-only preview: paginate every unit, classify, return the summary.

        Does NOT persist ``rom_metadata`` — the metadata stamp happens in
        the reporter's per-unit commit, after the frontend acknowledges
        shortcuts for that unit. Stamping during preview would persist the
        registry-reconstructed thin ROMs from the per-unit incremental-skip
        path, which carry no ``metadatum`` (#738).
        """
        box = self._sync_state
        run_id = self._uuid_gen.uuid4()
        if not box.try_begin_run(run_id):
            return {"success": False, "reason": "sync_in_progress", "message": "Sync already in progress"}
        box.sync_last_heartbeat = self._clock.monotonic()
        try:
            await self.emit_progress(SyncStage.DISCOVERING, message="Fetching platforms...")
            work_queue = await self._fetcher.build_work_queue()

            all_roms: list[dict[str, Any]] = []
            platform_rom_ids: set[int] = set()
            collection_memberships: dict[tuple[str, str], CollectionMembership] = {}
            synced_rom_ids: set[int] = set()

            total_units = len(work_queue)
            for unit_index, unit in enumerate(work_queue, 1):
                if box.is_cancelling():
                    raise SyncCancelled(_SYNC_CANCELLED)
                await self.emit_progress(
                    SyncStage.FETCHING,
                    current=len(all_roms),
                    message=f"Fetching {unit.name}... ({unit_index}/{total_units})",
                    step=unit_index,
                    total_steps=total_units,
                )
                await self._fetch_preview_unit(
                    unit,
                    all_roms,
                    platform_rom_ids,
                    synced_rom_ids,
                    collection_memberships,
                    progress_step=unit_index,
                    progress_total_steps=total_units,
                )

            installed_paths = await self._loop.run_in_executor(None, self._scan_installed_paths)
            core_overrides = await self._loop.run_in_executor(None, self._build_core_overrides, all_roms)
            # Stamp each fresh ROM's component sibling-group key before the build so
            # the collapse below groups games, not dumps. The preview union is a
            # complete view of every enabled platform's groups; the DB's persisted
            # keys seed a member edging into a resident sibling on a skipped platform.
            resident_keys = await self._loop.run_in_executor(None, self._read_resident_group_keys)
            self._stamp_component_group_keys(all_roms, resident_keys)
            shortcuts_data = build_shortcuts_data(all_roms, self._plugin_dir, installed_paths, core_overrides)
            platform_name_set = {u.name for u in work_queue if u.type == "platform"}
            slug_to_name = {u.slug: u.name for u in work_queue if u.type == "platform" and u.slug}
            registry, last_synced_platforms, last_synced_collections = await self._loop.run_in_executor(
                None, self._read_preview_baseline, slug_to_name
            )
            # Enabled platforms lacking a completion stamp (#1416): a
            # late-ack-recovered platform is complete but unstamped, so the
            # wholesale-skip gate full-fetches it forever and its run status
            # never heals. Counted here (side-effect-free read) so the preview
            # can still offer Apply on an otherwise-empty delta — the apply's
            # 0-delta empty final chunk re-writes the stamp and records a fresh
            # SyncRun (the one-time re-walk ADR-0023 intends).
            restamp_platform_count = await self._loop.run_in_executor(
                None, self._count_unstamped_platforms, set(slug_to_name)
            )
            # Collapse to one entry per sibling group (ADR-0021) so the preview
            # counts games, not individual dumps: a multi-version game becomes one
            # shortcut and its unbound siblings stop reading as perpetual "new".
            # The preview union is a COMPLETE view of every group (a group is
            # per-platform, and every enabled platform is fully fetched), which the
            # apply path's platform units match — so the preview counts can't
            # diverge from what those units produce (the #1292 bug class). A
            # collection apply unit is a partial view and only grandfathers, so it
            # never adds a shortcut the preview didn't already count.
            emitted = collapse_sibling_groups(
                shortcuts_data,
                registry,
                set(installed_paths),
                complete_group_view=True,
                preferred_region=self._settings.get("preferred_region", AUTO_REGION),
            )
            new, changed, unchanged_ids, stale, disabled_count = classify_roms(
                emitted,
                registry,
                platform_name_set,
            )
            # Cover-only work (#1386): count the bound fetched ROMs whose server
            # cover fingerprint changed, with the SAME kernel the apply-path
            # invalidation pass refreshes by — an in-memory compare of the fetch
            # against the registry projection already read above (no extra DB
            # pass, no downloads; the preview stays side-effect-free). Runs over
            # the raw union (platform + collection units alike), not the collapsed
            # delta: covers are per-ROM, cover-blind classify_roms (ADR-0025)
            # stays untouched, and without this an empty shortcut delta would
            # short-circuit the apply and strand a changed cover forever.
            cover_refresh_count = count_cover_refreshes(all_roms, registry)

            # Final cancel checkpoint: a cancel can land after the unit loop's
            # last per-unit check but before the preview is staged. Re-check
            # here so a late cancel routes into the SyncCancelled branch (which
            # leaves ``pending_delta`` None) instead of staging a delta the
            # user already cancelled (#1202).
            if box.is_cancelling():
                raise SyncCancelled(_SYNC_CANCELLED)

            preview_id = self._uuid_gen.uuid4()
            platforms_count = sum(1 for u in work_queue if u.type == "platform")
            collections_count = sum(1 for u in work_queue if u.type == "collection")
            box.pending_delta = PreviewDelta(
                preview_id=preview_id,
                created_at=self._clock.time(),
                platforms_count=platforms_count,
                total_roms=len(all_roms),
            )

            await self.emit_progress(SyncStage.DONE, message="Preview ready", running=False)

            # Post-preview session-budget prognosis (#1383): warn up front when the
            # planned work would walk the renderer heap past the budget ceiling. The
            # gate is fail-open, so an unreadable renderer simply yields no warning.
            pause_likely = await self._session_budget.predict_pause_likely(
                new_items=len(new), changed_items=len(changed)
            )

            return {
                "success": True,
                "pause_likely": pause_likely,
                "summary": {
                    "new_count": len(new),
                    "changed_count": len(changed),
                    "unchanged_count": len(unchanged_ids),
                    "remove_count": len(stale),
                    "disabled_platform_remove_count": disabled_count,
                    # Bound ROMs whose server-side cover changed (#1386) — work the
                    # apply run performs even when the shortcut delta is empty, so
                    # the frontend must offer Apply on a cover-only preview instead
                    # of short-circuiting on "no changes". Additive: old consumers
                    # ignore it.
                    "cover_refresh_count": cover_refresh_count,
                    # Enabled platforms lacking a completion stamp (#1416) — a
                    # late-ack-recovered platform is complete but unstamped. Its
                    # apply is a 0-delta empty final chunk that re-writes the
                    # stamp and records a fresh SyncRun, so a restamp-only preview
                    # must still offer Apply instead of short-circuiting on "no
                    # changes". Additive: old consumers ignore it.
                    "restamp_platform_count": restamp_platform_count,
                    # Scope of the run (#29): how many platforms / collections this
                    # sync spans, shown as an always-on informational line
                    # independent of the change diffs.
                    "sync_platform_count": platforms_count,
                    "sync_collection_count": collections_count,
                    "collection_diff": compute_collection_diff(
                        {m.name for m in collection_memberships.values()},
                        last_synced_collections,
                    ),
                    "platform_collection_diff": compute_platform_collection_diff(
                        emitted,
                        platform_rom_ids,
                        last_synced_platforms,
                        self._settings.get("collection_create_platform_groups", False),
                    ),
                },
                "new_names": [s["name"] for s in new[:10]],
                "changed_names": [s["name"] for s in changed[:10]],
                "preview_id": preview_id,
            }
        except SyncCancelled:
            # sync_preview is a Decky callable — the frontend awaits its return.
            # Re-raising leaves that promise unsettled, so a user-initiated
            # cancel mid-preview returns the canonical failure shape instead of
            # propagating the cooperative cancel out of the callable (#1035).
            # SyncCancelled is a BaseException (not Exception), so it skips the
            # generic ``except Exception`` below and lands here as a distinct
            # cooperative signal — never conflated with a real asyncio cancel.
            box.pending_delta = None
            await self._finish_sync(_SYNC_CANCELLED)
            return {"success": False, "reason": "cancelled", "message": _SYNC_CANCELLED}
        except Exception as e:
            import traceback

            self._logger.error(f"Sync preview failed: {e}\n{traceback.format_exc()}")
            box.pending_delta = None
            _reason, _msg = classify_error(e)
            await self.emit_progress(SyncStage.ERROR, message=_msg, running=False)
            return {"success": False, "reason": _reason, "message": _msg}
        finally:
            box.finish_run(run_id)

    async def _fetch_preview_unit(
        self,
        unit: WorkUnit,
        all_roms: list[dict[str, Any]],
        platform_rom_ids: set[int],
        synced_rom_ids: set[int],
        collection_memberships: dict[tuple[str, str], CollectionMembership],
        *,
        progress_step: int = 0,
        progress_total_steps: int = 0,
    ) -> None:
        """Fetch one work unit's ROMs and fold them into the preview accumulators.

        Platform units add every ROM to ``platform_rom_ids`` and
        ``synced_rom_ids``; collection units record their full membership
        under a collision-free ``(collection_kind, collection_id)`` key (with the
        name in the value), so same-named collections never overwrite each other
        (#1503). ``all_roms`` is extended in both cases.
        Mutates the passed-in accumulators in place. ``progress_step`` /
        ``progress_total_steps`` thread the unit's coarse position into the
        fetcher's per-page ``fetching`` frames (on top of the per-unit frame
        the preview loop already emits).
        """
        if unit.type == "platform":
            unit_roms, _skipped = await self._fetcher.fetch_platform_unit(
                unit, progress_step=progress_step, progress_total_steps=progress_total_steps
            )
            for rom in unit_roms:
                platform_rom_ids.add(rom["id"])
                synced_rom_ids.add(rom["id"])
            all_roms.extend(unit_roms)
        else:
            unit_roms, all_collection_rom_ids, _skipped = await self._fetcher.fetch_collection_unit(
                unit, synced_rom_ids, progress_step=progress_step, progress_total_steps=progress_total_steps
            )
            if all_collection_rom_ids:
                collection_memberships[_collection_membership_key(unit)] = CollectionMembership(
                    name=unit.name,
                    rom_ids=all_collection_rom_ids,
                    kind=str(unit.collection_kind),
                    virtual_type=unit.virtual_type,
                )
            all_roms.extend(unit_roms)

    async def sync_apply_delta(self, preview_id):
        box = self._sync_state
        if not box.pending_delta or box.pending_delta.preview_id != preview_id:
            return {
                "success": False,
                "reason": ErrorCode.STALE_PREVIEW.value,
                "message": "Preview expired, please re-sync",
            }
        age = self._clock.time() - box.pending_delta.created_at
        if age > _PREVIEW_MAX_AGE_SECONDS:
            box.pending_delta = None
            return {
                "success": False,
                "reason": ErrorCode.STALE_PREVIEW.value,
                "message": "Preview is older than 30 minutes, please re-run sync",
            }
        # Admission guard: a rapid second apply (or an apply landing while a
        # sync is already in flight) must be rejected without consuming the
        # staged delta, so the still-valid preview survives for the legitimate
        # apply (#1202). Claim the run slot before nulling ``pending_delta``.
        run_id = self._uuid_gen.uuid4()
        if not box.try_begin_run(run_id):
            return {"success": False, "reason": "sync_in_progress", "message": "Sync already in progress"}
        box.pending_delta = None
        box.sync_last_heartbeat = self._clock.monotonic()

        self._loop.create_task(self._do_sync_per_unit())

        return {"success": True, "message": "Applying changes"}

    def sync_cancel_preview(self):
        self._sync_state.pending_delta = None
        return {"success": True}

    # ── Progress & safety ────────────────────────────────────────

    async def emit_progress(
        self, stage, current=0, total=0, message="", running=True, step=0, total_steps=0, sub_stage=""
    ):
        """Persist the progress snapshot and emit the sync_progress event.

        ``stage`` is a :class:`SyncStage` (or its string value); ``step``
        / ``total_steps`` are the coarse unit index / total units that
        drive the determinate main bar — stages without a unit index yet
        (discovering, fetching) pass ``0`` / ``0``, which the frontend
        treats as indeterminate. ``current`` / ``total`` are the fine
        within-unit counters. ``sub_stage`` discriminates the ``fetching``
        stage's phases — ``"fetch"`` (paginated ROM listing) vs ``"covers"``
        (cover download/refresh) — so the frontend can fill each phase's own
        monotonic sub-slice of the running unit's width (#1407); it is empty
        for every other frame. It rides the payload as the camelCase
        ``subStage`` key, matching the other multi-word snapshot keys
        (``totalSteps`` / ``runId``); the Python parameter stays snake_case.
        The snapshot is written to the box first so :meth:`get_sync_status`
        always returns the latest state even if the event never reaches a
        freshly remounted QAM — ``subStage`` therefore rides both the event
        and the remount re-seed.
        """
        self._sync_state.sync_progress = {
            "running": running,
            "stage": SyncStage(stage).value,
            "current": current,
            "total": total,
            "message": message,
            "step": step,
            "totalSteps": total_steps,
            "subStage": sub_stage,
            "runId": str(self._sync_state.current_sync_id or ""),
        }
        await self._emit("sync_progress", self._sync_state.sync_progress)

    def get_sync_status(self) -> dict[str, Any]:
        """Return the persisted progress snapshot — the authoritative sync state.

        Idle returns the default ``running: False`` snapshot; a live run
        returns the latest snapshot written by :meth:`emit_progress`.
        """
        return self._sync_state.sync_progress

    # ── Sync termination ─────────────────────────────────────────

    async def _finish_sync(self, message):
        """Emit the terminal CANCELLED progress snapshot for the in-flight run.

        Emission only — the IDLE/None reset of the run-lifecycle pair is owned
        by the caller's ``finally: box.finish_run(run_id)`` so every run has a
        single, run-scoped termination point (#1202).
        """
        box = self._sync_state
        box.sync_progress = {
            "running": False,
            "stage": SyncStage.CANCELLED.value,
            "current": box.sync_progress.get("current", 0),
            "total": box.sync_progress.get("total", 0),
            "message": message,
            "step": box.sync_progress.get("step", 0),
            "totalSteps": box.sync_progress.get("totalSteps", 0),
            "runId": str(box.current_sync_id or ""),
        }
        await self._emit("sync_progress", box.sync_progress)
        self._logger.info(message)

    # ── Per-unit pipeline ────────────────────────────────────────

    async def _do_sync_per_unit(self):
        """Per-unit sync pipeline (Phase 0 + per-unit dispatch + finalize).

        Builds a work queue, opens a ``SyncRun`` for the planned counts,
        processes each platform/collection unit to completion (fetch ->
        shortcuts -> artwork -> apply -> per-unit ``roms`` + ``rom_metadata``
        commit) before moving on, then emits stale-removal + Steam-
        collection mappings + ``sync_complete`` at the end and writes the
        ``SyncRun``'s terminal status. Each completed unit is a crash-safe
        checkpoint: the reporter's per-unit commit writes the ``roms`` row
        and its cached metadata in one write UoW (Rom first, metadata
        second — FK-safe), so a ROM and its metadata are always consistent
        across a crash.
        """
        box = self._sync_state
        # Cross-unit accumulators — built up unit-by-unit, consumed by the
        # final phase. ``synced_rom_ids`` is shared with collection units
        # for dedup. ``collection_memberships`` and ``platform_rom_ids``
        # feed the reporter's ``_build_collection_app_ids`` once every
        # unit has been applied. ``total_games_applied`` is the run's
        # processed-ROM count — wholesale skips, delta-skips, and committed
        # applies alike — surfaced as ``total_games`` (the terminal frame's
        # "N of M games processed" numerator).
        synced_rom_ids: set[int] = set()
        collection_memberships: dict[tuple[str, str], CollectionMembership] = {}
        platform_rom_ids: set[int] = set()
        total_games_applied = 0
        cancelled = False
        # Reset the per-run set of appIds the reporter binds (across both the
        # happy-path and late-ack commit paths). The stale scan excludes it so
        # a new rom_id reusing an old appId is never wrongly removed (#1036).
        box.committed_app_ids = set()
        box.run_interrupted = False
        # Session-budget gate run-scoped state (#1383): the paused flag, the
        # emitted-chunk counter (first chunk exempt → guaranteed forward progress),
        # and the distinct pause reason. The gate's own once-per-run measurement
        # latch is re-armed by the baseline stamp below, in the module that sets it.
        box.run_paused = False
        box.chunks_emitted_this_run = 0
        box.interrupt_reason = None
        # Run-scoped progress counters for the paused banner's "X of Y games done"
        # (#1383). The total is stamped at plan time below; the done count grows with
        # the skipped + committed work as the run proceeds.
        box.run_total_items = None
        box.run_done_items = 0
        # Re-arm the gate's measurement latch and stamp the run-start RSS baseline
        # for the last-run memory delta (#1383) — UNCONDITIONALLY, before any chunk is
        # applied, so even a fully-incremental-skip run reports an honest delta.
        await self._session_budget.record_run_start_baseline()
        # Capture the run id up front so the terminal SyncRun writes and the
        # ``finally`` reset below operate on a stable id for the lifetime of
        # this run. Every terminal IDLE/None reset for this run is collapsed
        # into the single ``finally: box.finish_run(run_id)`` — a run-scoped
        # compare-and-reset that no-ops if a fresher run already owns the slot,
        # so a rapid Sync/Cancel can't leave a half-reset run id (#1202).
        run_id = box.current_sync_id

        try:
            try:
                work_queue = await self._fetcher.build_work_queue()
            except asyncio.CancelledError:
                await self._finish_sync(_SYNC_CANCELLED)
                raise
            except Exception as e:
                self._logger.error(f"Failed to build work queue: {e}")
                _code, _msg = classify_error(e)
                await self.emit_progress(SyncStage.ERROR, message=_msg, running=False)
                return

            total_units = len(work_queue)
            total_roms_planned = sum(u.rom_count for u in work_queue)
            # Skip-aware estimate total (#1382): predicted-skip units weigh 0,
            # the rest their persisted collapsed count (raw ``rom_count``
            # fallback). Estimate-only — it prices the frontend's seeds and
            # never feeds the actual skip decision (ADR-0023); ``total_roms``
            # below stays the raw planned total for backward compatibility.
            total_estimated_items = sum(u.estimated_items() for u in work_queue)
            platforms_planned = sum(1 for u in work_queue if u.type == "platform")
            # Live ``platform_slug → display_name`` map from the work-queue;
            # threaded into finalize so collections key on display names and
            # the offline name cache stays current as of this sync.
            platform_names = {u.slug: u.name for u in work_queue if u.type == "platform" and u.slug}
            # The run's denominator for the paused banner's "X of Y games done" — the
            # same planned total the ``sync_plan`` event carries (#1383).
            box.run_total_items = total_roms_planned
            self._logger.info(f"Per-unit pipeline: {total_units} units planned, {total_roms_planned} ROMs total")
            await self._emit(
                "sync_plan",
                {
                    "run_id": str(run_id or ""),
                    "units": [u.to_event_payload() for u in work_queue],
                    "total_units": total_units,
                    "total_roms": total_roms_planned,
                    "total_estimated_items": total_estimated_items,
                },
            )

            if total_units == 0:
                # A zero-unit sync MUST NOT open or complete a ``SyncRun``:
                # an empty completed run would become ``get_latest_completed``
                # and reset the preview baseline (every platform would then
                # report as 'added') and the ``last_sync`` timestamp. Leaving
                # the prior completed run as the baseline matches the JSON era.
                await self.emit_progress(SyncStage.DONE, message="Nothing to sync", running=False)
                return

            # SyncRun.start — short write UoW for the planned counts.
            await self._loop.run_in_executor(None, self._open_sync_run, run_id, platforms_planned, total_roms_planned)

            try:
                for unit_index, unit in enumerate(work_queue):
                    if box.is_cancelling():
                        cancelled = True
                        break

                    applied = await self._sync_one_unit(
                        unit,
                        unit_index=unit_index,
                        total_units=total_units,
                        synced_rom_ids=synced_rom_ids,
                        collection_memberships=collection_memberships,
                        platform_rom_ids=platform_rom_ids,
                    )
                    total_games_applied += applied

                    if box.is_cancelling():
                        cancelled = True
                        break
            except SyncCancelled:
                # A cooperative cancel delivered mid-fetch (fetcher._check_cancelling
                # raised SyncCancelled inside _sync_one_unit) bypasses the
                # is_cancelling() checkpoints. Route it into the same graceful
                # finalize the checkpoint break uses, so the SyncRun is marked
                # cancelled and sync_state is restored to IDLE instead of wedging
                # until a plugin reload (#1035). SyncCancelled is a BaseException,
                # so a REAL asyncio.CancelledError raised mid-fetch is NOT caught
                # here — it propagates out, never swallowed into the finalize.
                cancelled = True

            # Final phase: stale cleanup + Steam collections + sync_complete.
            # Surface a non-terminal finalizing snapshot before the terminal
            # done/cancelled emit so the bar stays full while the reporter
            # commits collections. Cancelled runs skip it — their next emit
            # is the terminal cancelled snapshot from the reporter.
            if not cancelled:
                await self.emit_progress(
                    SyncStage.FINALIZING,
                    message="Finalizing…",
                    step=total_units,
                    total_steps=total_units,
                )
            outcome = await self._finalize_per_unit(
                synced_rom_ids=synced_rom_ids,
                collection_memberships=collection_memberships,
                platform_rom_ids=platform_rom_ids,
                platform_names=platform_names,
                cancelled=cancelled,
            )

            # SyncRun terminal status — short write UoW. Clean runs complete;
            # a stopped run records WHY, in priority order: a deliberate
            # session-budget ``paused`` (#1383) wins, then a heartbeat-timeout
            # ``interrupted`` (an external death — frontend crash/reload), else the
            # user's own ``cancelled``. The split keeps the UI from blaming a
            # self-imposed pause or a crash on the Cancel button.
            if cancelled:
                if box.run_paused:
                    await self._loop.run_in_executor(
                        None, self._mark_sync_run_paused, run_id, box.interrupt_reason or SYNC_PAUSED_BUDGET
                    )
                elif box.run_interrupted:
                    reason = box.interrupt_reason or _SYNC_INTERRUPTED
                    await self._loop.run_in_executor(None, self._mark_sync_run_interrupted, run_id, reason)
                else:
                    await self._loop.run_in_executor(None, self._mark_sync_run_cancelled, run_id, _SYNC_CANCELLED)
            else:
                await self._loop.run_in_executor(
                    None,
                    self._complete_sync_run,
                    run_id,
                    list(outcome.platform_app_ids.keys()),
                    list(outcome.romm_collection_app_ids.keys()),
                )

            # Emit the terminal signals LAST — only now that the terminal SyncRun
            # status is persisted, so a frontend stats refetch triggered by
            # ``sync_complete`` / the terminal progress frame reads the fresh run
            # status instead of racing the DB write (#39). The error path below never
            # reaches here, so it never double-emits sync_complete.
            await self._reporter.get().emit_sync_complete(
                platform_app_ids=outcome.platform_app_ids,
                romm_collection_app_ids=outcome.romm_collection_app_ids,
                total_games=total_games_applied,
                cancelled=cancelled,
                interrupt_reason=outcome.interrupt_reason,
                restart_recommended=outcome.restart_recommended,
            )
        except Exception as e:
            import traceback

            self._logger.error(f"Per-unit sync failed: {e}\n{traceback.format_exc()}")
            _code, _msg = classify_error(e)
            box.sync_progress = {
                "running": False,
                "stage": SyncStage.ERROR.value,
                "current": 0,
                "total": 0,
                "message": f"Sync failed — {_msg}",
                "step": 0,
                "totalSteps": 0,
                "runId": str(box.current_sync_id or ""),
            }
            self._loop.create_task(self._emit("sync_progress", box.sync_progress))
            # Use the captured ``run_id`` — ``_mark_sync_run_errored`` no-ops
            # gracefully on a falsy id (pre-``_open_sync_run`` failures, where
            # the run was never opened).
            await self._loop.run_in_executor(None, self._mark_sync_run_errored, run_id or box.current_sync_id, _msg)
        finally:
            # Single run-scoped termination point for every exit path (success,
            # cancel, error, zero-unit) — resets to IDLE only if ``run_id``
            # still owns the slot (#1202).
            box.finish_run(run_id)

    # ── SyncRun lifecycle (short write UoWs) ─────────────────────

    def _open_sync_run(self, run_id: str | None, platforms_planned: int, roms_planned: int) -> None:
        """Persist a fresh ``running`` SyncRun for the planned counts."""
        if not run_id:
            return
        run = SyncRun.start(
            id=run_id,
            at=self._clock.now().isoformat(),
            platforms_planned=platforms_planned,
            roms_planned=roms_planned,
        )
        with self._uow_factory() as uow:
            uow.sync_runs.save(run)

    def _complete_sync_run(self, run_id: str | None, platforms: list[str], collections: list[str]) -> None:
        """Transition the SyncRun to ``completed`` with its synced platform/collection names."""
        self._terminate_sync_run(
            run_id, lambda run: run.complete(self._clock.now().isoformat(), platforms, collections)
        )

    def _mark_sync_run_cancelled(self, run_id: str | None, reason: str) -> None:
        """Transition the SyncRun to ``cancelled``."""
        self._terminate_sync_run(run_id, lambda run: run.mark_cancelled(self._clock.now().isoformat(), reason))

    def _mark_sync_run_interrupted(self, run_id: str | None, reason: str) -> None:
        """Transition the SyncRun to ``interrupted`` (external death, not user cancel)."""
        self._terminate_sync_run(run_id, lambda run: run.mark_interrupted(self._clock.now().isoformat(), reason))

    def _mark_sync_run_paused(self, run_id: str | None, reason: str) -> None:
        """Transition the SyncRun to ``paused`` (a deliberate session-budget gate stop)."""
        self._terminate_sync_run(run_id, lambda run: run.mark_paused(self._clock.now().isoformat(), reason))

    def _mark_sync_run_errored(self, run_id: str | None, error: str) -> None:
        """Transition the SyncRun to ``errored``."""
        self._terminate_sync_run(run_id, lambda run: run.mark_errored(self._clock.now().isoformat(), error))

    def _terminate_sync_run(self, run_id: str | None, transition) -> None:
        """Load the SyncRun, apply *transition*, and save it in one write UoW.

        No-op when the run is absent (never opened) or already terminal —
        the per-run lifecycle is single-shot, so a double-terminal call
        (e.g. an exception after a cancel) is silently dropped.
        """
        if not run_id:
            return
        with self._uow_factory() as uow:
            run = uow.sync_runs.get(run_id)
            if run is None or run.status != "running":
                return
            transition(run)
            uow.sync_runs.save(run)

    def _read_preview_baseline(
        self, slug_to_name: dict[str, str]
    ) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
        """Read the classify baseline from SQLite in one short read UoW.

        Returns ``(registry, last_synced_platforms, last_synced_collections)``
        where ``registry`` is the ``classify_roms``-shaped dict (keyed by
        ``str(rom_id)``) reconstructed from the bound ``roms`` rows, with
        the platform display name resolved from *slug_to_name* (the live
        work-queue) and falling back to the slug. Each entry also carries the
        persisted ``cover_source`` fingerprint so the preview's cover-work
        count (#1386) compares in memory against the same projection — no
        second DB pass. The last-synced platform/collection lists come from
        the newest completed ``SyncRun``.
        """
        with self._uow_factory() as uow:
            registry: dict[str, dict[str, Any]] = {}
            for rom in uow.roms.iter_all():
                if rom.shortcut_app_id is None:
                    continue
                registry[str(rom.rom_id)] = {
                    "app_id": rom.shortcut_app_id,
                    "name": rom.name,
                    "fs_name": rom.fs_name,
                    "platform_name": slug_to_name.get(rom.platform_slug, rom.platform_slug),
                    "platform_slug": rom.platform_slug,
                    "sibling_group_key": rom.sibling_group_key,
                    "applied_launch_options": rom.applied_launch_options,
                    "cover_source": rom.cover_source,
                }
            latest = uow.sync_runs.get_latest_completed()
            last_platforms = list(latest.platforms_completed or []) if latest is not None else []
            last_collections = list(latest.collections_completed or []) if latest is not None else []
        return registry, last_platforms, last_collections

    def _count_unstamped_platforms(self, platform_slugs: set[str]) -> int:
        """Count enabled platform slugs without a ``PlatformSyncState`` stamp.

        A platform lacking a completion stamp has no wholesale-skip authority —
        ``LibraryFetcher._try_unit_incremental_skip`` full-fetches it — so its
        apply runs even at a zero shortcut delta and the empty final chunk
        re-writes the stamp (the one-time re-walk ADR-0023 intends after a
        late-ack recovery leaves a platform complete-but-unstamped). Surfaced as
        the preview's ``restamp_platform_count`` so the frontend still offers
        Apply on an otherwise-empty delta (#1416). One short read UoW.
        """
        with self._uow_factory() as uow:
            return sum(1 for slug in platform_slugs if uow.platform_sync_state.get(slug) is None)

    def _read_apply_registry(self, unit: WorkUnit) -> dict[str, dict[str, Any]]:
        """Read the bound-row registry the per-unit group collapse diffs against.

        Platform units scope to their own platform's rows (a sibling group is
        per-platform, so a vanished bound sibling shares the platform); collection
        units read the whole registry since their ROMs span platforms. A platform
        unit's fetch is therefore a COMPLETE view of every group it touches (the
        collapse may rebind); a collection unit's fetch is a PARTIAL view — the
        whole registry surfaces bindings for groups the unit only partly fetched,
        so the collapse must not treat those as vanished (it passes
        ``complete_group_view=False`` and only grandfathers). Only bound rows (a
        live ``shortcut_app_id``) are returned — an unbound sibling is not a
        shortcut the collapse can grandfather or rebind. Each entry also carries
        the persisted ``cover_source`` fingerprint, so the cover-cache
        invalidation pass (#1386) scans against this same projection instead of
        per-ROM DB lookups. The apply path did not read the registry before
        group-aware sync (ADR-0021).
        """
        with self._uow_factory() as uow:
            rows = (
                uow.roms.iter_by_platform(unit.slug) if unit.type == "platform" and unit.slug else uow.roms.iter_all()
            )
            return {
                str(rom.rom_id): {
                    "app_id": rom.shortcut_app_id,
                    "name": rom.name,
                    "fs_name": rom.fs_name,
                    "platform_slug": rom.platform_slug,
                    "sibling_group_key": rom.sibling_group_key,
                    "applied_launch_options": rom.applied_launch_options,
                    "cover_source": rom.cover_source,
                }
                for rom in rows
                if rom.shortcut_app_id is not None
            }

    def _read_resident_group_keys(self) -> dict[int, str]:
        """Read every persisted non-null ``sibling_group_key`` (``rom_id → key``).

        The preview builds one shortcut set over every enabled platform, so it
        needs the DB's canonical summaries for a fresh member that edges into a
        sibling on a skipped (incremental) platform, which the preview reconstructs
        only its bound rows of. One short read UoW.
        """
        with self._uow_factory() as uow:
            return {
                rom.rom_id: rom.sibling_group_key for rom in uow.roms.iter_all() if rom.sibling_group_key is not None
            }

    def _stamp_component_group_keys(self, roms: list[dict[str, Any]], resident_keys: dict[int, str]) -> None:
        """Stamp each fresh ROM's component sibling-group key onto its raw dict.

        Delegates the whole decision to :func:`compute_component_group_keys` (the
        pure kernel: union-find over ``sibling_roms`` edges + canonical-source
        agreement) and writes the result back so the downstream
        :func:`build_shortcuts_data` / group collapse read the component key rather
        than a per-ROM coalesce-first key. A resident dict (one that already carries
        a key — an incremental-reconstructed row) is left untouched: the kernel
        returns no key for it. Mutates *roms* in place, matching the other in-place
        decorations (``platform_name``, popped ``files``) the fetch already applied.
        """
        keys = compute_component_group_keys(roms, resident_keys)
        for rom in roms:
            key = keys.get(int(rom["id"]))
            if key is not None:
                rom["sibling_group_key"] = key

    def _clear_platform_stamp_io(self, platform_slug: str) -> None:
        """Delete *platform_slug*'s completion stamp in one short write UoW.

        Called at a platform unit's apply start (ADR-0023 / #1025): the stamp
        asserts "this platform's last apply completed", so a fresh apply must
        drop it up front and let the final chunk re-write it only on a clean
        finish. A no-op when no stamp exists.
        """
        with self._uow_factory() as uow:
            uow.platform_sync_state.delete(platform_slug)

    async def _sync_one_unit(
        self,
        unit: WorkUnit,
        *,
        unit_index: int,
        total_units: int,
        synced_rom_ids: set[int],
        collection_memberships: dict[tuple[str, str], CollectionMembership],
        platform_rom_ids: set[int],
    ) -> int:
        """Process one work unit start-to-finish; return its processed-ROM count.

        The ROMs for the unit come from a live per-unit fetch. After the
        frontend acks the unit's shortcuts (via ``report_unit_results``),
        the reporter commits the unit: it upserts each acked ROM into the
        ``roms`` aggregate and stamps the ROM's cached ``rom_metadata`` in
        the same write UoW (Rom row first, metadata second — FK-safe), so
        a ROM and its metadata land atomically.

        When the fetcher reports ``skipped=True`` (registry already
        matches the server-side platform state), the entire apply +
        commit branch is short-circuited: no frontend roundtrip, no
        registry write. The unit's ROMs still count toward the
        ``total_games_applied`` total returned to the user. Delta-skipped
        entries (content-unchanged ROMs the delta-restricted apply never
        re-emits) count the same way: the return value is the unit's
        processed ROMs — skips plus acked applies — not just the
        shortcuts written.
        """
        box = self._sync_state
        # Coarse anchor for the unit: FETCHING (not APPLYING) so the whole
        # per-unit prep phase — the paginated fetch + cover download below —
        # reads as "Fetching library". The label flips to "Applying shortcuts"
        # exactly once, when the chunk loop starts (frontend-driven), instead of
        # showing a frozen "Applying shortcuts" bar for the minutes-long fetch.
        await self.emit_progress(
            SyncStage.FETCHING,
            message=f"Fetching {unit.name}",
            step=unit_index + 1,
            total_steps=total_units,
        )

        # Fetch this unit's ROMs. Platform units may incremental-skip;
        # collection units always paginate (collection membership is
        # the source of truth, no per-collection "last_sync" gate today).
        # The unit's coarse step/total is threaded so the fetcher's per-page
        # ``fetching`` frames keep the bar's position.
        if unit.type == "platform":
            unit_roms, skipped = await self._sync_platform_unit(
                unit,
                synced_rom_ids=synced_rom_ids,
                platform_rom_ids=platform_rom_ids,
                progress_step=unit_index + 1,
                progress_total_steps=total_units,
            )
        else:
            unit_roms, skipped = await self._sync_collection_unit(
                unit,
                synced_rom_ids=synced_rom_ids,
                collection_memberships=collection_memberships,
                progress_step=unit_index + 1,
                progress_total_steps=total_units,
            )

        if box.is_cancelling():
            return 0

        # Per-unit incremental skip: registry already matches the
        # server-side state for this platform, so neither apply nor
        # commit have any work. Skip the frontend roundtrip and the
        # no-op two-phase commit. Force Full Sync clears ``last_sync``
        # upstream, so ``skipped`` is always False on forced runs.
        if skipped:
            self._logger.info(f"Per-unit apply skipped: {unit.name} ({len(unit_roms)} ROMs unchanged)")
            # A wholesale-skipped unit's ROMs are already correct in Steam, so they
            # count toward the run's done total — the resume of a paused run skips
            # every platform it finished before the pause, and those games ARE done
            # (#1383).
            box.run_done_items += len(unit_roms)
            return len(unit_roms)

        # Build shortcut data for every fetched ROM. Installed ROMs carry the
        # full launch command; uninstalled ROMs get an empty placeholder until
        # they are downloaded.
        installed_paths = await self._loop.run_in_executor(
            None, self._read_installed_paths, {rom["id"] for rom in unit_roms}
        )
        core_overrides = await self._loop.run_in_executor(None, self._build_core_overrides, unit_roms)

        # Read the bound-row registry once, before the build: its persisted keys
        # seed the component keying (a fresh member edging into a DB-resident
        # sibling adopts its canonical summary) AND drive the group collapse below.
        registry = await self._loop.run_in_executor(None, self._read_apply_registry, unit)
        resident_keys = {
            int(rom_id): entry["sibling_group_key"] for rom_id, entry in registry.items() if entry["sibling_group_key"]
        }
        self._stamp_component_group_keys(unit_roms, resident_keys)
        shortcuts_data = build_shortcuts_data(unit_roms, self._plugin_dir, installed_paths, core_overrides)

        # Collapse to one Steam shortcut per sibling group (ADR-0021): only the
        # representative (plus any grandfathered bound siblings) is emitted; a
        # rebinding group's entry is keyed to its vanished sibling so the
        # frontend reuses that shortcut. A PLATFORM unit fetches a group's whole
        # membership (a group is per-platform), so it is a complete view and may
        # rebind; a COLLECTION unit is a partial view (it fetches only its
        # members, not a group's bound sibling on another/skipped platform) — the
        # collapse then only grandfathers, never rebinds a live binding onto an
        # uninstalled sibling (#1296).
        emitted = collapse_sibling_groups(
            shortcuts_data,
            registry,
            set(installed_paths),
            complete_group_view=(unit.type == "platform"),
            preferred_region=self._settings.get("preferred_region", AUTO_REGION),
        )

        # Delta-restricted apply (#1383 / #1382-M3): split the collapsed entries
        # against the recorded applied state and emit ONLY new + changed. An
        # unchanged entry — identity matches AND its built target launch_options
        # matches the recorded ``applied_launch_options`` — is already correct on
        # the shortcut, so it never reaches the frontend (no Set* walk, no confirm
        # poll). Its ``roms`` row still commits: chunking routes the skipped
        # groups' rows to chunk 0's leftover (sync_chunking), so DB work is never
        # dropped and the platform stamp still rides the final chunk. Rebind
        # entries are force-kept even when classify calls them "unchanged" — they
        # MUST move their binding onto the surviving representative, and skipping
        # one would let the vanished sibling's shortcut go stale-removed instead of
        # rebound. ``new_ids`` prices each chunk create-vs-update for the budget
        # gate. Covers ride only the delta (covers are creates-only, #1391).
        new, changed, unchanged_ids, _stale, _disabled = classify_roms(emitted, registry, set())
        rebind_ids = {e["rom_id"] for e in emitted if BIND_ROM_ID_KEY in e}
        skip_ids = set(unchanged_ids) - rebind_ids
        new_ids = {e["rom_id"] for e in new}
        apply_emitted = [e for e in emitted if e["rom_id"] not in skip_ids]
        self._logger.info(
            f"Delta apply: {unit.name} — {len(new)} new + {len(changed)} changed + "
            f"{len(rebind_ids)} rebind of {len(emitted)} collapsed ({len(skip_ids)} unchanged skipped)"
        )
        # A skipped entry is already correct on its Steam shortcut — no Set* walk is
        # owed — so it counts as done the moment the delta is computed (#1383). It is
        # just as processed as a wholesale-skipped unit's ROMs, so every return below
        # also adds ``len(skip_ids)`` to the unit's processed count — keeping the
        # terminal frame's "N of M games processed" numerator (``total_games``)
        # consistent with this counter on a resumed run.
        box.run_done_items += len(skip_ids)

        # Cover-cache invalidation pass (#1386): before any cover is reused, refresh
        # the cache + grid copy of every BOUND fetched ROM whose server cover source
        # changed (delta-skipped ROMs included — covers otherwise ride only the
        # delta) and persist the fresh fingerprint. Runs BEFORE the cover download
        # below so a changed apply-emitted ROM downloads once here and the apply
        # path then reuses the refreshed cache. The returned {rom_id, app_id} list
        # rides the unit's first apply chunk so the frontend re-applies each cover
        # to the EXISTING shortcut via SetCustomArtworkForApp — without that push
        # the Steam tile stays stale in-session until a client restart.
        cover_refreshes = await self._refresh_changed_covers(
            unit_roms, registry, progress_step=unit_index + 1, progress_total_steps=total_units, label=unit.name
        )

        if box.is_cancelling():
            return len(skip_ids)

        # Download artwork for the shortcuts about to be applied and stamp each
        # delta entry's cover path in place (a no-op when the delta is empty).
        # Returns the confirmed cover fingerprints (rom_id → fresh source) the
        # per-unit commit persists onto the upserted rows (#1386).
        confirmed_cover_sources = await self._attach_unit_cover_paths(
            unit, unit_roms, apply_emitted, unit_index=unit_index, total_units=total_units
        )

        if box.is_cancelling():
            return len(skip_ids)

        # Clear this platform's completion stamp now that its apply is actually
        # beginning — the fetch succeeded, artwork is done, and the cancel guard
        # above has passed, so the next line starts emitting chunks. The stamp's
        # contract is "this platform's last apply ran to completion", which a
        # fresh apply invalidates the instant it starts: a crash / cancel /
        # heartbeat-timeout before the final chunk must leave NO stamp, so the
        # skip gate can't honour a stale one over a half-applied platform (the
        # #1025 silent-gap regression). The final chunk re-writes the stamp on a
        # clean finish. A fetch that failed raised before here (fetch failure ≠
        # apply started) and a cancel during fetch/artwork returned at a guard
        # above with the old stamp intact, so an unstarted apply keeps it. A
        # skipped platform returned before this point and keeps its stamp. Only
        # platform units carry a skip gate, so only they are stamped/cleared.
        # A dedicated short write UoW (not folded into the first chunk's commit)
        # so the clear is unconditional at apply start — even a first-chunk
        # heartbeat-timeout, whose late ack commits without the stamp, leaves no
        # stale stamp behind (ADR-0023 / #1025).
        if unit.type == "platform" and unit.slug:
            await self._loop.run_in_executor(None, self._clear_platform_stamp_io, unit.slug)

        # Stage the DELTA representatives for cover finalise + binding, and the
        # full built set for the ack-independent identity + version persist (the
        # reporter upserts a row for every sibling — skipped ones included — and
        # binds only the delta's acked representatives). Staging stays whole-unit;
        # the apply is chunked below, so a mid-unit CEF crash forfeits only the
        # in-flight chunk, not every prior chunk.
        box.pending_sync = {e["rom_id"]: e for e in apply_emitted}
        box.pending_all_roms = {sd["rom_id"]: sd for sd in shortcuts_data}
        box.pending_cover_sources = confirmed_cover_sources

        # A collection unit's final chunk stamps a CollectionSyncState over its
        # full membership (the accumulator just populated by the fetch, #742); a
        # platform unit passes None. The full set — not just the applied new_roms —
        # is what a future skip replays to rebuild membership. Read back by the
        # collision-free ``(collection_kind, collection_id)`` key, not the name, so
        # a same-named sibling collection can't shadow this unit's members (#1503).
        collection_member_ids = None
        if unit.type == "collection":
            membership = collection_memberships.get(_collection_membership_key(unit))
            collection_member_ids = membership.rom_ids if membership is not None else None

        applied_count = await self._apply_unit_in_chunks(
            unit,
            unit_index=unit_index,
            total_units=total_units,
            emitted=apply_emitted,
            shortcuts_data=shortcuts_data,
            unit_roms=unit_roms,
            new_ids=new_ids,
            cover_refreshes=cover_refreshes,
            collection_member_ids=collection_member_ids,
        )
        return len(skip_ids) + applied_count

    async def _attach_unit_cover_paths(
        self,
        unit: WorkUnit,
        unit_roms: list[dict[str, Any]],
        emitted: list[dict[str, Any]],
        *,
        unit_index: int,
        total_units: int,
    ) -> dict[int, str]:
        """Download artwork for the shortcuts about to be emitted and stamp each
        emitted entry's ``cover_path`` in place.

        Only the ROMs that actually get a shortcut (representatives + grandfathered
        siblings) are fetched — no eager covers for versions with no shortcut. A
        rebind entry pulls its cover from the representative it binds
        (``BIND_ROM_ID_KEY``), whose raw dict is the one present in *unit_roms*.
        A no-op when nothing is emitted.

        Returns the confirmed cover fingerprints — ``rom_id → applied cover
        source`` for every ROM whose cache the download resolved (fresh
        download, reuse, or grid seed all confirm; a failed download does not) —
        which the per-unit commit persists as ``roms.cover_source`` (#1386). The
        source is the one ArtworkService *actually* applied: the fresh
        ``path_cover`` normally, or the ROM's ``url_cover`` when the RomM asset
        404s and the external fallback wins (#1450), reported through the
        ``applied_sources`` accumulator.
        """
        if not emitted:
            return {}
        artwork_ids = {int(e.get(BIND_ROM_ID_KEY, e["rom_id"])) for e in emitted}
        artwork_roms = [rom for rom in unit_roms if rom["id"] in artwork_ids]
        applied_sources: dict[int, str] = {}
        cover_paths = await self._download_artwork(
            artwork_roms,
            progress_step=unit_index + 1,
            progress_total_steps=total_units,
            label=unit.name,
            applied_sources=applied_sources,
        )
        for e in emitted:
            e["cover_path"] = cover_paths.get(int(e.get(BIND_ROM_ID_KEY, e["rom_id"])), "")
        return {
            int(rom["id"]): applied_sources.get(int(rom["id"])) or source
            for rom in artwork_roms
            if int(rom["id"]) in cover_paths and (source := rom.get("path_cover_large") or rom.get("path_cover_small"))
        }

    async def _apply_unit_in_chunks(
        self,
        unit: WorkUnit,
        *,
        unit_index: int,
        total_units: int,
        emitted: list[dict[str, Any]],
        shortcuts_data: list[dict[str, Any]],
        unit_roms: list[dict[str, Any]],
        new_ids: set[int],
        cover_refreshes: list[dict[str, int]] | None = None,
        collection_member_ids: list[int] | None = None,
    ) -> int:
        """Emit → wait → commit the unit's DELTA shortcuts one durable chunk at a time.

        ``emitted`` is the delta (new + changed + rebind) the frontend applies;
        skipped-unchanged entries never reach here but their rows still ride the
        chunks' ``rom_ids`` (routed to chunk 0's leftover by ``build_unit_chunks``).
        The delta shortcuts are split into commit chunks processed one at a time
        (emit → wait → commit durably → next), so a mid-unit CEF crash forfeits
        only the in-flight chunk, not every prior chunk. ``chunk.rom_ids`` are the
        chunk's fetched ROMs (its sibling groups' rows, plus chunk 0's skipped
        leftover); a keyed lookup into the whole unit's live fetch yields the
        chunk's commit subset. ``new_ids`` (the classified creates) prices each
        chunk create-vs-update for the session-budget gate. ``cover_refreshes``
        (the #1386 invalidation pass's ``{rom_id, app_id}`` list) rides the
        unit's FIRST chunk payload, clipped to the budget headroom left after
        that chunk's own projected cost — a big refresh list degrades to fewer
        in-session tile refreshes (the grid files are already updated; a Steam
        restart shows the rest), never to a run pause. Returns the running
        count of shortcuts applied — a cancel or heartbeat timeout returns early
        with the chunks committed so far.
        """
        box = self._sync_state
        # One generation id per platform fetch. The run id serves: a platform is
        # fetched at most once per run, so it identifies this platform's fetch
        # uniquely, and every chunk of the unit shares it — unlike a clock reading,
        # which differs per chunk and would leave the earlier chunks' rows stamped
        # before the final chunk's completion stamp (#1504).
        fetch_id = str(box.current_sync_id or "") if unit.type == "platform" else None
        chunks = build_unit_chunks(emitted, shortcuts_data, _APPLY_CHUNK_SIZE)
        roms_by_id = {r["id"]: r for r in unit_roms if "id" in r}
        chunk_count = len(chunks)
        applied_count = 0
        for chunk_index, chunk in enumerate(chunks):
            # A cancel landing in the inter-chunk window — after the prior chunk's
            # commit but before this chunk's emit — discards the rest of the unit
            # here, before any per-chunk mutation or emit. Without this the
            # frontend would fully process another ~200-shortcut chunk (~2 min)
            # whose ack the backend then rejects, orphaning those shortcuts until
            # the next sync. Same cleanup as the mid-wait user-cancel branch.
            if box.is_cancelling():
                box.clear_active_unit()
                return applied_count

            # Session-budget gate (#1383): at every chunk boundary ask the monitor
            # whether applying this chunk would cross Steam's per-session heap budget,
            # and pause here — a clean chunk boundary — if it would. On pause the
            # gate sets ``run_paused`` + ``interrupt_reason`` and requests
            # cancel, so the check just below returns cleanly with the prior chunks
            # committed — the terminal finalize then records the resumable ``paused``
            # state (the deliberate sibling of a heartbeat timeout's
            # ``interrupted``). Both modes are PREDICTIVE (RSS plus this chunk's
            # worst-case cost) and differ only in the line the projection is
            # measured against:
            #  - Every LATER chunk projects against the effective ceiling
            #    (``cliff - margin`` ≈ 2.2 GB), keeping the anti-thrash safety margin.
            #  - The run's very FIRST chunk projects against the CLIFF itself
            #    (``CLIFF_KB`` ≈ 2.45 GB). Forward progress must be guaranteed — the
            #    run has to apply at least one chunk or it loops forever on a
            #    no-progress pause — so the first chunk is allowed to spend into the
            #    safety margin, but the predictive projection still stops it before
            #    the crash line. Net effect: a resume proceeds only when this chunk's
            #    worst-case peak stays below the cliff (≈ 1.95 GB for a full 200-item
            #    chunk of cover-applying creates, each priced create + cover) and can
            #    never be projected past it; at/above that it re-pauses with zero
            #    progress and the banner directs the user to restart Steam.
            # The chunk is priced by composition, so the gate needs its creates and
            # updates apart. The frontend decides create-vs-update itself via its
            # existing-shortcut scan; a small backend/frontend mismatch only ever
            # overprices (worst-case safe).
            creates = sum(1 for e in chunk.emitted if e["rom_id"] in new_ids)
            updates = len(chunk.emitted) - creates
            budget_limit_kb = CLIFF_KB if box.chunks_emitted_this_run == 0 else EFFECTIVE_CEILING_KB
            rss_kb = await self._session_budget.maybe_pause_for_budget(
                creates=creates, updates=updates, limit_kb=budget_limit_kb
            )
            if box.is_cancelling():
                box.clear_active_unit()
                return applied_count

            # The #1386 cover-refresh list rides the unit's FIRST chunk, clipped to
            # the budget headroom left after this chunk's own projected cost — the
            # refreshes must never be the reason a run pauses, so they degrade to
            # fewer in-session tile refreshes instead (grid files already updated).
            chunk_cover_refreshes: list[dict[str, int]] = []
            if chunk_index == 0 and cover_refreshes:
                chunk_cover_refreshes = self._clip_cover_refreshes(
                    cover_refreshes, rss_kb=rss_kb, creates=creates, updates=updates, limit_kb=budget_limit_kb
                )

            chunk_rows = [roms_by_id[rid] for rid in chunk.rom_ids if rid in roms_by_id]

            # Fresh per-chunk coordination: a new event + identity (run + unit +
            # chunk index) so the reporter validates each chunk's ack.
            box.unit_complete_event = asyncio.Event()
            box.last_unit_results = None
            box.active_unit_id = unit.id
            box.active_chunk_index = chunk_index
            box.sync_last_heartbeat = self._clock.monotonic()
            await self._emit(
                "sync_apply_unit",
                {
                    "run_id": str(box.current_sync_id or ""),
                    "unit_type": unit.type,
                    "unit_id": unit.id,
                    "unit_name": unit.name,
                    "unit_index": unit_index,
                    "total_units": total_units,
                    "chunk_index": chunk_index,
                    "chunk_count": chunk_count,
                    "chunk_offset": chunk.offset,
                    "unit_total": len(emitted),
                    # Strip backend-internal keys (staged cover path, rebind target)
                    # from the wire — the frontend fetches a created shortcut's cover
                    # via get_artwork_base64(rom_id); the commit reads them from
                    # pending_sync, which keeps the full entries.
                    "shortcuts": wire_shortcuts(chunk.emitted),
                    # Existing shortcuts whose server-side cover changed (#1386):
                    # the frontend re-applies each via SetCustomArtworkForApp so
                    # the tile refreshes in-session. Non-empty only on chunk 0.
                    "cover_refreshes": chunk_cover_refreshes,
                },
            )
            # Count this emit so the session-budget gate exempts only the very
            # first chunk of the run (forward-progress guarantee, #1383).
            box.chunks_emitted_this_run += 1

            applied = await self._wait_for_unit_complete(unit, box.unit_complete_event)
            if applied is None:
                # The wait gave up — the reason (user cancel vs heartbeat timeout)
                # decides whether this chunk's in-flight work is recoverable. Chunks
                # committed before this one stay committed either way.
                self._abandon_active_chunk(box, chunk_rows)
                return applied_count

            platform_stamp = self._build_final_platform_stamp(unit, chunk_index, chunk_count, fetch_id)
            collection_stamp = self._build_final_collection_stamp(unit, chunk_index, chunk_count, collection_member_ids)

            # Per-chunk commit: the reporter upserts every fetched ROM of this
            # chunk into the ``roms`` aggregate (identity + version metadata,
            # unbound for non-representatives) and binds only the acked
            # representatives, stamping each ROM's cached ``rom_metadata`` in the
            # same write UoW (Rom row first, metadata second — FK-safe).
            # ``chunk_rows`` is this chunk's slice of the live RomM fetch — the
            # source of ``metadatum`` — so each committed chunk is a crash-safe
            # checkpoint. The final-chunk ``platform_stamp`` / ``collection_stamp``
            # (whichever the unit type produces) rides the same UoW.
            await self._reporter.get().commit_unit_results(
                applied,
                chunk_rows,
                platform_stamp=platform_stamp,
                collection_stamp=collection_stamp,
                # The fetch generation for a PLATFORM unit's rows (#1504), passed on
                # EVERY chunk so the whole unit shares the generation the final
                # chunk's stamp records. A collection unit passes None — it spans
                # platforms, and re-marking a foreign platform's row would drop it
                # from that platform's counted rows.
                fetch_id=fetch_id,
            )
            applied_count += len(applied)
            # Only a COMMITTED chunk's items count as done (#1383): an emitted chunk
            # whose ack never landed — a cancel or a heartbeat timeout — returns above,
            # before this line, so the paused banner never over-reports.
            box.run_done_items += len(applied)

        box.clear_active_unit()
        return applied_count

    def _abandon_active_chunk(self, box: LibrarySyncStateBox, chunk_rows: list[dict[str, Any]]) -> None:
        """Tear down or stash the in-flight chunk after its wait gave up.

        A user cancel (box already CANCELLING) intentionally discards the chunk:
        drop the whole-unit staging, null the event, and clear the unit + chunk
        identity so a stray late ack can't commit it. A heartbeat timeout (box
        still RUNNING) instead stashes THIS chunk (its run/unit/chunk identity +
        rows) into ``abandoned_chunk`` via ``stash_abandoned_chunk`` — inert data
        that survives the run's teardown — while leaving the whole-unit staging
        live, so a late ``report_unit_results`` still commits the delivered
        bindings instead of leaving orphan shortcuts (#1052 / #1367). It then
        marks the run ``interrupted`` (the frontend went dark, not the user's
        Cancel — so the terminal SyncRun write records ``interrupted``) and
        requests the cancel that stops the chunk loop.
        """
        if box.is_cancelling():
            box.clear_active_unit()
        else:
            box.stash_abandoned_chunk(chunk_rows)
            box.run_interrupted = True
            box.request_cancel()

    def _clip_cover_refreshes(
        self,
        cover_refreshes: list[dict[str, int]],
        *,
        rss_kb: int | None,
        creates: int,
        updates: int,
        limit_kb: int,
    ) -> list[dict[str, int]]:
        """Clip the #1386 cover-refresh list to the chunk's remaining budget headroom.

        Each refresh is a ``SetCustomArtworkForApp`` push costing one transient
        cover (:data:`domain.session_budget.COVER_TRANSIENT_KB`) of renderer heap
        at the chunk's peak, on top of the chunk's own projected cost. The clip
        keeps only as many refreshes as fit under ``limit_kb`` so the refreshes
        can never push a chunk the gate just approved over the line; the
        remainder is skipped gracefully — their grid files are already updated,
        so a Steam restart shows them (the same degradation the budget gate uses
        elsewhere). ``rss_kb`` ``None`` (measurement unavailable) fails open and
        keeps the whole list, mirroring the gate itself.
        """
        if rss_kb is None:
            return cover_refreshes
        headroom_kb = limit_kb - rss_kb - chunk_worst_cost_kb(creates, updates)
        allowance = max(0, headroom_kb // COVER_TRANSIENT_KB)
        if allowance >= len(cover_refreshes):
            return cover_refreshes
        self._logger.info(
            f"Session-budget headroom clips cover refreshes: applying {allowance} of "
            f"{len(cover_refreshes)}; the remaining grid files are already updated and "
            f"show after a Steam restart"
        )
        return cover_refreshes[:allowance]

    def _build_final_platform_stamp(
        self, unit: WorkUnit, chunk_index: int, chunk_count: int, fetch_id: str | None = None
    ) -> PlatformSyncState | None:
        """Build the completion stamp for a platform unit's FINAL chunk, else ``None``.

        On the final chunk of a PLATFORM unit the stamp rides that chunk's commit
        UoW so "platform fully synced" ⟺ "stamp exists" is atomic on a crash. The
        stamp lets the next sync's incremental-skip gate skip this platform even
        when the whole run is later cancelled (its library-wide ``last_sync`` never
        advances). Only platform units carry a skip gate — collections have none,
        so they are never stamped. A cancel or heartbeat timeout mid-unit returns
        before the final chunk, so an incomplete platform is never stamped
        (ADR-0023 / #1025).
        """
        if unit.type == "platform" and unit.slug and chunk_index == chunk_count - 1:
            return PlatformSyncState.stamp(
                platform_slug=unit.slug,
                at=self._clock.now().isoformat(),
                rom_count=unit.rom_count,
                fetch_id=fetch_id,
            )
        return None

    def _build_final_collection_stamp(
        self,
        unit: WorkUnit,
        chunk_index: int,
        chunk_count: int,
        member_rom_ids: list[int] | None,
    ) -> CollectionSyncState | None:
        """Build the completion stamp for a standard/smart collection's FINAL chunk, else ``None``.

        The collection sibling of :meth:`_build_final_platform_stamp` (#742). On
        the final chunk of a standard/smart collection unit whose listing carried an
        ``updated_at``, the stamp rides that chunk's commit UoW so "collection
        fully synced" ⟺ "stamp exists" is atomic on a crash. ``member_rom_ids`` is
        the collection's FULL membership (every member id, not just the applied
        new_roms), which a future skip replays to rebuild the Steam-collection
        map. Virtual collections carry no stamp (no stable
        ``updated_at``), and a cancel or heartbeat timeout mid-unit returns before
        the final chunk — an incomplete collection is never stamped (ADR-0023).
        """
        if (
            unit.type == "collection"
            and unit.collection_kind in ("standard", "smart")
            and unit.collection_updated_at
            and member_rom_ids is not None
            and chunk_index == chunk_count - 1
        ):
            return CollectionSyncState.stamp(
                collection_id=str(unit.id),
                collection_kind=unit.collection_kind,
                updated_at=unit.collection_updated_at,
                completed_at=self._clock.now().isoformat(),
                rom_count=unit.rom_count,
                member_rom_ids=tuple(member_rom_ids),
            )
        return None

    async def _sync_platform_unit(
        self,
        unit: WorkUnit,
        *,
        synced_rom_ids: set[int],
        platform_rom_ids: set[int],
        progress_step: int = 0,
        progress_total_steps: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Resolve ROMs for a platform unit and update cross-unit accumulators.

        Returns ``(unit_roms, skipped)`` for the caller's downstream
        shortcut + artwork + apply phases. ROMs come from a live per-unit
        fetch (no preview cache); the fetcher's incremental-skip path
        handles the "unchanged platform" optimisation internally.
        ``progress_step`` / ``progress_total_steps`` thread the unit's coarse
        position into the fetcher's per-page ``fetching`` frames.
        """
        unit_roms, skipped = await self._fetcher.fetch_platform_unit(
            unit, progress_step=progress_step, progress_total_steps=progress_total_steps
        )
        platform_rom_ids.update(r["id"] for r in unit_roms)
        synced_rom_ids.update(r["id"] for r in unit_roms)
        return unit_roms, skipped

    async def _sync_collection_unit(
        self,
        unit: WorkUnit,
        *,
        synced_rom_ids: set[int],
        collection_memberships: dict[tuple[str, str], CollectionMembership],
        progress_step: int = 0,
        progress_total_steps: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Resolve ROMs for a collection unit and record its membership.

        Returns ``(unit_roms, skipped)`` — ``skipped`` is True when the
        incremental-skip gate (#742) reconstructed the collection from its
        completion stamp instead of paginating. ROMs come from a live per-unit
        fetch (or, on a skip, the registry) that already dedups against
        ``synced_rom_ids``. ``progress_step`` / ``progress_total_steps`` thread
        the unit's coarse position into the fetcher's per-page ``fetching`` frames.
        """
        unit_roms, all_collection_rom_ids, skipped = await self._fetcher.fetch_collection_unit(
            unit, synced_rom_ids, progress_step=progress_step, progress_total_steps=progress_total_steps
        )
        if all_collection_rom_ids:
            collection_memberships[_collection_membership_key(unit)] = CollectionMembership(
                name=unit.name,
                rom_ids=all_collection_rom_ids,
                kind=str(unit.collection_kind),
                virtual_type=unit.virtual_type,
            )
        return unit_roms, skipped

    async def _wait_for_unit_complete(self, unit: WorkUnit, event: asyncio.Event) -> dict[str, int] | None:
        """Heartbeat-based wait for the active unit's frontend callback.

        Returns the frontend-reported ``rom_id_to_app_id`` on success.
        Returns ``None`` on timeout or cancel — the outer loop maps that
        onto a recoverable cancellation. The wait poll polls the
        heartbeat clock rather than ``asyncio.wait_for(timeout=...)``
        because the frontend sends ``sync_heartbeat`` calls during long
        per-unit applies (artwork download, Set* calls) and a 60s
        absolute cap would still race those.
        """
        box = self._sync_state
        while not event.is_set():
            if box.is_cancelling():
                self._logger.info(f"Per-unit cancel observed while waiting for unit {unit.name}")
                return None
            elapsed = self._clock.monotonic() - box.sync_last_heartbeat
            if elapsed > _UNIT_HEARTBEAT_TIMEOUT_SEC:
                self._logger.warning(f"Per-unit timeout: no heartbeat for {elapsed:.0f}s waiting on unit {unit.name}")
                return None
            try:
                await self._sleeper.sleep(_UNIT_WAIT_POLL_SEC)
            except asyncio.CancelledError:
                self._logger.info(f"Per-unit wait cancelled for unit {unit.name}")
                raise

        results = box.last_unit_results or {}
        box.last_unit_results = None
        return results

    async def _finalize_per_unit(
        self,
        *,
        synced_rom_ids: set[int],
        collection_memberships: dict[tuple[str, str], CollectionMembership],
        platform_rom_ids: set[int],
        platform_names: dict[str, str],
        cancelled: bool,
    ) -> FinalizeOutcome:
        """Emit stale-removal + collection mappings; measure the run's memory delta.

        Does NOT emit the terminal ``sync_complete`` — that is deferred to the
        caller's :meth:`SyncReporter.emit_sync_complete` after the terminal SyncRun
        write (#39). Returns a :class:`FinalizeOutcome` carrying the collection maps
        (the completed-run write needs their keys) plus the ``interrupt_reason`` /
        ``restart_recommended`` surfacing values the deferred emit needs.
        """
        # Stale ROMs: any bound ROM in the registry whose rom_id wasn't
        # seen by any processed unit. Only meaningful on a non-cancelled run
        # — a partial run can't tell "stale" from "didn't get to it yet".
        # Each entry carries the ROM's ``shortcut_app_id`` as read BEFORE the
        # reporter's finalize unbinds it (which NULLs the binding); the
        # frontend removes the Steam shortcut directly by ``app_id`` so it
        # never has to re-resolve rom_id→app_id after the binding is gone.
        # ``committed_app_ids`` (every appId this run bound, across both commit
        # paths) is excluded so a new rom_id reusing an old appId is never
        # wrongly removed (#1036).
        if not cancelled:
            stale = await self._loop.run_in_executor(
                None, self._scan_stale_roms, synced_rom_ids, set(self._sync_state.committed_app_ids)
            )
        else:
            stale = []
        await self._emit(
            "sync_stale",
            {"remove": [{"rom_id": rom_id, "app_id": app_id} for rom_id, app_id in stale]},
        )

        # Session-budget surfacing (#1383): a budget pause carries its distinct
        # reason into the terminal payload; a CLEAN run recommends a Steam restart
        # when its post-run RSS is high (the next large operation would likely
        # pause/crash). The memory delta is measured at every terminal this
        # finalize reaches — completed/paused/cancelled/interrupted; an errored
        # run aborts before this path and keeps the prior delta (#36). It is the
        # GC-settled terminal RSS differenced against this run's raw run-start
        # baseline, so a paused/cancelled/interrupted run reports ITS OWN
        # consumption-so-far ("last run: +X GB") instead of leaving a prior clean
        # run's number in place. The restart advisory stays clean-run-only. The delta
        # is retained IN THE BOX only, surfaced to the QAM via
        # get_session_budget_status (not the sync_complete wire; the UI reads it from
        # the callable). Fail-open — an unavailable reading or any seam error
        # recommends nothing and leaves the delta unmeasurable (never a stale number).
        interrupt_reason = self._sync_state.interrupt_reason if cancelled else None
        restart_recommended = False
        memory_delta_kb: int | None = None
        try:
            rss_kb = await self._session_budget.measure_rss()
            memory_delta_kb = session_memory_delta(self._sync_state.run_start_rss_kb, rss_kb)
            if not cancelled:
                restart_recommended = rss_kb is not None and post_run_advisory(rss_kb)
        except Exception as e:  # fail-open: the advisory/delta must never fail finalize
            self._logger.debug(f"Session-budget terminal advisory/delta skipped: {e}")
        self._sync_state.last_run_delta_kb = memory_delta_kb

        platform_app_ids, romm_collection_app_ids = await self._reporter.get().finalize_per_unit_run(
            pending_collection_memberships=collection_memberships,
            pending_platform_rom_ids=platform_rom_ids,
            platform_names=platform_names,
            stale_rom_ids=[rom_id for rom_id, _app_id in stale],
        )
        return FinalizeOutcome(
            platform_app_ids=platform_app_ids,
            romm_collection_app_ids=romm_collection_app_ids,
            interrupt_reason=interrupt_reason,
            restart_recommended=restart_recommended,
        )

    def _build_core_overrides(self, roms: list[dict[str, Any]]) -> dict[int, EmulatorInvocation]:
        """Resolve each ROM's FULL active emulator for the bake.

        Runs every ROM in *roms* through the shared per-ROM ``active_core``
        resolver (the single seam that folds the per-game ``emulator_override``
        and per-platform ``settings.json`` core over the standalone-aware
        es_systems default). Only ROMs that resolve to an emulator (libretro core
        or standalone) appear in the returned ``{rom_id: EmulatorInvocation}``
        map, so :func:`build_shortcuts_data` bakes their ``-e`` form; a ROM that
        resolves to nothing (a genuinely unresolvable platform) is absent and
        falls back to the plain launch. The resolver already warns + degrades on
        a stale label, so no bogus invocation ever reaches the bake.
        """
        resolved: dict[int, EmulatorInvocation] = {}
        for rom in roms:
            emulator = self._active_core.active_emulator_for_rom(rom["id"])
            if emulator is not None:
                resolved[rom["id"]] = emulator
        return resolved

    def _scan_installed_paths(self) -> dict[int, str]:
        """Read ``{rom_id: bake_path}`` for the whole installed library in one scan.

        Used by the preview path, which already operates over every ROM in the
        library — a single ``iter_all()`` is the cheapest way to cover them all.
        Each path is the disc-resolved launch path: a multi-disc ROM resolves its
        persisted ``selected_disc`` pin against its install directory (a
        single-disc ROM resolves to its own ``file_path``, unchanged), or ``""``
        when the install has no launch target. Only ROMs with a current install
        record appear in the map; a ROM not downloaded is absent, and both cases
        reach :func:`build_shortcuts_data` as the same empty launch command.
        """
        with self._uow_factory() as uow:
            paths: dict[int, str] = {}
            for install in uow.rom_installs.iter_all():
                rom = uow.roms.get(install.rom_id)
                selected_disc = rom.selected_disc if rom is not None else None
                paths[install.rom_id] = self._disc_resolver.resolve_for_install(install, selected_disc)
            return paths

    def _read_installed_paths(self, rom_ids: set[int]) -> dict[int, str]:
        """Read ``{rom_id: bake_path}`` for *rom_ids* via targeted point-lookups.

        Used by the per-unit apply path: scanning the whole ``rom_installs``
        table once per unit is O(units * all-installs) (#797), so this resolves
        only the unit's ROMs via ``get(rom_id)``. Each path is the disc-resolved
        launch path — a multi-disc ROM resolves its persisted ``selected_disc``
        pin against its install directory (a single-disc ROM resolves to its own
        ``file_path``, unchanged), or ``""`` when the install has no launch
        target. A ROM with no install record is absent; both cases reach
        :func:`build_shortcuts_data` as the same empty launch command.
        """
        with self._uow_factory() as uow:
            paths: dict[int, str] = {}
            for rom_id in rom_ids:
                install = uow.rom_installs.get(rom_id)
                if install is not None:
                    rom = uow.roms.get(rom_id)
                    selected_disc = rom.selected_disc if rom is not None else None
                    paths[rom_id] = self._disc_resolver.resolve_for_install(install, selected_disc)
            return paths

    def _scan_stale_roms(self, synced_rom_ids: set[int], synced_app_ids: set[int]) -> list[tuple[int, int]]:
        """Return ``(rom_id, app_id)`` for bound ROMs not synced this run.

        Unbound (stale) rows are skipped — they were already cleared on a
        prior run and carry no Steam shortcut to remove. The ``app_id`` is
        the still-live ``shortcut_app_id`` captured here, before the
        reporter's finalize unbinds the row; the orchestrator threads it
        into the ``sync_stale`` payload so the frontend removes the Steam
        shortcut without re-resolving rom_id→app_id after the unbind.

        Any candidate whose ``app_id`` is in *synced_app_ids* — an appId this
        run bound to a freshly-synced ROM — is excluded by
        :func:`select_stale_removals`: a new server-issued ``rom_id`` can reuse
        an old appId (unchanged ``exe + name``), so the old colliding row looks
        stale but its appId now belongs to the new row. Removing it would wipe
        the shortcut the run just created/updated (#1036).
        """
        with self._uow_factory() as uow:
            candidate_stale = [
                (rom.rom_id, rom.shortcut_app_id)
                for rom in uow.roms.iter_all()
                if rom.shortcut_app_id is not None and rom.rom_id not in synced_rom_ids
            ]
        return select_stale_removals(candidate_stale, synced_app_ids)

    # ── Artwork delegation ───────────────────────────────────────

    async def _download_artwork(
        self, all_roms, progress_step=4, progress_total_steps=6, label="", applied_sources=None
    ):
        """Delegate artwork download to ArtworkService callback.

        ``label`` is the unit's display name, threaded into the cover-download
        progress frames ("Preparing covers for <label>"). ``applied_sources`` is
        the optional accumulator ArtworkService fills with the cover source
        actually applied per ROM (``url_cover`` on a 404 fallback, #1450), so the
        per-unit commit persists a truthful ``cover_source`` fingerprint.
        """
        box = self._sync_state
        return await self._artwork.download_artwork(
            all_roms,
            emit_progress=self.emit_progress,
            is_cancelling=box.is_cancelling,
            progress_step=progress_step,
            progress_total_steps=progress_total_steps,
            label=label,
            applied_sources=applied_sources,
        )

    async def _refresh_changed_covers(self, unit_roms, registry, progress_step=4, progress_total_steps=6, label=""):
        """Delegate the #1386 cover-cache invalidation pass to the ArtworkManager.

        ``registry`` is the unit's bound-row projection (``_read_apply_registry``)
        the pass compares fingerprints against — the same read the group collapse
        already made. ``label`` is the unit's display name, threaded into the
        throttled "Refreshing covers for <label>" progress frames. Returns the
        refreshed ``{rom_id, app_id}`` list the first apply chunk carries to the
        frontend.
        """
        box = self._sync_state
        return await self._artwork.refresh_changed_covers(
            unit_roms,
            registry,
            emit_progress=self.emit_progress,
            is_cancelling=box.is_cancelling,
            progress_step=progress_step,
            progress_total_steps=progress_total_steps,
            label=label,
        )
