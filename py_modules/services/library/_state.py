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

``pending_delta`` is held to the same discipline, unenforced: the staged
preview snapshot is written only through ``stage_preview`` /
``read_fresh_preview`` / ``discard_preview``, so its whole lifetime — when it
appears, when it ages out, when it is consumed — is readable in one place
rather than inferred from assignments scattered across the orchestrator. The
TTL lives here with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from domain.preview_delta import PreviewDelta, preview_expires_at
from domain.sync_state import SyncState

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping


PREVIEW_MAX_AGE_SECONDS = 1800  # 30 minutes — preview snapshots stale beyond this


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


@dataclass(frozen=True)
class CollectionMembership:
    """One enabled collection's full member rom_ids, tagged with its display name.

    The value type of :attr:`LibrarySyncStateBox.pending_collection_memberships`,
    whose key is a collision-free ``(collection_kind, collection_id)`` identity so
    two collections that merely SHARE a display name never overwrite each other's
    members in the finalize accumulator. The name rides here in the value because
    Steam's collection namespace is by-name: the reporter groups same-named
    memberships and UNIONs their resolved appIds into the one
    ``RomM: [<name>] (host)`` Steam collection (RomM permits same-named collections
    across kinds/users, so the plugin merges rather than owner-filters, #1503).

    ``kind`` (``"standard"`` / ``"smart"`` / ``"virtual"``) and — for the virtual
    kind — ``virtual_type`` (``"franchise"`` / ``"collection"``) ride here too so
    the reporter can build the fine display label under the ``by_label`` naming
    mode (``domain.collection_label``); in the default ``merge`` mode they are
    unused and same-named collections still union by name.
    """

    name: str
    rom_ids: list[int]
    kind: str
    virtual_type: str | None = None


