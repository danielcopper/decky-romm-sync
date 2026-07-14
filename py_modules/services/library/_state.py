"""Shared mutable state for the library sync pipeline.

Owned by :class:`LibraryService`; each sub-service receives a reference
so they can coordinate without back-refs to the façade. The contract:
sub-services mutate the box's coordination fields directly (it is the
single source of truth for in-flight sync run state); the façade exposes
property accessors over the box so external callers see a flat shape
rather than reaching through ``service._state.x``.

The run-lifecycle pair — ``sync_state`` and ``current_sync_id`` — is the
one exception: it is mutated **only** through the box's own verb methods
(``try_begin_run`` / ``request_cancel`` / ``finish_run``), never by direct
field assignment from a sub-service. Confining those two writes to the box
keeps run admission, cancellation, and termination a single
compare-and-swap on the one event loop, so a rapid Sync/Cancel can't leave
a half-reset run id (#1202). Enforced by ``scripts/check_sync_lifecycle_owner.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from domain.sync_state import SyncState

if TYPE_CHECKING:
    import asyncio

    from domain.preview_delta import PreviewDelta


def _default_progress() -> dict[str, Any]:
    return {
        "running": False,
        "stage": "",
        "current": 0,
        "total": 0,
        "message": "",
        "step": 0,
        "totalSteps": 0,
        "runId": "",
    }


@dataclass
class LibrarySyncStateBox:
    """In-memory state for one library sync run, plus held preview data.

    Holds the current ``SyncState`` (idle/running/cancelling), the
    generation id used to invalidate stale background work after the
    run ends, the heartbeat timestamp, the live progress dict emitted
    to the frontend, and the apply-staging dicts populated during
    ``sync_preview`` / ``sync_apply_delta`` and consumed by the
    per-unit pipeline.
    """

    sync_state: SyncState = SyncState.IDLE
    current_sync_id: str | None = None
    sync_last_heartbeat: float = 0.0
    sync_progress: dict[str, Any] = field(default_factory=_default_progress)
    pending_sync: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Every fetched ROM of the active unit (built shortcut-shape, keyed by
    # rom_id), not just the emitted representatives in ``pending_sync``. The
    # per-unit commit upserts an identity + version-metadata row for ALL of them
    # (ADR-0021 group-aware persist) while only representatives carry a binding.
    # Populated alongside ``pending_sync`` in ``_sync_one_unit`` and reset with
    # it; kept across the heartbeat-timeout abandon window so a late ack can
    # still drive the full persist.
    pending_all_roms: dict[int, dict[str, Any]] = field(default_factory=dict)
    pending_delta: PreviewDelta | None = None
    pending_collection_memberships: dict[str, list[int]] = field(default_factory=dict)
    pending_platform_rom_ids: set[int] | None = None
    # Per-unit pipeline coordination. ``unit_complete_event`` is set by
    # :meth:`SyncReporter.report_unit_results` when the frontend reports
    # back for the active unit; the orchestrator awaits it (with a
    # heartbeat-based timeout) before dispatching the next unit. Cleared
    # back to None between units. On a heartbeat **timeout** (not a user
    # cancel) the orchestrator does NOT clear ``pending_sync`` or null
    # ``unit_complete_event`` — it flags ``unit_abandoned`` instead, so a
    # late ``report_unit_results`` can still commit the delivered bindings
    # that the frontend already created Steam shortcuts for (#1052).
    unit_complete_event: asyncio.Event | None = None
    # Identity of the unit currently dispatched to the frontend: the
    # ``WorkUnit.id`` (a platform's numeric id or a collection's string id).
    # Set by the orchestrator just before it emits ``sync_apply_unit`` and
    # cleared once the unit's ack is committed (or the unit is cancelled).
    # ``SyncReporter.report_unit_results`` validates the ack against this and
    # ``current_sync_id`` (the run id) so a late ack from a cancelled run —
    # or a stray ack for a different unit — is ignored rather than credited
    # to the wrong unit/run (#1041). Kept (not cleared) across the
    # heartbeat-timeout abandon window so the late ack for the SAME unit still
    # validates; the cleared cross-run/cross-unit case is what it rejects.
    active_unit_id: int | str | None = None
    # 0-based index of the unit's apply chunk currently dispatched to the
    # frontend. A unit's emitted shortcuts are split into chunks emitted +
    # committed one at a time (each chunk is a durable checkpoint); this stamps
    # which chunk is in flight so ``SyncReporter.report_unit_results`` can reject
    # an ack for a stale chunk alongside the run/unit identity check. Set by the
    # orchestrator before each chunk's ``sync_apply_unit`` emit and cleared to
    # ``None`` once the unit's last chunk is committed (or the unit is cancelled).
    active_chunk_index: int | None = None
    # Holds the frontend-supplied ``rom_id_to_app_id`` mapping reported
    # for the active unit. Surfaces the result so the orchestrator can
    # accumulate the per-unit registry into the cross-run accumulators.
    last_unit_results: dict[str, int] | None = None
    # Set True when a per-unit wait times out on a stale heartbeat (not a
    # user cancel): the orchestrator abandoned the unit but the frontend
    # may still ack it. A late :meth:`SyncReporter.report_unit_results`
    # observes this flag and drives the per-unit commit itself so the
    # delivered bindings are persisted rather than discarded (#1052).
    unit_abandoned: bool = False
    # Set True (alongside ``unit_abandoned``) when a heartbeat timeout ends the
    # run, so the terminal ``SyncRun`` write records ``interrupted`` — an
    # external death (frontend crash/reload) — instead of ``cancelled``, which
    # is reserved for the user's own Cancel. Reset at the start of each run;
    # never reset by the per-chunk loop, so a timeout anywhere in the run wins.
    run_interrupted: bool = False
    # Set True when the session-budget gate stops the run deliberately at a chunk
    # boundary (Steam's renderer is near its heap budget). The terminal ``SyncRun``
    # write then records ``paused`` — a resumable, self-imposed stop distinct from
    # both ``cancelled`` (the user's Cancel) and ``interrupted`` (an external
    # death). Takes precedence over ``run_interrupted`` in the terminal branch.
    # Reset at the start of each run (#1383).
    run_paused: bool = False
    # The abandoned CHUNK's rows (the fetched ROMs of the in-flight chunk's
    # sibling groups, each the source of its ``metadatum``), stashed so a late
    # ack can rebuild ``acked_roms`` for the commit it drives. Only the chunk
    # that timed out is stashed — already-committed chunks stay committed. Reset
    # between chunks alongside ``last_unit_results``.
    pending_unit_roms: list[dict[str, Any]] = field(default_factory=list)
    # Every Steam appId bound by a ``commit_unit_results`` this run, across
    # BOTH the happy path and the heartbeat-timeout late-ack path (#1052).
    # The stale-removal scan excludes these so a new server-issued rom_id that
    # reuses an old appId (CRC32 of unchanged exe+name) can't wipe the shortcut
    # the run just bound (#1036). Reset at the start of each run.
    committed_app_ids: set[int] = field(default_factory=set)
    # Count of apply chunks emitted so far this run — the session-budget gate
    # skips its very first chunk (this counter still 0) so every run/resume makes
    # at least one chunk of forward progress before it can pause, never an
    # immediate no-progress pause loop. Incremented after each ``sync_apply_unit``
    # emit; reset at the start of each run (#1383).
    chunks_emitted_this_run: int = 0
    # Distinct terminal reason for an ``interrupted`` run, when the interrupt was
    # a deliberate session-budget pause rather than a heartbeat timeout. Set by
    # the gate alongside ``run_interrupted``; ``None`` leaves the interrupted
    # write on its default heartbeat-timeout reason. Surfaced in the
    # ``sync_complete`` payload so the UI shows the pause guidance distinctly.
    # Reset at the start of each run (#1383).
    interrupt_reason: str | None = None
    # One-shot guard so the session-budget gate logs "RSS unavailable" at most
    # once per run instead of on every chunk boundary (the reading is fail-open —
    # a ``None`` reading skips the gate). Reset at the start of each run (#1383).
    budget_measure_unavailable_logged: bool = False
    # Renderer RSS (KB) captured at run START — a RAW read (may include transient
    # garbage; not GC-settled), taken before any chunk is applied. The run-end
    # advisory read is differenced against it to report roughly how much the run
    # grew Steam's memory; the delta is an approximation for information only, which
    # a raw baseline is fine for. ``None`` when the run-start reading was unavailable
    # (delta then unmeasurable). Set at the start of each run (#1383).
    run_start_rss_kb: int | None = None
    # The run's planned ROM count — the same total the ``sync_plan`` event carries.
    # ``None`` until a run reaches its plan (no run made yet, or the plugin reloaded
    # and wiped the box). Set once per run, reset at the start of each run (#1383).
    run_total_items: int | None = None
    # Items of the run already correct in Steam: the delta-restricted apply's
    # per-unit SKIPPED entries (unchanged — the shortcut is already right) plus each
    # wholesale-skipped unit's ROMs, plus every COMMITTED chunk's acked items. An
    # emitted-but-uncommitted chunk (cancelled / abandoned) never counts. Read
    # against ``run_total_items`` by ``get_session_budget_status`` so the paused
    # banner can say "X of Y games done" — the counters live here, in the backend,
    # precisely because the plugin process survives the Steam restart the banner
    # asks for (only the frontend reloads). A plugin/backend reload DOES lose them
    # (in-memory, no migration); the banner then omits the sentence rather than
    # showing a wrong number. Reset at the start of each run (#1383).
    run_done_items: int = 0
    # Signed renderer-RSS growth (KB) of the last run to reach the terminal
    # finalize (end - start) — completed, paused, cancelled, and interrupted all
    # overwrite it with THEIR OWN delta (#36), so ``get_session_budget_status``
    # can surface "last run: ±X GB" on a QAM remount. An errored run aborts
    # before the finalize and keeps the prior value. In-memory only — lost on
    # plugin reload, which is acceptable (no migration). ``None`` when either
    # endpoint of that run was unmeasurable.
    last_run_delta_kb: int | None = None

    # ── Run lifecycle — the only writers of sync_state / current_sync_id ──
    #
    # These four methods are the sole mutators of the run-lifecycle pair. The
    # plugin runs on a single event loop, so a method body that reads then
    # writes ``sync_state`` without awaiting in between is a true atomic
    # compare-and-swap — nothing else can observe or change the pair mid-update.

    def try_begin_run(self, run_id: str) -> bool:
        """Claim the single in-flight run slot for ``run_id`` (compare-and-swap).

        Returns ``False`` with no state change when a run is already in flight
        (the admission guard a rapid second Sync/Apply hits); otherwise
        transitions IDLE → RUNNING, stamps ``current_sync_id``, and returns
        ``True``.
        """
        if self.sync_state is not SyncState.IDLE:
            return False
        self.sync_state = SyncState.RUNNING
        self.current_sync_id = run_id
        return True

    def request_cancel(self, run_id: str | None = None) -> str:
        """Request cancellation of the in-flight run, scoped to ``run_id``.

        Returns ``"no_sync"`` when nothing is in flight; ``"stale"`` when a
        truthy ``run_id`` does not match the active ``current_sync_id`` (the
        #1198/#1200 run-scoping, centralized here); otherwise flips to
        CANCELLING and returns ``"cancelling"``. A falsy ``run_id`` cancels
        unconditionally — the legacy no-id callers and the "no active id yet"
        safety case, so cancel is never made less reliable.
        """
        if self.sync_state is SyncState.IDLE:
            return "no_sync"
        if run_id and str(run_id) != str(self.current_sync_id):
            return "stale"
        self.sync_state = SyncState.CANCELLING
        return "cancelling"

    def finish_run(self, run_id: str | None) -> bool:
        """Return to IDLE only when ``run_id`` owns the slot (compare-and-reset).

        Resets to IDLE + nulls ``current_sync_id`` only when ``run_id`` equals
        the active ``current_sync_id``; a late, foreign, or doubled terminal is
        a no-op and can never null a freshly-started run. Returns ``True`` when
        it reset.
        """
        if str(run_id) != str(self.current_sync_id):
            return False
        self.sync_state = SyncState.IDLE
        self.current_sync_id = None
        return True

    def is_in_flight(self) -> bool:
        """True while a run is not IDLE (running or cancelling)."""
        return self.sync_state is not SyncState.IDLE

    def is_cancelling(self) -> bool:
        """True while a cancel has been requested for the in-flight run."""
        return self.sync_state is SyncState.CANCELLING

    def clear_active_unit(self) -> None:
        """Tear down the active unit's in-flight dispatch state.

        Resets the chunk-coordination quintet: the emitted + all-ROM staging
        (``pending_sync`` / ``pending_all_roms``), the ack event
        (``unit_complete_event``), and the unit + chunk identity
        (``active_unit_id`` / ``active_chunk_index``). The single teardown for a
        unit that finished, was cancelled, or whose inter-chunk window closed.
        NOT called on the heartbeat-timeout branch, which deliberately KEEPS this
        staging so a late ``report_unit_results`` can still commit the delivered
        bindings (#1052).
        """
        self.pending_sync = {}
        self.pending_all_roms = {}
        self.unit_complete_event = None
        self.active_unit_id = None
        self.active_chunk_index = None
