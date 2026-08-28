"""Preview / apply / per-unit sync lifecycle and progress emission.

Owns every async path the user triggers from the QAM that mutates in-flight
sync state: starting and cancelling syncs, computing a preview (read-only),
and dispatching the per-unit sync pipeline on apply. Of the heartbeat clock
this module holds only two of the three stamps — the one taken when a run is
admitted, and the frontend's ``sync_heartbeat`` route. The stamp that opens a
chunk's 60-second window, and the wait that measures against it, belong to
:class:`ChunkDispatcher`, so the window a timeout is judged by starts at a
chunk's emit rather than at run admission. Progress emission lives here —
sub-services that need to surface progress receive the orchestrator's
``emit_progress`` callback through their config. Anything that fetches ROMs
belongs in :class:`LibraryFetcher`; anything that finalises shortcuts after the apply
completes belongs in :class:`SyncReporter`; Steam's renderer memory belongs
in :class:`SessionBudgetMonitor`; a single ROM's launch facts — where its
file is and what runs it — belong in :class:`ShortcutLaunchResolver`; driving
one unit's built delta to the frontend and back — emit a chunk, wait for its
ack, commit it — belongs in :class:`ChunkDispatcher`; reading
the registry and the completion stamps into the projections these decisions
are made against belongs in :class:`LocalLibraryReader`. Two of that last group
are this module's deliberately: the platform stamp's DELETE — a write, and a step
of the apply pipeline rather than a question a run weighs; its ordering is argued
at its call site — and the pure component-key stamp, which does no I/O at all. Cached
``rom_metadata`` is written by the reporter's per-unit commit (the same write
UoW as the ``roms`` upsert), so preview never persists metadata and an
interrupted apply leaves only already-committed units' metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.cover_refresh import count_cover_refreshes
from domain.session_budget import post_run_advisory, session_memory_delta
from domain.shortcut_data import build_shortcuts_data
from domain.sibling_group import compute_component_group_keys
from domain.sibling_resolution import AUTO_REGION
from domain.sync_diff import (
    BIND_ROM_ID_KEY,
    classify_roms,
    collapse_sibling_groups,
    compute_collection_diff,
    compute_platform_collection_diff,
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
    from services.library.chunk_dispatcher import ChunkDispatcher
    from services.library.fetcher import LibraryFetcher
    from services.library.local_library_reader import LocalLibraryReader
    from services.library.reporter import SyncReporter
    from services.library.shortcut_launch_resolver import ShortcutLaunchResolver
    from services.protocols import (
        ArtworkManager,
        Clock,
        EventEmitter,
        UnitOfWorkFactory,
        UuidGen,
    )


_SYNC_CANCELLED = "Sync cancelled"
# Terminal reason when the run died externally (heartbeat timeout — the
# frontend crashed or reloaded) rather than by the user's Cancel. Stored in
# ``sync_runs.error`` via ``mark_interrupted``; the status split lets the UI
# report "(interrupted)" instead of "(cancelled)" for a crash.
_SYNC_INTERRUPTED = "Sync interrupted (Steam UI stopped responding)"


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
    Clock/UuidGen test seams, the SQLite Unit-of-Work factory
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
    ``shortcut_launch_resolver`` peer answers the two per-ROM questions each
    shortcut bake needs — the disc-resolved installed path and the active
    emulator — which preview and the per-unit apply both hand straight to
    :func:`build_shortcuts_data`. The ``local_library_reader`` peer answers every
    question the run asks of this device's own record of the library — the
    classify baseline, the per-unit bound-row projection, the unstamped-platform
    count, the resident sibling-group keys, and the stale-row scan — each in its
    own short read Unit of Work, where the fetcher asks the same kinds of
    question of RomM. The ``chunk_dispatcher`` peer takes the unit's built delta
    the rest of the way: it emits each chunk to the frontend, waits out the
    heartbeat clock for the ack, and commits the chunk through the reporter. The
    ``session_budget`` seam owns every
    renderer-heap reading and verdict, which the dispatcher asks at each chunk
    boundary — whether applying the chunk would exhaust Steam's per-session heap
    budget, and how much headroom is left for the chunk's additive cover work
    (#1383); the terminal memory delta this module reports is drawn from the same
    seam.
    """

    settings: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    uow_factory: UnitOfWorkFactory
    sync_state_box: LibrarySyncStateBox
    fetcher: LibraryFetcher
    reporter: LateBinding[SyncReporter]
    artwork: ArtworkManager
    shortcut_launch_resolver: ShortcutLaunchResolver
    local_library_reader: LocalLibraryReader
    session_budget: SessionBudgetMonitor
    chunk_dispatcher: ChunkDispatcher


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
        self._uow_factory = config.uow_factory
        self._sync_state = config.sync_state_box
        self._fetcher = config.fetcher
        self._artwork = config.artwork
        self._reporter = config.reporter
        self._shortcut_launch_resolver = config.shortcut_launch_resolver
        self._local_library_reader = config.local_library_reader
        self._session_budget = config.session_budget
        self._chunk_dispatcher = config.chunk_dispatcher

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

            installed_paths = await self._loop.run_in_executor(
                None, self._shortcut_launch_resolver.do_scan_installed_paths
            )
            core_overrides = await self._loop.run_in_executor(
                None, self._shortcut_launch_resolver.do_build_core_overrides, all_roms
            )
            # Stamp each fresh ROM's component sibling-group key before the build so
            # the collapse below groups games, not dumps. The preview union is a
            # complete view of every enabled platform's groups; the DB's persisted
            # keys seed a member edging into a resident sibling on a skipped platform.
            resident_keys = await self._loop.run_in_executor(
                None, self._local_library_reader.do_read_resident_group_keys
            )
            self._stamp_component_group_keys(all_roms, resident_keys)
            shortcuts_data = build_shortcuts_data(all_roms, self._plugin_dir, installed_paths, core_overrides)
            platform_name_set = {u.name for u in work_queue if u.type == "platform"}
            slug_to_name = {u.slug: u.name for u in work_queue if u.type == "platform" and u.slug}
            registry, last_synced_platforms, last_synced_collections = await self._loop.run_in_executor(
                None, self._local_library_reader.do_read_preview_baseline, slug_to_name
            )
            # Enabled platforms lacking a completion stamp (#1416): a
            # late-ack-recovered platform is complete but unstamped, so the
            # wholesale-skip gate full-fetches it forever and its run status
            # never heals. Counted here (side-effect-free read) so the preview
            # can still offer Apply on an otherwise-empty delta — the apply's
            # 0-delta empty final chunk re-writes the stamp and records a fresh
            # SyncRun (the one-time re-walk ADR-0023 intends).
            restamp_platform_count = await self._loop.run_in_executor(
                None, self._local_library_reader.do_count_unstamped_platforms, set(slug_to_name)
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

            # Post-preview session-budget prognosis (#1383): warn up front when the
            # planned work would walk the renderer heap past the budget ceiling. The
            # gate is fail-open, so an unreadable renderer simply yields no warning.
            #
            # Taken here rather than after the DONE frame so that nothing awaits
            # between the cancel checkpoint below and the staging that follows it.
            pause_likely = await self._session_budget.predict_pause_likely(
                new_items=len(new), changed_items=len(changed)
            )

            # Final cancel checkpoint: a cancel can land after the unit loop's
            # last per-unit check but before the preview is staged. Re-check
            # here so a late cancel routes into the SyncCancelled branch (which
            # leaves ``pending_delta`` None) instead of staging a delta the
            # user already cancelled (#1202).
            if box.is_cancelling():
                raise SyncCancelled(_SYNC_CANCELLED)

            preview_id = self._uuid_gen.uuid4()
            created_at = self._clock.time()
            platforms_count = sum(1 for u in work_queue if u.type == "platform")
            collections_count = sum(1 for u in work_queue if u.type == "collection")

            answer = {
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
                # Absolute wall-clock deadline (epoch seconds) the backend stops
                # accepting this snapshot at. Absolute rather than a remaining-
                # seconds count because the panel runs on the same machine and the
                # same clock, and a deadline survives the Deck suspending where a
                # locally counted-down number does not.
                "expires_at": box.preview_deadline(created_at),
            }
            box.stage_preview(
                preview_id=preview_id,
                created_at=created_at,
                answer=answer,
            )

            await self.emit_progress(SyncStage.DONE, message="Preview ready", running=False)

            return answer
        except SyncCancelled:
            # sync_preview is a Decky callable — the frontend awaits its return.
            # Re-raising leaves that promise unsettled, so a user-initiated
            # cancel mid-preview returns the canonical failure shape instead of
            # propagating the cooperative cancel out of the callable (#1035).
            # SyncCancelled is a BaseException (not Exception), so it skips the
            # generic ``except Exception`` below and lands here as a distinct
            # cooperative signal — never conflated with a real asyncio cancel.
            box.discard_preview()
            await self._finish_sync(_SYNC_CANCELLED)
            return {"success": False, "reason": "cancelled", "message": _SYNC_CANCELLED}
        except Exception as e:
            import traceback

            self._logger.error(f"Sync preview failed: {e}\n{traceback.format_exc()}")
            box.discard_preview()
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
        if not box.matches_preview(preview_id):
            return {
                "success": False,
                "reason": ErrorCode.STALE_PREVIEW.value,
                "message": "Preview expired, please re-sync",
            }
        # The read drops an over-age snapshot on its way out, so a repeat apply
        # can't pick it up.
        if box.read_fresh_preview(self._clock.time()) is None:
            return {
                "success": False,
                "reason": ErrorCode.STALE_PREVIEW.value,
                "message": "Preview is older than 30 minutes, please re-run sync",
            }
        # Admission guard: a rapid second apply (or an apply landing while a
        # sync is already in flight) must be rejected without consuming the
        # staged delta, so the still-valid preview survives for the legitimate
        # apply (#1202). Claim the run slot BEFORE discarding the preview.
        run_id = self._uuid_gen.uuid4()
        if not box.try_begin_run(run_id):
            return {"success": False, "reason": "sync_in_progress", "message": "Sync already in progress"}
        box.discard_preview()
        box.sync_last_heartbeat = self._clock.monotonic()

        self._loop.create_task(self._do_sync_per_unit())

        return {"success": True, "message": "Applying changes"}

    def sync_cancel_preview(self):
        self._sync_state.discard_preview()
        return {"success": True}

    def get_pending_preview(self):
        """Hand back the staged preview answer, so a remounted panel can show its card again.

        ``preview: None`` — nothing staged, a snapshot the read aged out, or a
        run in flight (which withholds it rather than discarding it) — is a
        normal answer, never the failure shape. Starts and cancels no run.
        """
        delta = self._sync_state.read_restorable_preview(self._clock.time())
        return {"success": True, "preview": dict(delta.answer) if delta else None}

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
        """Return the latest progress frame plus whether a run is actually in flight.

        Idle returns the default ``running: False`` snapshot; a live run
        returns the latest snapshot written by :meth:`emit_progress`.

        ``inFlight`` is the run-lifecycle state itself, not a re-reading of the
        frame, and it rides only this answer — never an emitted ``sync_progress``
        event. The two can disagree, legitimately: during a cancel drain the
        CANCELLED frame already says ``running: False`` while the slot is still
        owned, and between ``try_begin_run`` and the run's first frame the
        opposite holds. It exists because the frontend cannot tell "the backend
        has no run" from "the backend has not said anything about this run yet"
        by looking at a frame — and the difference decides whether a panel may
        retract a run it believes is live. See ``src/components/MainPage.tsx``.
        """
        box = self._sync_state
        return {**box.sync_progress, "inFlight": box.is_in_flight()}

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
            None, self._shortcut_launch_resolver.do_read_installed_paths, {rom["id"] for rom in unit_roms}
        )
        core_overrides = await self._loop.run_in_executor(
            None, self._shortcut_launch_resolver.do_build_core_overrides, unit_roms
        )

        # Read the bound-row registry once, before the build: its persisted keys
        # seed the component keying (a fresh member edging into a DB-resident
        # sibling adopts its canonical summary) AND drive the group collapse below.
        registry = await self._loop.run_in_executor(None, self._local_library_reader.do_read_apply_registry, unit)
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
        # clean finish; the other half of the pair is
        # ``ChunkDispatcher._build_final_platform_stamp``, which rides the final
        # chunk's commit UoW. A fetch that failed raised before here (fetch failure ≠
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

        applied_count = await self._chunk_dispatcher.apply_unit_in_chunks(
            unit,
            unit_index=unit_index,
            total_units=total_units,
            emitted=apply_emitted,
            shortcuts_data=shortcuts_data,
            unit_roms=unit_roms,
            new_ids=new_ids,
            confirmed_cover_sources=confirmed_cover_sources,
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
                None,
                self._local_library_reader.do_scan_stale_roms,
                synced_rom_ids,
                set(self._sync_state.committed_app_ids),
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

        ``registry`` is the unit's bound-row projection (``do_read_apply_registry``)
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
