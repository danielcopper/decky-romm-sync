"""One work unit's apply, driven as a sequence of durable chunk round-trips.

Emit a chunk of the unit's delta shortcuts to the frontend, wait for its
acknowledgement against the heartbeat clock, commit it through the reporter,
and decide what becomes of a chunk whose acknowledgement never comes. That loop
is the whole module: *what* to apply is settled before it — the fetch, the
group collapse and the delta are
:class:`~services.library.sync_orchestrator.SyncOrchestrator`'s — and what the
run makes of the outcome is settled after it.

Nothing here opens a transaction or touches the outside world on its own:
every durable write goes through :class:`~services.library.reporter.SyncReporter`
and every message to the frontend through the injected emitter. That is why the
dependency surface carries no Unit-of-Work factory, no settings, no plugin
directory and no event loop — reaching for one would mean the round-trip had
started doing something other than dispatching.

**The run's lifecycle stays with the orchestrator.** A chunk is a step inside a
run, never its beginning or its end: this module may *request* a cancel (a
heartbeat timeout does, so the loop above it stops), but ``try_begin_run`` and
``finish_run`` are the orchestrator's alone.

The per-platform completion stamp is built here, on a platform unit's final
chunk, so "platform fully synced" ⟺ "stamp exists" is atomic on a crash. Its
other half — the DELETE that invalidates the stamp the moment a fresh apply
starts — is taken by ``SyncOrchestrator._sync_one_unit``
(``_clear_platform_stamp_io``); *where in the pipeline* that write falls is a
property of its call site, and the argument for the ordering lives there in
full.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.collection_sync_state import CollectionSyncState
from domain.platform_sync_state import PlatformSyncState
from domain.session_budget import CLIFF_KB, EFFECTIVE_CEILING_KB
from domain.sync_chunking import build_unit_chunks, wire_shortcuts

if TYPE_CHECKING:
    import logging

    from domain.work_unit import WorkUnit
    from lib.late_binding import LateBinding
    from services.library._state import LibrarySyncStateBox
    from services.library.reporter import SyncReporter
    from services.library.session_budget import SessionBudgetMonitor
    from services.protocols import Clock, EventEmitter, Sleeper


# Per-unit heartbeat-based timeout. If the frontend stops calling
# ``sync_heartbeat`` for this many seconds while the dispatcher is
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


@dataclass(frozen=True)
class ChunkDispatcherConfig:
    """Frozen wiring bundle handed to ``ChunkDispatcher.__init__``.

    Holds the logger, the event emitter the chunk frames go out through, the
    Clock/Sleeper seams the heartbeat wait is clocked by, the shared
    :class:`LibrarySyncStateBox` carrying the per-chunk coordination state, the
    :class:`SessionBudgetMonitor` the loop asks its two budget questions of at
    each chunk boundary, and the reporter every commit runs through. The
    ``reporter`` field is a :class:`LateBinding` because :class:`LibraryService`
    constructs this dispatcher before the reporter exists; the façade plugs it in
    via ``set()`` once the reporter is built.

    What is absent is the contract: no Unit-of-Work factory (the dispatcher
    opens no transaction), no event loop (it offloads nothing), no settings and
    no plugin directory (every decision it acts on was made before the round-trip
    began).
    """

    logger: logging.Logger
    emit: EventEmitter
    clock: Clock
    sleeper: Sleeper
    sync_state_box: LibrarySyncStateBox
    reporter: LateBinding[SyncReporter]
    session_budget: SessionBudgetMonitor


class ChunkDispatcher:
    """Drives one work unit's apply as emit → wait → commit, one chunk at a time."""

    def __init__(self, *, config: ChunkDispatcherConfig) -> None:
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock
        self._sleeper = config.sleeper
        self._sync_state = config.sync_state_box
        self._reporter = config.reporter
        self._session_budget = config.session_budget

    async def apply_unit_in_chunks(
        self,
        unit: WorkUnit,
        *,
        unit_index: int,
        total_units: int,
        emitted: list[dict[str, Any]],
        shortcuts_data: list[dict[str, Any]],
        unit_roms: list[dict[str, Any]],
        new_ids: set[int],
        confirmed_cover_sources: dict[int, str],
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
        restart shows the rest), never to a run pause. ``confirmed_cover_sources``
        is the unit's ``rom_id → applied cover source`` map — the third of the
        whole-unit staging dicts the reporter's commit reads. Returns the running
        count of shortcuts applied — a cancel or heartbeat timeout returns early
        with the chunks committed so far.
        """
        box = self._sync_state
        # Stage the DELTA representatives for cover finalise + binding, and the
        # full built set for the ack-independent identity + version persist (the
        # reporter upserts a row for every sibling — skipped ones included — and
        # binds only the delta's acked representatives). Staging stays whole-unit;
        # the apply is chunked below, so a mid-unit CEF crash forfeits only the
        # in-flight chunk, not every prior chunk. Written here rather than by the
        # caller so these three fields have one production writer module from the
        # moment they are staged to ``clear_active_unit``'s teardown.
        box.pending_sync = {e["rom_id"]: e for e in emitted}
        box.pending_all_roms = {sd["rom_id"]: sd for sd in shortcuts_data}
        box.pending_cover_sources = confirmed_cover_sources
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
                chunk_cover_refreshes = self._session_budget.clip_cover_refreshes(
                    cover_refreshes, rss_kb=rss_kb, creates=creates, updates=updates, limit_kb=budget_limit_kb
                )

            chunk_rows = [roms_by_id[rid] for rid in chunk.rom_ids if rid in roms_by_id]

            # Fresh per-chunk coordination: a new event + identity (run + unit +
            # chunk index) so the reporter validates each chunk's ack. These four
            # assignments MUST precede the emit below and nothing may be awaited
            # between them and it. The frontend can ack within milliseconds of
            # receiving the frame, and the reporter checks that ack against exactly
            # this identity: emit first and a fast ack is rejected as stray, the
            # event it would have set is never set, and the wait below runs its full
            # heartbeat timeout before giving up — a silent 60 s stall that then
            # stashes a chunk the frontend already applied (#1041 / #1052 / #1367).
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
                self._abandon_active_chunk(chunk_rows)
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

    def _abandon_active_chunk(self, chunk_rows: list[dict[str, Any]]) -> None:
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
        box = self._sync_state
        if box.is_cancelling():
            box.clear_active_unit()
        else:
            box.stash_abandoned_chunk(chunk_rows)
            box.run_interrupted = True
            box.request_cancel()

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