@dataclass(frozen=True)
class AbandonedChunk:
    """A heartbeat-timed-out apply chunk, stashed for a late-ack commit.

    An inert snapshot that outlives the run that produced it: the
    run/unit/chunk identity a late ``report_unit_results`` must match, plus the
    chunk's fetched RomM rows (the ``metadatum`` source the recovery commit
    upserts). The whole-unit staging the commit also reads
    (``pending_sync`` / ``pending_all_roms`` / ``pending_cover_sources``) is
    kept live on the box, not copied here — this chunk's identity is what the
    late ack keys on. Held on :class:`LibrarySyncStateBox` **outside** the
    run-lifecycle state and cleared at the next run's start, so a frontend that
    crashes and never acks just leaves inert data until the next run (#1367).
    """

    run_id: str | None
    unit_id: int | str | None
    chunk_index: int | None
    chunk_rows: list[dict[str, Any]]


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
    # The latest progress frame, which ``get_sync_status`` hands a remounting
    # QAM so it can recover a live run without waiting for the next event. It
    # describes an IN-FLIGHT run only: ``finish_run`` puts it back to the idle
    # default, because a finished run's terminal frame is an event the panel was
    # already sent, and a panel that merely FINDS one on a later mount is
    # required to ignore it (#1019) — so leaving it here answered every mount
    # with a run that ended, message and run id included, for as long as the
    # plugin stayed loaded.
    sync_progress: dict[str, Any] = field(default_factory=_default_progress)
    pending_sync: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Every fetched ROM of the active unit (built shortcut-shape, keyed by
    # rom_id), not just the emitted representatives in ``pending_sync``. The
    # per-unit commit upserts an identity + version-metadata row for ALL of them
    # (ADR-0021 group-aware persist) while only representatives carry a binding.
    # Populated alongside ``pending_sync`` by ``ChunkDispatcher`` and reset with
    # it; kept across the heartbeat-timeout abandon window so a late ack can
    # still drive the full persist.
    pending_all_roms: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Confirmed cover fingerprints for the active unit: rom_id → the fresh RomM
    # cover source whose bytes the artwork layer just put (or confirmed) in the
    # per-ROM cover cache during this unit's cover download. The per-unit commit
    # merges these onto the upserted Rom rows (``Rom.adopt_cover_source``);
    # a rom absent here keeps its persisted fingerprint — a failed download
    # never advances it (#1386). Populated alongside ``pending_sync`` by
    # ``ChunkDispatcher`` and reset with it; kept across the heartbeat-timeout
    # abandon window so a late ack still stamps the confirmed values.
    pending_cover_sources: dict[int, str] = field(default_factory=dict)
    # The staged preview snapshot, between ``sync_preview`` and the apply that
    # consumes it. Written only through the preview-lifecycle verbs below.
    pending_delta: PreviewDelta | None = None
    # Per-run collection-membership accumulator, keyed by a collision-free
    # ``(collection_kind, collection_id)`` identity (never the display name), so
    # two collections that share a name each keep their own members. The name
    # travels in the :class:`CollectionMembership` value; the reporter unions
    # same-named memberships when it builds the by-name Steam-collection map
    # (#1503).
    pending_collection_memberships: dict[tuple[str, str], CollectionMembership] = field(default_factory=dict)
    pending_platform_rom_ids: set[int] | None = None
    # Per-unit pipeline coordination. ``unit_complete_event`` is set by
    # :meth:`SyncReporter.report_unit_results` when the frontend acks the
    # active chunk; ``ChunkDispatcher`` awaits it (with a heartbeat-based
    # timeout) before dispatching the next chunk. Cleared back to None
    # between chunks and on either wait-give-up branch (user cancel and
    # heartbeat timeout). On a heartbeat timeout the abandoned chunk is moved
    # into ``abandoned_chunk`` (``stash_abandoned_chunk``), so its late
    # ``report_unit_results`` still commits the bindings the frontend already
    # created Steam shortcuts for (#1052 / #1367).
    unit_complete_event: asyncio.Event | None = None
    # Identity of the unit currently dispatched to the frontend: the
    # ``WorkUnit.id`` (a platform's numeric id or a collection's string id).
    # Set by ``ChunkDispatcher`` just before it emits ``sync_apply_unit`` and
    # cleared once the unit's ack is committed, the unit is cancelled, or the
    # chunk times out (its identity is moved into ``abandoned_chunk``).
    # ``SyncReporter.report_unit_results`` validates a live ack against this and
    # ``current_sync_id`` (the run id) so a stray ack for a different unit — or a
    # late ack from a cancelled run — is ignored rather than credited to the
    # wrong unit/run (#1041); a timed-out chunk's late ack instead matches the
    # ``abandoned_chunk`` stash by identity, after this field has been cleared
    # (#1367).
    active_unit_id: int | str | None = None
    # 0-based index of the unit's apply chunk currently dispatched to the
    # frontend. A unit's emitted shortcuts are split into chunks emitted +
    # committed one at a time (each chunk is a durable checkpoint); this stamps
    # which chunk is in flight so ``SyncReporter.report_unit_results`` can reject
    # an ack for a stale chunk alongside the run/unit identity check. Set by
    # ``ChunkDispatcher`` before each chunk's ``sync_apply_unit`` emit and cleared to
    # ``None`` once the unit's last chunk is committed, the unit is cancelled, or
    # the chunk times out (its identity is moved into ``abandoned_chunk``).
    active_chunk_index: int | None = None
    # Holds the frontend-supplied ``rom_id_to_app_id`` mapping reported
    # for the active unit. Surfaces the result so ``ChunkDispatcher`` can
    # accumulate the per-unit registry into the cross-run accumulators.
    last_unit_results: dict[str, int] | None = None
    # A heartbeat-timed-out apply chunk, stashed for its late
    # :meth:`SyncReporter.report_unit_results` to commit **after** the run has
    # already wound down (``finish_run`` nulled ``current_sync_id``, so the
    # active-unit ack check can no longer match). ``None`` outside the window
    # between a chunk's heartbeat timeout and its late ack (or the next run
    # start, whichever comes first). Written only through the box verbs
    # ``stash_abandoned_chunk`` (on timeout) / ``take_abandoned_chunk`` (on the
    # matching late ack) / ``try_begin_run`` (cleared at the next run start).
    # The recovery commit it enables is what makes #1052 reachable in
    # production (#1367).
    abandoned_chunk: AbandonedChunk | None = None
    # Set True when a heartbeat timeout ends the run, so the terminal
    # ``SyncRun`` write records ``interrupted`` — an external death (frontend
    # crash/reload) — instead of ``cancelled``, which is reserved for the user's
    # own Cancel. Reset at the start of each run; never reset by the per-chunk
    # loop, so a timeout anywhere in the run wins.
    run_interrupted: bool = False
    # Set True when the session-budget gate stops the run deliberately at a chunk
    # boundary (Steam's renderer is near its heap budget). The terminal ``SyncRun``
    # write then records ``paused`` — a resumable, self-imposed stop distinct from
    # both ``cancelled`` (the user's Cancel) and ``interrupted`` (an external
    # death). Takes precedence over ``run_interrupted`` in the terminal branch.
    # Reset at the start of each run (#1383).
    run_paused: bool = False
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
    # Distinct terminal reason for a run stopped early on purpose, when the stop
    # was a deliberate session-budget pause rather than a heartbeat timeout. Set
    # by the gate alongside ``run_paused`` (never ``run_interrupted``); ``None``
    # leaves an interrupted write on its default heartbeat-timeout reason.
    # Surfaced in the ``sync_complete`` payload so the UI shows the pause
    # guidance distinctly. Reset at the start of each run (#1383).
    interrupt_reason: str | None = None
    # One-shot latch: once a run finds the renderer's RSS unreadable it stops
    # re-reading (and stops re-logging) for the rest of that run — the reading is
    # fail-open, so a ``None`` reading skips the gate. Re-armed at the start of each
    # run by ``SessionBudgetMonitor.record_run_start_baseline``, which is the only
    # writer besides the gate's own measure path (#1383).
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
        transitions IDLE → RUNNING, stamps ``current_sync_id``, drops any
        abandoned-chunk stash left by a prior run, and returns ``True``.
        """
        if self.sync_state is not SyncState.IDLE:
            return False
        self.sync_state = SyncState.RUNNING
        self.current_sync_id = run_id
        # Bounded stash lifetime: a heartbeat-timed-out chunk whose late ack
        # never arrived is dropped when the next run starts, so stale abandoned
        # data can never outlive one run (#1367).
        self.abandoned_chunk = None
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
        it reset. The progress snapshot goes back to the idle default with it,
        so ``get_sync_status`` answers a finished run the way it answers a
        plugin that has never run — see :attr:`sync_progress`.
        """
        if str(run_id) != str(self.current_sync_id):
            return False
        self.sync_state = SyncState.IDLE
        self.current_sync_id = None
        # Ordering: every run emits or SCHEDULES its terminal frame before
        # reaching the ``finally: finish_run(run_id)`` that lands here, so this
        # drops a snapshot the frontend has already been sent as an event — never
        # one it is still waiting for. The per-unit error path schedules its emit
        # through ``create_task``, which may run after this line; that is safe
        # only because the coroutine binds the ERROR frame BY VALUE and this
        # rebinds the attribute rather than mutating the dict it holds. A future
        # in-place edit here would corrupt a frame already handed to a pending
        # task. Not itself a frame either way: nothing emits this one, so no
        # ``running: False`` reaches the panel mid-run from this line.
        self.sync_progress = _default_progress()
        return True

    def is_in_flight(self) -> bool:
        """True while a run is not IDLE (running or cancelling)."""
        return self.sync_state is not SyncState.IDLE

    def is_cancelling(self) -> bool:
        """True while a cancel has been requested for the in-flight run."""
        return self.sync_state is SyncState.CANCELLING

    # ── Preview snapshot — the only writers of pending_delta ──

    def stage_preview(
        self,
        *,
        preview_id: str,
        created_at: float,
        answer: Mapping[str, Any],
    ) -> None:
        """Hold the computed preview for the apply — and for a panel that comes back to it.

        ``answer`` is the exact dict ``sync_preview`` returned; a restored card
        is that payload, never a second assembly of it. Replaces any snapshot
        already staged: only the newest preview is appliable.
        """
        self.pending_delta = PreviewDelta(
            preview_id=preview_id,
            created_at=created_at,
            answer=answer,
        )

    def preview_deadline(self, created_at: float) -> float:
        """The wall clock this box stops accepting a snapshot taken at ``created_at``.

        The frontend counts down against it, so the number it shows and the
        verdict :meth:`read_fresh_preview` reaches come from the same TTL.
        """
        return preview_expires_at(created_at, PREVIEW_MAX_AGE_SECONDS)

    def read_fresh_preview(self, now: float) -> PreviewDelta | None:
        """The staged snapshot while it is still appliable, else ``None``.

        A snapshot past its TTL at wall-clock ``now`` is discarded here rather
        than returned, so no caller can be handed one the apply would refuse.
        """
        delta = self.pending_delta
        if delta is None:
            return None
        if delta.is_expired(now, PREVIEW_MAX_AGE_SECONDS):
            self.pending_delta = None
            return None
        return delta

    def read_restorable_preview(self, now: float) -> PreviewDelta | None:
        """The staged snapshot a returning panel may put back on screen, else ``None``.

        Everything :meth:`read_fresh_preview` refuses, plus one more: while a
        run is in flight the snapshot is **withheld, never discarded**. A panel
        that remounts mid-run must show that run, not a card that would render
        over its progress rows — and the same snapshot is handed back on the
        next mount after the run ends, as long as it is still inside its TTL.
        Withholding is this reader's alone: the apply path must keep answering
        an overlapping apply with its own refusal, not this one's silence.
        """
        if self.is_in_flight():
            return None
        return self.read_fresh_preview(now)

    def matches_preview(self, preview_id: str) -> bool:
        """Whether the staged snapshot is the one ``preview_id`` names.

        Identity only — a match says nothing about the snapshot's age.
        """
        return self.pending_delta is not None and self.pending_delta.preview_id == preview_id

    def discard_preview(self) -> None:
        """Drop the staged snapshot — consumed by an apply, cancelled, or failed."""
        self.pending_delta = None

    # ── Abandoned-chunk stash — the heartbeat-timeout recovery seam ──

    def stash_abandoned_chunk(self, chunk_rows: list[dict[str, Any]]) -> None:
        """Move the timed-out chunk's identity + rows into the abandoned-chunk stash.

        Snapshots the active run/unit/chunk identity and the chunk's fetched
        rows into :attr:`abandoned_chunk` — inert data that survives the run's
        teardown — then clears the active-unit dispatch identity (the ack event
        and the unit/chunk id) so a late ``report_unit_results`` routes through
        the stash (:meth:`take_abandoned_chunk`) rather than the now-dead
        active-unit path. The whole-unit staging (``pending_sync`` /
        ``pending_all_roms`` / ``pending_cover_sources``) is deliberately left
        live so the reporter's late-ack commit can still read it; it is
        overwritten by the next run's first chunk. Only the heartbeat-timeout
        branch stashes; a user cancel discards the chunk via
        :meth:`clear_active_unit`.
        """
        self.abandoned_chunk = AbandonedChunk(
            run_id=self.current_sync_id,
            unit_id=self.active_unit_id,
            chunk_index=self.active_chunk_index,
            chunk_rows=chunk_rows,
        )
        self.unit_complete_event = None
        self.active_unit_id = None
        self.active_chunk_index = None

    def take_abandoned_chunk(
        self, run_id: str | None, unit_id: int | str | None, chunk_index: int
    ) -> AbandonedChunk | None:
        """Pop the abandoned-chunk stash iff the late ack's identity matches it.

        A late ``report_unit_results`` whose ``run_id`` / ``unit_id`` /
        ``chunk_index`` equals the stashed chunk's identity clears the stash (so
        a duplicate late ack finds nothing) and returns it for the recovery
        commit. Any other identity — or no stash — returns ``None`` and leaves
        the stash intact. ``run_id`` / ``unit_id`` compare by string value (a
        platform id is numeric, a collection id is a string) and ``chunk_index``
        as an int — the same coercion the active-unit ack check uses (#1041).
        """
        stash = self.abandoned_chunk
        if stash is None:
            return None
        if (
            str(run_id) == str(stash.run_id)
            and str(unit_id) == str(stash.unit_id)
            and int(chunk_index) == stash.chunk_index
        ):
            self.abandoned_chunk = None
            return stash
        return None

    def clear_active_unit(self) -> None:
        """Tear down the active unit's in-flight dispatch state.

        Resets the chunk-coordination state: the emitted + all-ROM + cover-
        fingerprint staging (``pending_sync`` / ``pending_all_roms`` /
        ``pending_cover_sources``), the ack event (``unit_complete_event``), and
        the unit + chunk identity (``active_unit_id`` / ``active_chunk_index``).
        The single teardown for a unit that finished, was cancelled, or whose
        inter-chunk window closed. NOT called on the heartbeat-timeout branch —
        that path moves the chunk into ``abandoned_chunk`` via
        :meth:`stash_abandoned_chunk` (which clears only the dispatch identity,
        not the staging), so a late ``report_unit_results`` can still read the
        whole-unit staging and commit the delivered bindings (#1052 / #1367).
        """
        self.pending_sync = {}
        self.pending_all_roms = {}
        self.pending_cover_sources = {}
        self.unit_complete_event = None
        self.active_unit_id = None
        self.active_chunk_index = None
