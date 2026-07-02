"""Per-ROM playtime — running totals, the in-flight session marker, and the outbox.

One Playtime per Rom (referenced by id). Tracks cumulative play seconds and
session count, the open session's start timestamp (durable so a session
survives a plugin reload mid-game), the most recent session's duration, and a
pending-session outbox (closed sessions awaiting ingest into RomM's native
``/api/play-sessions``, keyed by start timestamp). Individual completed sessions
are not entities once ingested — only their start (while open), their folded-in
result, and the still-unsent outbox rows persist. RomM's native play-session
store is the shared additive record (ADR-0018); this aggregate is the local
durable + cumulative read model that reconciles with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NamedTuple

from domain._aggregate import cosmic_aggregate
from domain.iso_time import parse_iso

if TYPE_CHECKING:
    from collections.abc import Iterable

_MAX_SESSION_SECONDS = 86_400  # a single session contributes at most 24h


@dataclass(frozen=True, slots=True)
class PendingPlaySession:
    """One closed session held in the outbox until it ingests into RomM.

    Keyed on its ``start_time`` in ``Playtime.pending_sessions``. ``device_id``
    is the server device id it was recorded on, ``duration_ms`` the
    suspend-adjusted screen-on time (counted seconds x 1000), ``attempts`` the
    running count of ingest ``error`` verdicts this row has drawn (bounded-retry
    quarantine driver).
    """

    device_id: str
    end_time: str
    duration_ms: int
    attempts: int = 0


class PendingSessionRow(NamedTuple):
    """One outbox row projected flat for the flush gather (a query result, not the aggregate).

    The flush reads these straight from ``rom_playtime_sessions`` (bypassing a
    full-library aggregate rebuild), groups them by ``device_id`` for the native
    ingest POST, and correlates the per-row verdict back by ``(rom_id, start_time)``.
    ``attempts`` is the row's accumulated ingest-failure count.
    """

    rom_id: int
    start_time: str
    device_id: str
    end_time: str
    duration_ms: int
    attempts: int


@cosmic_aggregate
class Playtime:
    """Cumulative playtime, the open-session marker, and the unsent-session outbox for one ROM."""

    total_seconds: int = 0
    session_count: int = 0
    last_session_start: str | None = None
    last_session_duration_sec: int | None = None
    pending_sessions: dict[str, PendingPlaySession] = field(default_factory=dict)

    def begin_session(self, at: str) -> None:
        """Open a play session that started at ISO timestamp ``at``."""
        self.last_session_start = at

    def record_session(self, ended_at: str, *, suspended_seconds: int = 0) -> None:
        """Close the open session at ``ended_at`` and fold its duration into the totals.

        The duration is the span from the stored ``last_session_start`` to
        ``ended_at`` minus any ``suspended_seconds`` the device spent suspended
        during the session (a negative value is treated as 0), clamped to
        ``[0, 24h]``. The suspend subtraction happens before the 24h cap, so a
        long session minus suspend still respects the cap and never goes
        negative. Raises ``ValueError`` if no session is open or either
        timestamp is unusable.
        """
        if self.last_session_start is None:
            raise ValueError("no open session to record")
        start = parse_iso(self.last_session_start)
        end = parse_iso(ended_at)
        if start is None or end is None:
            raise ValueError("unparseable session timestamps")
        try:
            raw_elapsed = (end - start).total_seconds()
        except TypeError as exc:  # naive/aware datetime mismatch
            raise ValueError("inconsistent session timestamps") from exc
        elapsed = max(0.0, raw_elapsed - max(0, suspended_seconds))
        seconds = int(min(elapsed, _MAX_SESSION_SECONDS))
        self.total_seconds += seconds
        self.session_count += 1
        self.last_session_duration_sec = seconds
        self.last_session_start = None

    def enqueue_session(self, *, device_id: str, start_time: str, end_time: str, duration_ms: int) -> None:
        """Add a closed session to the outbox, awaiting native ingest.

        Keyed by ``start_time`` (matching RomM's dedup key), so re-enqueuing the
        same session window overwrites rather than duplicates.
        """
        self.pending_sessions[start_time] = PendingPlaySession(
            device_id=device_id,
            end_time=end_time,
            duration_ms=duration_ms,
        )

    def mark_sessions_sent(self, start_times: Iterable[str]) -> None:
        """Dequeue the outbox rows whose sessions the server accepted (created or duplicate)."""
        for start_time in start_times:
            self.pending_sessions.pop(start_time, None)

    def record_ingest_failure(self, start_times: Iterable[str]) -> None:
        """Increment the ingest-failure counter on each named outbox row.

        Called when the native ingest returns an ``error`` verdict for a row
        that has not yet reached the quarantine threshold. Bumps ``attempts`` so
        a persistently-rejected session is eventually quarantined rather than
        retried forever. Unknown start times are ignored.
        """
        for start_time in start_times:
            session = self.pending_sessions.get(start_time)
            if session is not None:
                self.pending_sessions[start_time] = replace(session, attempts=session.attempts + 1)

    def quarantine_sessions(self, start_times: Iterable[str]) -> None:
        """Drop outbox rows that have exhausted their ingest retries (unrecoverable).

        The server keeps rejecting these sessions; only playtime is lost, so the
        row is discarded to unwedge the outbox. Unknown start times are ignored.
        """
        for start_time in start_times:
            self.pending_sessions.pop(start_time, None)

    def reconcile_total(self, seconds: int) -> None:
        """Raise the cumulative total to ``seconds`` if it is higher.

        The cross-device union total from a native play-session GET is
        reconciled into the aggregate here. Playtime is monotonic, so the clamp
        never regresses the local total — a smaller ``seconds`` (a server view
        that lags behind local play, e.g. an unflushed outbox) is ignored.
        """
        self.total_seconds = max(self.total_seconds, seconds)
