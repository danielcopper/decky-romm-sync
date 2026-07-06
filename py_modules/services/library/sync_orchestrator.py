"""Preview / apply / per-unit sync lifecycle and the heartbeat clock.

Owns every async path the user triggers from the QAM that mutates
in-flight sync state: starting and cancelling syncs, computing a
preview (read-only), and dispatching the per-unit sync pipeline on
apply. The heartbeat clock — refreshed on every progress emission and
inspected by per-unit waits — lives here too. Progress emission also
lives here — sub-services that need to surface progress receive the
orchestrator's ``emit_progress`` callback through their config.
Anything that fetches ROMs belongs in :class:`LibraryFetcher`;
anything that finalises shortcuts after the apply completes belongs
in :class:`SyncReporter`. Cached ``rom_metadata`` is written by the
reporter's per-unit commit (the same write UoW as the ``roms`` upsert),
so preview never persists metadata and an interrupted apply leaves only
already-committed units' metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.preview_delta import PreviewDelta
from domain.shortcut_data import EmulatorInvocation, build_shortcuts_data
from domain.sibling_resolution import AUTO_REGION
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
    the installed-launch path at sync time.
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
            collection_memberships: dict[str, list[int]] = {}
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
                await self._fetch_preview_unit(unit, all_roms, platform_rom_ids, synced_rom_ids, collection_memberships)

            installed_paths = await self._loop.run_in_executor(None, self._scan_installed_paths)
            core_overrides = await self._loop.run_in_executor(None, self._build_core_overrides, all_roms)
            shortcuts_data = build_shortcuts_data(all_roms, self._plugin_dir, installed_paths, core_overrides)
            platform_name_set = {u.name for u in work_queue if u.type == "platform"}
            slug_to_name = {u.slug: u.name for u in work_queue if u.type == "platform" and u.slug}
            registry, last_synced_platforms, last_synced_collections = await self._loop.run_in_executor(
                None, self._read_preview_baseline, slug_to_name
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

            # Final cancel checkpoint: a cancel can land after the unit loop's
            # last per-unit check but before the preview is staged. Re-check
            # here so a late cancel routes into the SyncCancelled branch (which
            # leaves ``pending_delta`` None) instead of staging a delta the
            # user already cancelled (#1202).
            if box.is_cancelling():
                raise SyncCancelled(_SYNC_CANCELLED)

            preview_id = self._uuid_gen.uuid4()
            platforms_count = sum(1 for u in work_queue if u.type == "platform")
            box.pending_delta = PreviewDelta(
                preview_id=preview_id,
                created_at=self._clock.time(),
                platforms_count=platforms_count,
                total_roms=len(all_roms),
            )

            await self.emit_progress(SyncStage.DONE, message="Preview ready", running=False)

            return {
                "success": True,
                "summary": {
                    "new_count": len(new),
                    "changed_count": len(changed),
                    "unchanged_count": len(unchanged_ids),
                    "remove_count": len(stale),
                    "disabled_platform_remove_count": disabled_count,
                    "collection_diff": compute_collection_diff(
                        collection_memberships,
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
        collection_memberships: dict[str, list[int]],
    ) -> None:
        """Fetch one work unit's ROMs and fold them into the preview accumulators.

        Platform units add every ROM to ``platform_rom_ids`` and
        ``synced_rom_ids``; collection units record their full membership
        list under the unit name. ``all_roms`` is extended in both cases.
        Mutates the passed-in accumulators in place.
        """
        if unit.type == "platform":
            unit_roms, _skipped = await self._fetcher.fetch_platform_unit(unit)
            for rom in unit_roms:
                platform_rom_ids.add(rom["id"])
                synced_rom_ids.add(rom["id"])
            all_roms.extend(unit_roms)
        else:
            unit_roms, all_collection_rom_ids = await self._fetcher.fetch_collection_unit(unit, synced_rom_ids)
            if all_collection_rom_ids:
                collection_memberships[unit.name] = all_collection_rom_ids
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

    async def emit_progress(self, stage, current=0, total=0, message="", running=True, step=0, total_steps=0):
        """Persist the progress snapshot and emit the sync_progress event.

        ``stage`` is a :class:`SyncStage` (or its string value); ``step``
        / ``total_steps`` are the coarse unit index / total units that
        drive the determinate main bar — stages without a unit index yet
        (discovering, fetching) pass ``0`` / ``0``, which the frontend
        treats as indeterminate. ``current`` / ``total`` are the fine
        within-unit counters. The snapshot is written to the box first so
        :meth:`get_sync_status` always returns the latest state even if
        the event never reaches a freshly remounted QAM.
        """
        self._sync_state.sync_progress = {
            "running": running,
            "stage": SyncStage(stage).value,
            "current": current,
            "total": total,
            "message": message,
            "step": step,
            "totalSteps": total_steps,
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
        # unit has been applied.
        synced_rom_ids: set[int] = set()
        collection_memberships: dict[str, list[int]] = {}
        platform_rom_ids: set[int] = set()
        total_games_applied = 0
        cancelled = False
        # Reset the per-run set of appIds the reporter binds (across both the
        # happy-path and late-ack commit paths). The stale scan excludes it so
        # a new rom_id reusing an old appId is never wrongly removed (#1036).
        box.committed_app_ids = set()
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
            platforms_planned = sum(1 for u in work_queue if u.type == "platform")
            # Live ``platform_slug → display_name`` map from the work-queue;
            # threaded into finalize so collections key on display names and
            # the offline name cache stays current as of this sync.
            platform_names = {u.slug: u.name for u in work_queue if u.type == "platform" and u.slug}
            self._logger.info(f"Per-unit pipeline: {total_units} units planned, {total_roms_planned} ROMs total")
            await self._emit(
                "sync_plan",
                {
                    "run_id": str(run_id or ""),
                    "units": [u.to_event_payload() for u in work_queue],
                    "total_units": total_units,
                    "total_roms": total_roms_planned,
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
            platform_app_ids, romm_collection_app_ids = await self._finalize_per_unit(
                total_games_applied=total_games_applied,
                synced_rom_ids=synced_rom_ids,
                collection_memberships=collection_memberships,
                platform_rom_ids=platform_rom_ids,
                platform_names=platform_names,
                cancelled=cancelled,
            )

            # SyncRun terminal status — short write UoW. Cancelled runs
            # mark cancelled; clean runs complete with the synced platform
            # and collection names derived from the built maps.
            if cancelled:
                await self._loop.run_in_executor(None, self._mark_sync_run_cancelled, run_id, _SYNC_CANCELLED)
            else:
                await self._loop.run_in_executor(
                    None,
                    self._complete_sync_run,
                    run_id,
                    list(platform_app_ids.keys()),
                    list(romm_collection_app_ids.keys()),
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
        work-queue) and falling back to the slug. The last-synced
        platform/collection lists come from the newest completed
        ``SyncRun``.
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
                }
            latest = uow.sync_runs.get_latest_completed()
            last_platforms = list(latest.platforms_completed or []) if latest is not None else []
            last_collections = list(latest.collections_completed or []) if latest is not None else []
        return registry, last_platforms, last_collections

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
        shortcut the collapse can grandfather or rebind. The apply path did not
        read the registry before group-aware sync (ADR-0021).
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
                }
                for rom in rows
                if rom.shortcut_app_id is not None
            }

    async def _sync_one_unit(
        self,
        unit: WorkUnit,
        *,
        unit_index: int,
        total_units: int,
        synced_rom_ids: set[int],
        collection_memberships: dict[str, list[int]],
        platform_rom_ids: set[int],
    ) -> int:
        """Process one work unit start-to-finish; return shortcuts applied.

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
        ``total_games_applied`` total returned to the user.
        """
        box = self._sync_state
        await self.emit_progress(
            SyncStage.APPLYING,
            message=f"{unit.name} ({unit_index + 1}/{total_units})",
            step=unit_index + 1,
            total_steps=total_units,
        )

        # Fetch this unit's ROMs. Platform units may incremental-skip;
        # collection units always paginate (collection membership is
        # the source of truth, no per-collection "last_sync" gate today).
        if unit.type == "platform":
            unit_roms, skipped = await self._sync_platform_unit(
                unit,
                synced_rom_ids=synced_rom_ids,
                platform_rom_ids=platform_rom_ids,
            )
        else:
            unit_roms, skipped = await self._sync_collection_unit(
                unit,
                synced_rom_ids=synced_rom_ids,
                collection_memberships=collection_memberships,
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
            return len(unit_roms)

        # Build shortcut data for every fetched ROM. Installed ROMs carry the
        # full launch command; uninstalled ROMs get an empty placeholder until
        # they are downloaded.
        installed_paths = await self._loop.run_in_executor(
            None, self._read_installed_paths, {rom["id"] for rom in unit_roms}
        )
        core_overrides = await self._loop.run_in_executor(None, self._build_core_overrides, unit_roms)
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
        registry = await self._loop.run_in_executor(None, self._read_apply_registry, unit)
        emitted = collapse_sibling_groups(
            shortcuts_data,
            registry,
            set(installed_paths),
            complete_group_view=(unit.type == "platform"),
            preferred_region=self._settings.get("preferred_region", AUTO_REGION),
        )

        # Download artwork only for the ROMs that actually get a shortcut (the
        # representatives + grandfathered siblings), not every dump — no eager
        # covers for versions with no shortcut. A rebind entry pulls its cover
        # from the representative it binds (``bind_rom_id``), whose raw dict is
        # the one present in unit_roms.
        if emitted:
            artwork_ids = {int(e.get(BIND_ROM_ID_KEY, e["rom_id"])) for e in emitted}
            artwork_roms = [rom for rom in unit_roms if rom["id"] in artwork_ids]
            cover_paths = await self._download_artwork(
                artwork_roms, progress_step=unit_index + 1, progress_total_steps=total_units
            )
            for e in emitted:
                e["cover_path"] = cover_paths.get(int(e.get(BIND_ROM_ID_KEY, e["rom_id"])), "")

        if box.is_cancelling():
            return 0

        # Stage the emitted representatives for cover finalise + binding, and the
        # full built set for the ack-independent identity + version persist (the
        # reporter upserts a row for every sibling, binds only representatives).
        box.pending_sync = {e["rom_id"]: e for e in emitted}
        box.pending_all_roms = {sd["rom_id"]: sd for sd in shortcuts_data}

        # Emit per-unit apply event + wait for the frontend callback.
        box.unit_complete_event = asyncio.Event()
        box.last_unit_results = None
        # Stamp the unit's identity so the reporter can validate the ack's
        # run_id + unit_id and reject a late ack from a cancelled run or a
        # stray ack for a different unit (#1041).
        box.active_unit_id = unit.id
        # Reset the abandoned-unit stash so a prior timed-out unit can't
        # leak its state into this one's late-ack commit (#1052).
        box.unit_abandoned = False
        box.pending_unit_roms = []
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
                "shortcuts": emitted,
            },
        )

        applied = await self._wait_for_unit_complete(unit, box.unit_complete_event)
        if applied is None:
            # The wait gave up — but the reason matters. The outer loop
            # observes CANCELLING and stops either way; what differs is
            # whether the frontend's in-flight work is recoverable.
            if box.is_cancelling():
                # User cancel: in-flight work is intentionally discarded.
                # Drop the pending state, null the event, and clear the unit
                # identity so a stray late ack can't commit a cancelled unit.
                box.pending_sync = {}
                box.pending_all_roms = {}
                box.unit_complete_event = None
                box.active_unit_id = None
            else:
                # Heartbeat timeout: the frontend has already created this
                # unit's Steam shortcuts and will still fire its late
                # ``report_unit_results`` ack. Keep ``pending_sync`` +
                # ``pending_all_roms`` + ``unit_complete_event`` and stash the
                # unit's ROMs so the late ack commits the delivered bindings
                # instead of leaving orphan shortcuts (#1052). Flag the unit
                # abandoned so the reporter drives that commit itself.
                box.unit_abandoned = True
                box.pending_unit_roms = unit_roms
                box.request_cancel()
            return 0

        # Per-unit commit: the reporter upserts EVERY fetched ROM into the
        # ``roms`` aggregate (identity + version metadata, unbound for non-
        # representatives) and binds only the acked representatives, stamping
        # each ROM's cached ``rom_metadata`` in the same write UoW (Rom row
        # first, metadata second — FK-safe). ``unit_roms`` is the live RomM
        # fetch for the whole unit — the source of ``metadatum``.
        await self._reporter.get().commit_unit_results(applied, unit_roms)

        box.pending_sync = {}
        box.pending_all_roms = {}
        box.unit_complete_event = None
        box.active_unit_id = None
        return len(applied)

    async def _sync_platform_unit(
        self,
        unit: WorkUnit,
        *,
        synced_rom_ids: set[int],
        platform_rom_ids: set[int],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Resolve ROMs for a platform unit and update cross-unit accumulators.

        Returns ``(unit_roms, skipped)`` for the caller's downstream
        shortcut + artwork + apply phases. ROMs come from a live per-unit
        fetch (no preview cache); the fetcher's incremental-skip path
        handles the "unchanged platform" optimisation internally.
        """
        unit_roms, skipped = await self._fetcher.fetch_platform_unit(unit)
        platform_rom_ids.update(r["id"] for r in unit_roms)
        synced_rom_ids.update(r["id"] for r in unit_roms)
        return unit_roms, skipped

    async def _sync_collection_unit(
        self,
        unit: WorkUnit,
        *,
        synced_rom_ids: set[int],
        collection_memberships: dict[str, list[int]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Resolve ROMs for a collection unit and record its membership.

        Returns ``(unit_roms, skipped)`` — ``skipped`` is always
        ``False`` because collection units have no incremental-skip
        gate today. ROMs come from a live per-unit fetch that already
        dedups against ``synced_rom_ids``.
        """
        skipped = False
        unit_roms, all_collection_rom_ids = await self._fetcher.fetch_collection_unit(unit, synced_rom_ids)
        if all_collection_rom_ids:
            collection_memberships[unit.name] = all_collection_rom_ids
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
        total_games_applied: int,
        synced_rom_ids: set[int],
        collection_memberships: dict[str, list[int]],
        platform_rom_ids: set[int],
        platform_names: dict[str, str],
        cancelled: bool,
    ):
        """Emit stale-removal, collection mappings, and the terminal sync_complete.

        Returns ``(platform_app_ids, romm_collection_app_ids)`` so the
        caller can derive the SyncRun's completed platform/collection names.
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

        return await self._reporter.get().finalize_per_unit_run(
            pending_collection_memberships=collection_memberships,
            pending_platform_rom_ids=platform_rom_ids,
            total_games=total_games_applied,
            platform_names=platform_names,
            cancelled=cancelled,
            stale_rom_ids=[rom_id for rom_id, _app_id in stale],
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
        single-disc ROM resolves to its own ``file_path``, unchanged). Only ROMs
        with a current install record appear in the map; ROMs not downloaded are
        absent, so :func:`build_shortcuts_data` emits an empty placeholder launch
        command for them.
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
        ``file_path``, unchanged). ROMs with no install record are absent, so
        :func:`build_shortcuts_data` emits an empty placeholder launch command for
        them.
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

    async def _download_artwork(self, all_roms, progress_step=4, progress_total_steps=6):
        """Delegate artwork download to ArtworkService callback."""
        box = self._sync_state
        return await self._artwork.download_artwork(
            all_roms,
            emit_progress=self.emit_progress,
            is_cancelling=box.is_cancelling,
            progress_step=progress_step,
            progress_total_steps=progress_total_steps,
        )
