"""PlaytimeService — playtime tracking via RomM's native play-session ingest.

Owns per-ROM play sessions: opening a session, folding its duration into the
``Playtime`` aggregate on close, holding closed sessions in a durable outbox,
flushing them to RomM's native ``/api/play-sessions`` (best-effort, offline-safe),
and reconciling the local cumulative total against the cross-device server union
(ADR-0018). It also owns the durable "re-sign-in to enable cross-device playtime"
notice: a reconcile GET that 403s (the token predates the ``roms.user.read``
scope) raises the flag; a later successful GET, or a fresh sign-in, clears it.
All durable state lives in the ``rom_playtime`` / ``rom_playtime_sessions`` tables
and the ``kv_config`` scalar surface behind the Unit of Work; all RomM
communication goes through ``RommPlaytimeApi``. No ``import decky``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.iso_time import parse_iso
from domain.playtime import (
    Playtime,
    coerce_duration_ms,
    is_ingestable_session,
    latest_end_time,
    rejected_session_indices,
)
from lib.errors import RommForbiddenError, RommNotFoundError, RommUnprocessableEntityError
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging

    from models.play_sessions import PlaySessionIngestEntry, PlaySessionIngestResult

    from domain.playtime import PendingSessionRow
    from services.protocols import (
        Clock,
        DebugLogger,
        DeviceIdProvider,
        RetryStrategy,
        RommPlaytimeApi,
        UnitOfWorkFactory,
    )

# RomM accepts at most 100 sessions per ingest POST. A larger backlog (offline
# for many sessions) flushes incrementally: each launch/session-end/reconcile
# drains up to this many, so a >100 outbox catches up across successive flushes.
_FLUSH_BATCH_LIMIT = 100

# A single outbox row that keeps drawing an ``error`` ingest verdict is
# quarantined (dropped — only playtime is lost) once it reaches this many
# failures, so one permanently-rejected session cannot wedge the whole outbox.
_MAX_INGEST_ATTEMPTS = 5

# Durable ``kv_config`` flag: set when a reconcile GET 403s (the token lacks the
# ``roms.user.read`` scope), cleared on a later 200 or a fresh sign-in. The QAM
# banner reads it via ``get_playtime_scope_notice``.
_SCOPE_NOTICE_KEY = "playtime_scope_notice"


@dataclass(frozen=True)
class PlaytimeServiceConfig:
    """Frozen wiring bundle handed to ``PlaytimeService.__init__``.

    Holds the Protocol-typed RomM adapter and retry strategy, the device-id
    provider (the server identity that attributes native play-session ingests),
    runtime infrastructure, the clock/debug-logger seams, and the SQLite
    Unit-of-Work factory (the transactional seam over the ``rom_playtime``
    aggregate and the ``kv_config`` scope-notice flag this service reads and
    writes).
    """

    romm_api: RommPlaytimeApi
    retry: RetryStrategy
    device_id_provider: DeviceIdProvider
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    log_debug: DebugLogger
    uow_factory: UnitOfWorkFactory


def _empty_reconcile_result(*, server_query_failed: bool) -> dict[str, Any]:
    """The reconcile result for a missing local row — zero totals, no last-played."""
    return {
        "total_seconds": 0,
        "session_count": 0,
        "last_played": None,
        "server_query_failed": server_query_failed,
    }


def _session_debug_line(
    rom_id: int, started_at: str, ended_at: str, monotonic_start: float | None, monotonic_end: float, awake: int
) -> str:
    """Format the #1148 session-end verification line: wall span vs the counted awake span.

    ``wall`` is the raw start→end span; ``mono`` is the monotonic delta that
    excludes suspend time (``n/a`` when the row predates the monotonic marker and
    the domain falls back to wall); ``awake`` is the seconds actually counted. It
    is the on-device hook for confirming suspend exclusion. The timestamps are
    already known-parseable (``record_session`` validated them before this runs),
    so a parse miss degrades to ``-1`` rather than raising.
    """
    start = parse_iso(started_at)
    end = parse_iso(ended_at)
    try:
        wall = int((end - start).total_seconds()) if start is not None and end is not None else -1
    except TypeError:  # naive/aware mismatch — record_session would have rejected it first
        wall = -1
    mono = "n/a" if monotonic_start is None else str(int(monotonic_end - monotonic_start))
    return f"record_session_end: rom {rom_id} wall={wall}s mono={mono}s awake={awake}s"


class PlaytimeService:
    """Playtime tracking: record sessions, flush the outbox, and reconcile with RomM."""

    def __init__(self, *, config: PlaytimeServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._device_id_provider = config.device_id_provider
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._log_debug = config.log_debug
        self._uow_factory = config.uow_factory

    # ------------------------------------------------------------------
    # Session recording
    # ------------------------------------------------------------------

    def record_session_start(self, rom_id: int) -> dict[str, Any]:
        """Record the start of a play session for playtime tracking.

        Opens (or re-opens) the session marker on the ROM's ``Playtime``
        aggregate in a short write UoW. A ``rom_id`` with no matching ``roms``
        row violates the FK at commit; that is reported as a failure rather
        than auto-creating an identity anchor (ADR-0007).
        """
        rid = int(rom_id)
        try:
            with self._uow_factory() as uow:
                pt = uow.playtime.get(rid) or Playtime()
                pt.begin_session(self._clock.now().isoformat(), monotonic=self._clock.monotonic())
                uow.playtime.save(rid, pt)
        except sqlite3.IntegrityError as e:
            self._log_debug(f"Failed to record session start for rom {rid}: {e}")
            return {"success": False, "reason": "unknown_rom", "message": "Unknown ROM"}
        return {"success": True}

    async def record_session_end(self, rom_id: int) -> dict[str, Any]:
        """Record end of play session, accumulate playtime delta.

        Only handles playtime — save sync is handled separately. Suspend time is
        excluded via the monotonic clock: the session was opened with a
        ``Clock.monotonic()`` start, and the delta to the ``monotonic_end``
        captured here counts only awake time (the monotonic clock pauses while
        the device is suspended, #1148). The work runs in an executor: the
        durable fold + outbox enqueue happen in a short write UoW (the SQLite
        connection has thread affinity), then the native ingest flush runs
        best-effort outside any transaction.
        """
        return await self._loop.run_in_executor(None, self._record_session_end_io, int(rom_id))

    def _record_session_end_io(self, rom_id: int) -> dict[str, Any]:
        """Synchronous twin of :meth:`record_session_end` (runs in the executor).

        Phase A — fold the closed session (awake-only span, suspend excluded via
        the monotonic delta) into the aggregate and, when this device is
        registered, enqueue the session into the outbox, both in one short write
        UoW. Phase B — flush the outbox to RomM's native ingest outside the
        transaction (best-effort). Returns the same dict shape the frontend
        consumes: ``success`` plus ``duration_sec`` / ``total_seconds`` /
        ``session_count`` on the happy path, or ``success: False`` with a
        ``message`` otherwise.
        """
        ended_at = self._clock.now().isoformat()
        # Capture the monotonic reading at the same session-end instant as
        # ``ended_at``; its delta from the stored monotonic start is the awake-only
        # span (the monotonic clock pauses across suspend, #1148).
        monotonic_end = self._clock.monotonic()
        # Read the device id BEFORE opening the write UoW: get_device_id opens its
        # own DeviceRegistry UoW, and a nested BEGIN IMMEDIATE inside our open
        # write transaction would self-deadlock on the write lock.
        device_id = self._device_id_provider.get_device_id()
        try:
            with self._uow_factory() as uow:
                entry = uow.playtime.get(rom_id)
                if entry is None or not entry.last_session_start:
                    return {"success": False, "reason": "no_active_session", "message": "No active session"}
                # Capture the start markers BEFORE record_session clears them —
                # started_at keys the outbox row and RomM's dedup; the monotonic
                # start feeds the verification log line.
                started_at = entry.last_session_start
                monotonic_start = entry.last_session_start_monotonic
                try:
                    entry.record_session(ended_at, monotonic_end=monotonic_end)
                except ValueError:
                    return {
                        "success": False,
                        "reason": ErrorCode.UNKNOWN.value,
                        "message": "Failed to calculate session duration",
                    }
                duration = entry.last_session_duration_sec or 0
                self._log_debug(
                    _session_debug_line(rom_id, started_at, ended_at, monotonic_start, monotonic_end, duration)
                )
                if not device_id:
                    # Unregistered device: fold locally, never enqueue (an empty
                    # device id must never reach the wire, ADR-0018 decision #8).
                    self._log_debug(f"record_session_end: rom {rom_id} not enqueued — device unregistered")
                elif not is_ingestable_session(started_at, ended_at):
                    # A window that starts and ends inside one wall-clock second is
                    # a mis-fired launch (#1305), not real play, and RomM 422s the
                    # WHOLE ingest batch on it (#1312) — so it never enters the
                    # outbox. The duration is still folded into the local total
                    # above; only the native ingest is skipped.
                    self._log_debug(
                        f"record_session_end: rom {rom_id} sub-second session not enqueued "
                        f"(start={started_at}, end={ended_at})"
                    )
                else:
                    entry.enqueue_session(
                        device_id=device_id,
                        start_time=started_at,
                        end_time=ended_at,
                        duration_ms=duration * 1000,
                    )
                uow.playtime.save(rom_id, entry)
                total_seconds = entry.total_seconds
                session_count = entry.session_count
        except sqlite3.IntegrityError as e:
            self._log_debug(f"Failed to record session end for rom {rom_id}: {e}")
            return {"success": False, "reason": "unknown_rom", "message": "Unknown ROM"}

        # Best-effort native-ingest flush (outside the UoW). _flush_pending_sessions_io
        # is itself never-raising, but the outer catch stays as defence-in-depth so a
        # future regression there can never escape and discard this successful record
        # (the local total and the outbox are already persisted; #971 breadcrumb).
        try:
            self._flush_pending_sessions_io()
        except Exception as e:
            self._log_debug(f"record_session_end: play-session flush failed (non-fatal) for rom {rom_id}: {e}")

        return {
            "success": True,
            "duration_sec": duration,
            "total_seconds": total_seconds,
            "session_count": session_count,
        }

    # ------------------------------------------------------------------
    # Outbox flush (native ingest)
    # ------------------------------------------------------------------

    async def flush_pending_sessions(self) -> None:
        """Flush the pending-session outbox to RomM's native ingest (best-effort).

        Scheduled as a fire-and-forget background task (e.g. on session start)
        so an offline backlog catches up on the next reconnect. Not a Decky
        callable — internal orchestration only.
        """
        await self._loop.run_in_executor(None, self._flush_pending_sessions_io)

    def _flush_pending_sessions_io(self) -> None:
        """Synchronous twin of :meth:`flush_pending_sessions` (runs in the executor).

        Never raises: a flush is best-effort and offline-safe, so any failure
        (a locked UoW, a transport error, a malformed response) is logged at
        debug and swallowed — the outbox stays intact and catches up on the next
        flush. The actual gather/POST/dequeue work lives in
        :meth:`_flush_pending_sessions_worker`; this wrapper is the single
        never-raise boundary both callers (session-end, reconcile) rely on.
        """
        try:
            self._flush_pending_sessions_worker()
        except Exception as e:
            self._log_debug(f"Play-session flush failed (non-fatal): {e}")

    def _flush_pending_sessions_worker(self) -> None:
        """Gather → POST-per-device → dequeue the outbox (may raise; wrapped above).

        Reads up to ``_FLUSH_BATCH_LIMIT`` outbox rows directly (O(pending), not
        O(library)), groups them by the row's stored ``device_id`` (each ingest
        POST carries exactly one device_id), and for each group POSTs to
        ``/api/play-sessions`` strictly between the two UoWs (never holding a
        transaction across network I/O, ADR-0006). Accepted rows (``created`` /
        ``duplicate``) dequeue; a ``skipped`` row — the server's explicit
        rejection of that exact window — is dropped as terminal (the server
        draws the same verdict forever); an ``error`` or any unknown acknowledged
        status increments the attempt counter and is quarantined once it exhausts
        ``_MAX_INGEST_ATTEMPTS`` (never dropping data on an ambiguous verdict);
        rows absent from the response (or a transport failure) stay queued
        untouched.
        """
        with self._uow_factory() as uow:
            rows = uow.playtime.iter_pending_sessions(_FLUSH_BATCH_LIMIT)
        if not rows:
            return

        groups: dict[str, list[PendingSessionRow]] = {}
        for row in rows:
            groups.setdefault(row.device_id, []).append(row)

        sent_by_rom: dict[int, list[str]] = {}
        failed_by_rom: dict[int, list[str]] = {}
        quarantine_by_rom: dict[int, list[str]] = {}
        rejected_by_rom: dict[int, list[str]] = {}
        undrained: list[PendingSessionRow] = []

        for device_id, group in groups.items():
            self._flush_device_group(
                device_id,
                group,
                sent_by_rom=sent_by_rom,
                failed_by_rom=failed_by_rom,
                quarantine_by_rom=quarantine_by_rom,
                rejected_by_rom=rejected_by_rom,
                undrained=undrained,
            )

        if undrained:
            undrained_roms = sorted({row.rom_id for row in undrained})
            self._log_debug(
                f"Play-session flush: {len(undrained)} submitted session(s) not accepted; roms {undrained_roms}"
            )

        if not (sent_by_rom or failed_by_rom or quarantine_by_rom or rejected_by_rom):
            return

        self._apply_flush_outcome(sent_by_rom, failed_by_rom, quarantine_by_rom, rejected_by_rom)

    def _flush_device_group(
        self,
        device_id: str,
        group: list[PendingSessionRow],
        *,
        sent_by_rom: dict[int, list[str]],
        failed_by_rom: dict[int, list[str]],
        quarantine_by_rom: dict[int, list[str]],
        rejected_by_rom: dict[int, list[str]],
        undrained: list[PendingSessionRow],
    ) -> None:
        """POST one device's batch and sort each row into sent / failed / quarantine / rejected.

        A transport failure on the POST is non-fatal (mirrors
        ``_close_negotiate_session``): the group's rows stay queued and retry on
        the next flush. A whole-request **404** is peeled off that catch-all for
        its own log line but keeps the same retain-untouched handling: RomM
        NULL-resolves an unknown device or rom instead of rejecting, so a 404
        indicts the route rather than the sessions, and quarantining the outbox
        over it would lose playtime to a recoverable server-side
        misconfiguration. A whole-request 422 — RomM validates the ``sessions``
        array atomically and rejects the ENTIRE POST if any entry is invalid — is
        healed by :meth:`_handle_batch_rejection`: the server-flagged entries drop
        and the survivors resubmit, so one poison row never blocks the batch
        (#1312). Per-row 2xx verdicts are correlated back by the response
        ``index`` (position in this group's submitted batch).
        """
        batch: list[PlaySessionIngestEntry] = [self._entry_for_row(row) for row in group]
        try:
            response = self._romm_api.ingest_play_sessions(device_id, batch)
        except RommUnprocessableEntityError as exc:
            self._handle_batch_rejection(
                device_id,
                group,
                exc,
                sent_by_rom=sent_by_rom,
                failed_by_rom=failed_by_rom,
                quarantine_by_rom=quarantine_by_rom,
                rejected_by_rom=rejected_by_rom,
                undrained=undrained,
            )
            return
        except RommNotFoundError as e:
            # The ENDPOINT answered 404, which is not a verdict on these sessions:
            # RomM resolves an unknown device or rom to NULL rather than rejecting
            # (ADR-0018), so a 404 here points at the route — a misrouted tunnel, a
            # server that does not expose the endpoint — which is recoverable
            # server-side. Retain the whole group untouched rather than advancing
            # the counter, since bumping would quarantine the ENTIRE outbox within
            # _MAX_INGEST_ATTEMPTS flushes over a misconfiguration. Peeled off the
            # catch-all anyway so the log names the cause instead of burying it in
            # the generic transport line.
            self._log_debug(
                f"Play-session ingest endpoint answered 404 for device {device_id} — "
                f"retaining {len(group)} queued session(s) untouched: {e}"
            )
            undrained.extend(group)
            return
        except Exception as e:
            # No service-level retry wrap — a flush failure is non-fatal and
            # catches up next time. The whole group stays queued.
            self._log_debug(f"Play-session ingest failed (non-fatal): {e}")
            undrained.extend(group)
            return

        verdict_by_index: dict[int, PlaySessionIngestResult] = {}
        for result in response.get("results", []):
            index = result.get("index", -1)
            if 0 <= index < len(group):
                verdict_by_index[index] = result

        for index, row in enumerate(group):
            result = verdict_by_index.get(index)
            status = result.get("status") if result is not None else None
            detail = result.get("detail") if result is not None else None
            self._route_2xx_verdict(
                row,
                status,
                detail,
                sent_by_rom=sent_by_rom,
                failed_by_rom=failed_by_rom,
                quarantine_by_rom=quarantine_by_rom,
                rejected_by_rom=rejected_by_rom,
                undrained=undrained,
            )

    @staticmethod
    def _entry_for_row(row: PendingSessionRow) -> PlaySessionIngestEntry:
        """Project one outbox row to the ``PlaySessionIngestEntry`` wire shape."""
        return {
            "rom_id": row.rom_id,
            "start_time": row.start_time,
            "end_time": row.end_time,
            "duration_ms": row.duration_ms,
        }

    def _route_2xx_verdict(
        self,
        row: PendingSessionRow,
        status: str | None,
        detail: object,
        *,
        sent_by_rom: dict[int, list[str]],
        failed_by_rom: dict[int, list[str]],
        quarantine_by_rom: dict[int, list[str]],
        rejected_by_rom: dict[int, list[str]],
        undrained: list[PendingSessionRow],
    ) -> None:
        """Sort one row into a bucket from its acknowledged (2xx) per-session verdict.

        Shared by the batch loop and the per-session fallback so the two agree on
        the verdict taxonomy exactly.
        """
        if status in ("created", "duplicate"):
            # Both are successful ingests — dequeue.
            sent_by_rom.setdefault(row.rom_id, []).append(row.start_time)
            return
        if status is None:
            # Absent from a 2xx response (or a malformed row missing ``status``):
            # a transient omission, not a verdict. Stay queued untouched — no
            # attempt bump — and retry on the next flush.
            undrained.append(row)
            return
        if status == "skipped":
            # An EXPLICIT server rejection for this exact (device, rom,
            # start_time) window (e.g. a sub-second launch-death rejected on
            # validation). Re-POSTing the byte-identical row draws the same
            # verdict forever, so draining it is the honest terminal action (only
            # playtime is lost). Log once at info — the single line is the record.
            # Only ``skipped`` hard-drops: an outbox session exists nowhere else,
            # so we delete it solely on an explicit rejection, never on an
            # ambiguous verdict.
            rejected_by_rom.setdefault(row.rom_id, []).append(row.start_time)
            detail_suffix = f", detail={detail}" if detail else ""
            self._logger.info(
                f"play session rejected by server for rom {row.rom_id} "
                f"(start={row.start_time}, status={status}{detail_suffix}) — dropping from outbox"
            )
            return
        # ``error`` OR any unknown acknowledged status: bounded retry, then
        # quarantine once the row exhausts ``_MAX_INGEST_ATTEMPTS``. An unknown
        # verdict is not an explicit rejection, so it gets the same
        # never-drop-data-on-ambiguity hedge as a possibly-transient error — still
        # loop-free (it converges to quarantine after the threshold).
        self._bump_ingest_attempt(
            row,
            status=status,
            failed_by_rom=failed_by_rom,
            quarantine_by_rom=quarantine_by_rom,
            undrained=undrained,
        )

    def _handle_batch_rejection(
        self,
        device_id: str,
        group: list[PendingSessionRow],
        exc: RommUnprocessableEntityError,
        *,
        sent_by_rom: dict[int, list[str]],
        failed_by_rom: dict[int, list[str]],
        quarantine_by_rom: dict[int, list[str]],
        rejected_by_rom: dict[int, list[str]],
        undrained: list[PendingSessionRow],
    ) -> None:
        """Heal a whole-request 422 by dropping the flagged entries and resubmitting the rest.

        RomM validates the ``sessions`` array atomically: one invalid entry (e.g.
        a sub-second window) 422s the ENTIRE POST, so a single poison row would
        block every valid session queued behind it (#1312). The 422 body names
        the failing positions in ``detail[].loc[2]``; those rows drop as terminal
        (the byte-identical entry draws the same verdict forever, exactly like a
        ``skipped`` per-row verdict) and the survivors resubmit so they still
        ingest.

        When the body names NO usable index (a proxy/Cloudflare-mangled 422 body
        is a real risk — RomM sits behind a Cloudflare Tunnel here) a multi-row
        batch must NOT quarantine the whole group: a session recorded locally but
        not yet on the server exists nowhere else, so losing a valid sibling to a
        poison one would violate "never delete data that exists nowhere else".
        Instead each session is re-submitted on its OWN (:meth:`_flush_single_session`)
        so the server's per-session verdict isolates the genuine poison from its
        valid siblings. A lone row (``len == 1``) has no sibling to isolate, so it
        skips the re-POST and bumps its own attempt counter directly.

        Recursion is bounded: the indexed path drops at least the flagged rows, so
        the survivor group strictly shrinks; the no-index path fans out into
        single-session POSTs that never re-enter this method.
        """
        indices = set(rejected_session_indices(exc.detail, len(group)))
        if not indices:
            if len(group) == 1:
                # No sibling to isolate — bump this lone row toward its own quarantine.
                self._log_debug(
                    f"Play-session single-session 422 with no usable detail (device {device_id}) — bumping attempts"
                )
                self._bump_ingest_attempt(
                    group[0],
                    status="422",
                    failed_by_rom=failed_by_rom,
                    quarantine_by_rom=quarantine_by_rom,
                    undrained=undrained,
                )
                return
            self._log_debug(
                f"Play-session batch 422 with no usable detail indices "
                f"(device {device_id}, {len(group)} session(s)) — falling back to per-session ingest"
            )
            for row in group:
                self._flush_single_session(
                    device_id,
                    row,
                    sent_by_rom=sent_by_rom,
                    failed_by_rom=failed_by_rom,
                    quarantine_by_rom=quarantine_by_rom,
                    rejected_by_rom=rejected_by_rom,
                    undrained=undrained,
                )
            return

        survivors: list[PendingSessionRow] = []
        for index, row in enumerate(group):
            if index in indices:
                rejected_by_rom.setdefault(row.rom_id, []).append(row.start_time)
                self._logger.info(
                    f"play session rejected by server for rom {row.rom_id} "
                    f"(start={row.start_time}, batch index {index}, HTTP 422) — dropping from outbox"
                )
            else:
                survivors.append(row)

        if survivors:
            self._flush_device_group(
                device_id,
                survivors,
                sent_by_rom=sent_by_rom,
                failed_by_rom=failed_by_rom,
                quarantine_by_rom=quarantine_by_rom,
                rejected_by_rom=rejected_by_rom,
                undrained=undrained,
            )

    def _flush_single_session(
        self,
        device_id: str,
        row: PendingSessionRow,
        *,
        sent_by_rom: dict[int, list[str]],
        failed_by_rom: dict[int, list[str]],
        quarantine_by_rom: dict[int, list[str]],
        rejected_by_rom: dict[int, list[str]],
        undrained: list[PendingSessionRow],
    ) -> None:
        """Re-POST one outbox row on its own after an unindexed whole-batch 422.

        The single-session verdict isolates the genuine poison from its valid
        siblings, so a proxy-mangled batch 422 (no usable ``detail``) can never
        quarantine a valid session for a sibling's fault (#1312 L2). Verdicts:
        a 201 (``created`` / ``duplicate``) dequeues; a lone 422 that now DOES name
        the entry (index 0) drops it terminally (the genuine poison); a lone 422
        that STILL names no index bumps only THIS row's attempt counter;
        an endpoint 404, like a transport error, retains only this row.
        Deliberately does NOT re-enter
        :meth:`_handle_batch_rejection`, so the fan-out is one POST per session and
        cannot loop.
        """
        try:
            response = self._romm_api.ingest_play_sessions(device_id, [self._entry_for_row(row)])
        except RommUnprocessableEntityError as exc:
            if rejected_session_indices(exc.detail, 1):
                # The server named this lone session as the poison — drop terminally.
                rejected_by_rom.setdefault(row.rom_id, []).append(row.start_time)
                self._logger.info(
                    f"play session rejected by server for rom {row.rom_id} "
                    f"(start={row.start_time}, HTTP 422 single-session) — dropping from outbox"
                )
            else:
                # Still no usable detail even for one session — bump only this row.
                self._bump_ingest_attempt(
                    row,
                    status="422",
                    failed_by_rom=failed_by_rom,
                    quarantine_by_rom=quarantine_by_rom,
                    undrained=undrained,
                )
            return
        except RommNotFoundError as e:
            # Same route-level reading as the batch path, scoped to this row.
            self._log_debug(
                f"Play-session ingest endpoint answered 404 for device {device_id} — "
                f"retaining session {row.start_time} untouched: {e}"
            )
            undrained.append(row)
            return
        except Exception as e:
            self._log_debug(f"Play-session ingest failed (non-fatal): {e}")
            undrained.append(row)
            return

        result = next((r for r in response.get("results", []) if r.get("index", -1) == 0), None)
        status = result.get("status") if result is not None else None
        detail = result.get("detail") if result is not None else None
        self._route_2xx_verdict(
            row,
            status,
            detail,
            sent_by_rom=sent_by_rom,
            failed_by_rom=failed_by_rom,
            quarantine_by_rom=quarantine_by_rom,
            rejected_by_rom=rejected_by_rom,
            undrained=undrained,
        )

    def _bump_ingest_attempt(
        self,
        row: PendingSessionRow,
        *,
        status: str,
        failed_by_rom: dict[int, list[str]],
        quarantine_by_rom: dict[int, list[str]],
        undrained: list[PendingSessionRow],
    ) -> None:
        """Bounded-retry one outbox row that drew a non-terminal ingest failure.

        The row stays queued (``undrained``) and its attempt counter advances;
        once it would reach ``_MAX_INGEST_ATTEMPTS`` it is quarantined (dropped —
        only playtime is lost) so a persistently-failing row cannot wedge the
        outbox. Shared by an ``error`` / unknown per-row verdict and the
        no-usable-index whole-request-422 fallback.
        """
        undrained.append(row)
        if row.attempts + 1 >= _MAX_INGEST_ATTEMPTS:
            quarantine_by_rom.setdefault(row.rom_id, []).append(row.start_time)
            self._logger.warning(
                f"Dropping play session for rom {row.rom_id} (start={row.start_time}, status={status}) "
                f"after {row.attempts + 1} failed ingest attempts — unrecoverable, only playtime is lost"
            )
        else:
            failed_by_rom.setdefault(row.rom_id, []).append(row.start_time)

    def _apply_flush_outcome(
        self,
        sent_by_rom: dict[int, list[str]],
        failed_by_rom: dict[int, list[str]],
        quarantine_by_rom: dict[int, list[str]],
        rejected_by_rom: dict[int, list[str]],
    ) -> None:
        """Persist the flush verdicts in one short write UoW.

        For each affected ROM: dequeue accepted sessions, drop quarantined and
        server-rejected ones, and bump the attempt counter on the rest. Each
        start_time falls into exactly one bucket, so the four mutations never
        contend for a row.
        """
        rom_ids = set(sent_by_rom) | set(failed_by_rom) | set(quarantine_by_rom) | set(rejected_by_rom)
        with self._uow_factory() as uow:
            for rid in rom_ids:
                pt = uow.playtime.get(rid)
                if pt is None:
                    continue
                if rid in sent_by_rom:
                    pt.mark_sessions_sent(sent_by_rom[rid])
                if rid in quarantine_by_rom:
                    pt.quarantine_sessions(quarantine_by_rom[rid])
                if rid in rejected_by_rom:
                    pt.drop_rejected_sessions(rejected_by_rom[rid])
                if rid in failed_by_rom:
                    pt.record_ingest_failure(failed_by_rom[rid])
                uow.playtime.save(rid, pt)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_all_playtime(self) -> dict[str, Any]:
        """Return all local playtime entries keyed by rom_id string.

        Wire shape the frontend types and reads:
        ``{playtime: {rom_id_str: {total_seconds, session_count, last_played}}}``.
        ``last_played`` is the ISO end time of the newest recorded/reconciled
        session (``None`` until one exists). Callable-only, so its own short read
        UoW is safe (no in-transaction caller).
        """
        with self._uow_factory() as uow:
            return {
                "playtime": {
                    str(rom_id): {
                        "total_seconds": pt.total_seconds,
                        "session_count": pt.session_count,
                        "last_played": pt.last_played,
                    }
                    for rom_id, pt in uow.playtime.iter_all()
                }
            }

    def get_scope_notice(self) -> dict[str, bool]:
        """Report whether the playtime read-scope re-sign-in notice is pending.

        Reads the durable ``kv_config`` flag set when a reconcile GET 403s (the
        token predates the ``roms.user.read`` scope). Non-consuming — the flag
        survives a reload and is cleared only by a later successful GET or a fresh
        sign-in — so the QAM banner stays up until the user re-authenticates.
        """
        with self._uow_factory() as uow:
            pending = uow.kv_config.get(_SCOPE_NOTICE_KEY) is not None
        return {"pending": pending}

    def clear_scope_notice(self) -> None:
        """Clear the durable playtime read-scope re-sign-in notice (idempotent).

        Public seam so ConnectionService can drop the flag on a fresh sign-in
        (the new token carries ``roms.user.read``); reconcile also calls it after
        a successful GET. Reads first and only deletes when set, so a clean state
        adds no needless write.
        """
        with self._uow_factory() as uow:
            if uow.kv_config.get(_SCOPE_NOTICE_KEY) is not None:
                uow.kv_config.delete(_SCOPE_NOTICE_KEY)

    def _set_scope_notice(self) -> None:
        """Raise the durable playtime read-scope re-sign-in notice."""
        with self._uow_factory() as uow:
            uow.kv_config.set(_SCOPE_NOTICE_KEY, "1")

    async def reconcile_playtime(self, rom_id: int) -> dict[str, Any]:
        """Pull the cross-device server history into the local row on detail-page view.

        Flushes the outbox first, then reads the ROM's native play-session
        history and folds three values derived from it into the ``Playtime``
        aggregate — the summed total (``reconcile_total``), the row count
        (``reconcile_session_count``) and the newest end time
        (``reconcile_last_played``). Each is a monotonic clamp that never
        regresses local play, so a fresh device restores ``session_count`` and
        ``last_played`` alongside ``total_seconds`` (#903). Read-only against the
        aggregate's server view; it never ingests here. The work runs in an
        executor (the SQLite connection has thread affinity).
        """
        return await self._loop.run_in_executor(None, self._reconcile_playtime_io, int(rom_id))

    def _reconcile_playtime_io(self, rom_id: int) -> dict[str, Any]:
        """Synchronous twin of :meth:`reconcile_playtime` (runs in the executor).

        Flushes the outbox (so freshly-recorded local sessions are in the server
        union), fetches the ROM's play-session history outside any transaction,
        derives the summed seconds, the session count, and the newest end time,
        then folds all three into the aggregate in a short write UoW. Returns the
        partial-success shape ``{total_seconds, session_count, last_played,
        server_query_failed}``: the first three come from the resulting (or
        existing) local row, and ``server_query_failed`` flags an unreachable
        server. Never raises out of the callable — a fetch failure, a
        not-yet-scoped token (403 → durable re-sign-in notice, local-only
        degrade), or an orphan ``rom_id`` (no ``roms`` row) reports the local
        row's values.
        """
        # Drain the outbox so a session recorded moments ago is already part of
        # the server union we are about to read back. The flush never raises.
        self._flush_pending_sessions_io()

        try:
            sessions = self._retry.with_retry(self._romm_api.list_play_sessions, rom_id)
        except RommForbiddenError:
            # The token predates the roms.user.read scope (#1280): the GET is
            # forbidden, not merely unreachable. Raise the durable re-sign-in
            # notice and degrade to local-only.
            self._set_scope_notice()
            self._log_debug(
                f"Reconcile for rom {rom_id}: token lacks roms.user.read — re-sign-in needed for cross-device playtime"
            )
            return self._local_playtime_result(rom_id, server_query_failed=True)
        except Exception as e:
            self._log_debug(f"Failed to reconcile playtime for rom {rom_id}: {e}")
            return self._local_playtime_result(rom_id, server_query_failed=True)

        # A successful GET means the token now carries the read scope — clear any
        # stale re-sign-in notice a prior 403 left behind.
        self.clear_scope_notice()

        # Three values derived from the SAME session list: the summed cross-device
        # duration, the row count, and the newest session end. All three fold into
        # the aggregate via monotonic reconcile verbs so a fresh device restores
        # total_seconds AND session_count AND last_played, not the total alone (#903).
        server_total_seconds = sum(coerce_duration_ms(s) for s in sessions) // 1000
        server_session_count = len(sessions)
        server_last_played = latest_end_time(sessions)

        try:
            with self._uow_factory() as uow:
                entry = uow.playtime.get(rom_id)
                if server_session_count == 0 and entry is None:
                    # No server data and no local row — report zero, do not seed
                    # an empty ``rom_playtime`` row.
                    self._log_debug(f"Reconciled playtime for rom {rom_id}: no server sessions, no local row")
                    return _empty_reconcile_result(server_query_failed=False)
                pt = entry or Playtime()
                pt.reconcile_total(server_total_seconds)
                pt.reconcile_session_count(server_session_count)
                pt.reconcile_last_played(server_last_played)
                uow.playtime.save(rom_id, pt)
                total_seconds = pt.total_seconds
                session_count = pt.session_count
                last_played = pt.last_played
        except sqlite3.IntegrityError as e:
            # Orphan FK (rom_id absent from roms): the commit rolls back, so no
            # row exists to report — a graceful 0/0 no-op.
            self._log_debug(f"Failed to reconcile playtime for rom {rom_id}: {e}")
            return _empty_reconcile_result(server_query_failed=False)

        self._log_debug(
            f"Reconciled playtime for rom {rom_id}: server={server_total_seconds}s -> total={total_seconds}s"
        )
        return {
            "total_seconds": total_seconds,
            "session_count": session_count,
            "last_played": last_played,
            "server_query_failed": False,
        }

    def _local_playtime_result(self, rom_id: int, *, server_query_failed: bool) -> dict[str, Any]:
        """Build the reconcile result from the existing local row (zeros/None when absent)."""
        with self._uow_factory() as uow:
            entry = uow.playtime.get(rom_id)
        if entry is None:
            return _empty_reconcile_result(server_query_failed=server_query_failed)
        return {
            "total_seconds": entry.total_seconds,
            "session_count": entry.session_count,
            "last_played": entry.last_played,
            "server_query_failed": server_query_failed,
        }
