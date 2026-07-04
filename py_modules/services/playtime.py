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
from domain.playtime import Playtime
from lib.errors import RommForbiddenError
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


def _coerce_duration_ms(row: object) -> int:
    """Return a server play-session row's ``duration_ms`` as an int, else ``0``.

    The cross-device union spans every Device-Sync client, so a stored row may
    carry ``duration_ms: null`` or a non-numeric value (or not be a dict at all).
    Coercing defensively keeps one malformed row from crashing the whole reconcile
    sum — this never raises. Booleans are treated as non-numeric so ``True`` does
    not silently count as 1ms.
    """
    if not isinstance(row, dict):
        return 0
    value = row.get("duration_ms")
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _latest_end_time(sessions: list[Any]) -> str | None:
    """Return the newest parseable ``end_time`` across server play-session rows, else ``None``.

    Mirrors ``_coerce_duration_ms``'s defensive coercion: a row that is not a
    dict, lacks a string ``end_time``, or carries an unparseable timestamp is
    skipped, so one malformed row never crashes the reconcile. Compared by
    parsed datetime (never lexically), and a naive/aware mismatch between two
    server rows is skipped rather than raised. The original string of the newest
    row is returned so the stored ``last_played`` keeps the server's format.
    """
    latest_raw: str | None = None
    latest_dt = None
    for row in sessions:
        if not isinstance(row, dict):
            continue
        raw = row.get("end_time")
        if not isinstance(raw, str):
            continue
        parsed = parse_iso(raw)
        if parsed is None:
            continue
        if latest_dt is None:
            latest_raw, latest_dt = raw, parsed
            continue
        try:
            if parsed > latest_dt:
                latest_raw, latest_dt = raw, parsed
        except TypeError:  # naive/aware mismatch between rows — skip the outlier
            continue
    return latest_raw


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
                pt.begin_session(self._clock.now().isoformat())
                uow.playtime.save(rid, pt)
        except sqlite3.IntegrityError as e:
            self._log_debug(f"Failed to record session start for rom {rid}: {e}")
            return {"success": False, "reason": "unknown_rom", "message": "Unknown ROM"}
        return {"success": True}

    async def record_session_end(self, rom_id: int, suspended_seconds: int = 0) -> dict[str, Any]:
        """Record end of play session, accumulate playtime delta.

        Only handles playtime — save sync is handled separately. ``suspended_seconds``
        is the wall-clock time the device spent suspended during the session; it
        is subtracted from the raw elapsed span so suspend time is not counted as
        play. The work runs in an executor: the durable fold + outbox enqueue
        happen in a short write UoW (the SQLite connection has thread affinity),
        then the native ingest flush runs best-effort outside any transaction.
        """
        return await self._loop.run_in_executor(None, self._record_session_end_io, int(rom_id), suspended_seconds)

    def _record_session_end_io(self, rom_id: int, suspended_seconds: int = 0) -> dict[str, Any]:
        """Synchronous twin of :meth:`record_session_end` (runs in the executor).

        Phase A — fold the closed session (minus ``suspended_seconds``) into the
        aggregate and, when this device is registered, enqueue the session into
        the outbox, both in one short write UoW. Phase B — flush the outbox to
        RomM's native ingest outside the transaction (best-effort). Returns the
        same dict shape the frontend consumes: ``success`` plus ``duration_sec``
        / ``total_seconds`` / ``session_count`` on the happy path, or
        ``success: False`` with a ``message`` otherwise.
        """
        ended_at = self._clock.now().isoformat()
        # Read the device id BEFORE opening the write UoW: get_device_id opens its
        # own DeviceRegistry UoW, and a nested BEGIN IMMEDIATE inside our open
        # write transaction would self-deadlock on the write lock.
        device_id = self._device_id_provider.get_device_id()
        try:
            with self._uow_factory() as uow:
                entry = uow.playtime.get(rom_id)
                if entry is None or not entry.last_session_start:
                    return {"success": False, "reason": "no_active_session", "message": "No active session"}
                # Capture the start BEFORE record_session clears it — it keys the
                # outbox row and RomM's dedup.
                started_at = entry.last_session_start
                try:
                    entry.record_session(ended_at, suspended_seconds=suspended_seconds)
                except ValueError:
                    return {
                        "success": False,
                        "reason": ErrorCode.UNKNOWN.value,
                        "message": "Failed to calculate session duration",
                    }
                duration = entry.last_session_duration_sec or 0
                if device_id:
                    entry.enqueue_session(
                        device_id=device_id,
                        start_time=started_at,
                        end_time=ended_at,
                        duration_ms=duration * 1000,
                    )
                else:
                    # Unregistered device: fold locally, never enqueue (an empty
                    # device id must never reach the wire, ADR-0018 decision #8).
                    self._log_debug(f"record_session_end: rom {rom_id} not enqueued — device unregistered")
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
        the next flush. Per-row verdicts are correlated back by the response
        ``index`` (position in this group's submitted batch).
        """
        batch: list[PlaySessionIngestEntry] = [
            {
                "rom_id": row.rom_id,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "duration_ms": row.duration_ms,
            }
            for row in group
        ]
        try:
            response = self._romm_api.ingest_play_sessions(device_id, batch)
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
            if status in ("created", "duplicate"):
                # Both are successful ingests — dequeue.
                sent_by_rom.setdefault(row.rom_id, []).append(row.start_time)
                continue
            if status is None:
                # Absent from a 2xx response (or a malformed row missing
                # ``status``): a transient omission, not a verdict. Stay queued
                # untouched — no attempt bump — and retry on the next flush.
                undrained.append(row)
                continue
            if status == "skipped":
                # An EXPLICIT server rejection for this exact (device, rom,
                # start_time) window (e.g. a sub-second launch-death rejected on
                # validation). Re-POSTing the byte-identical row draws the same
                # verdict forever, so draining it is the honest terminal action
                # (only playtime is lost). Log once at info — the single line is
                # the record. Only ``skipped`` hard-drops: an outbox session
                # exists nowhere else, so we delete it solely on an explicit
                # rejection, never on an ambiguous verdict.
                rejected_by_rom.setdefault(row.rom_id, []).append(row.start_time)
                detail_suffix = f", detail={detail}" if detail else ""
                self._logger.info(
                    f"play session rejected by server for rom {row.rom_id} "
                    f"(start={row.start_time}, status={status}{detail_suffix}) — dropping from outbox"
                )
                continue
            # ``error`` OR any unknown acknowledged status: bounded retry, then
            # quarantine once the row exhausts ``_MAX_INGEST_ATTEMPTS``. An
            # unknown verdict is not an explicit rejection, so it gets the same
            # never-drop-data-on-ambiguity hedge as a possibly-transient error —
            # still loop-free (it converges to quarantine after the threshold).
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
        server_total_seconds = sum(_coerce_duration_ms(s) for s in sessions) // 1000
        server_session_count = len(sessions)
        server_last_played = _latest_end_time(sessions)

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
