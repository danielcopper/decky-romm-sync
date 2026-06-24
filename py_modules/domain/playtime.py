"""Per-ROM playtime — running totals plus the in-flight session marker.

One Playtime per Rom (referenced by id). Tracks cumulative play seconds and
session count, the open session's start timestamp (durable so a session
survives a plugin reload mid-game), and the most recent session's duration.
Individual sessions are not entities — only their start (while open) and their
folded-in result persist. RomM is the shared server record; this aggregate is
the local durable + read model that reconciles with it.
"""

from __future__ import annotations

import json
from typing import Any

from domain._aggregate import cosmic_aggregate
from domain.iso_time import parse_iso

_MAX_SESSION_SECONDS = 86_400  # a single session contributes at most 24h


def parse_playtime_note_content(content: str) -> dict[str, Any] | None:
    """Parse the JSON body of a RomM playtime note into a dict.

    Returns the decoded object when ``content`` holds a JSON dict, or ``None``
    for empty content, malformed JSON, or a non-dict top-level value.
    """
    if not content:
        return None
    try:
        data = json.loads(content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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

    def link_note(self, note_id: int) -> None:
        """Associate the RomM playtime note id used for server sync."""
        self.note_id = note_id

    def reconcile_total(self, seconds: int) -> None:
        """Raise the cumulative total to ``seconds`` if it is higher.

        The merged total from a RomM round-trip (server baseline plus the
        local session) is reconciled into the aggregate here. The clamp
        never regresses the local total — a smaller ``seconds`` (a server
        record that lags behind local play) is ignored.
        """
        self.total_seconds = max(self.total_seconds, seconds)
