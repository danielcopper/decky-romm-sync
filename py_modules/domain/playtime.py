"""Per-ROM playtime — running totals plus the in-flight session marker.

One Playtime per Rom (referenced by id). Tracks cumulative play seconds and
session count, the open session's start timestamp (durable so a session
survives a plugin reload mid-game), and the most recent session's duration.
Individual sessions are not entities — only their start (while open) and their
folded-in result persist. RomM is the shared server record; this aggregate is
the local durable + read model that reconciles with it.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate
from domain.iso_time import parse_iso

_MAX_SESSION_SECONDS = 86_400  # a single session contributes at most 24h


@cosmic_aggregate
class Playtime:
    """Cumulative playtime and the open-session marker for one ROM."""

    total_seconds: int = 0
    session_count: int = 0
    last_session_start: str | None = None
    last_session_duration_sec: int | None = None
    note_id: int | None = None

    def begin_session(self, at: str) -> None:
        """Open a play session that started at ISO timestamp ``at``."""
        self.last_session_start = at

    def record_session(self, ended_at: str) -> None:
        """Close the open session at ``ended_at`` and fold its duration into the totals.

        The duration is the span from the stored ``last_session_start`` to
        ``ended_at``, clamped to ``[0, 24h]``. Raises ``ValueError`` if no
        session is open or either timestamp is unusable.
        """
        if self.last_session_start is None:
            raise ValueError("no open session to record")
        start = parse_iso(self.last_session_start)
        end = parse_iso(ended_at)
        if start is None or end is None:
            raise ValueError("unparseable session timestamps")
        try:
            elapsed = (end - start).total_seconds()
        except TypeError as exc:  # naive/aware datetime mismatch
            raise ValueError("inconsistent session timestamps") from exc
        seconds = int(max(0, min(elapsed, _MAX_SESSION_SECONDS)))
        self.total_seconds += seconds
        self.session_count += 1
        self.last_session_duration_sec = seconds
        self.last_session_start = None

    def link_note(self, note_id: int) -> None:
        """Associate the RomM playtime note id used for server sync."""
        self.note_id = note_id
