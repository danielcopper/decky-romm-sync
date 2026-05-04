"""ISO-8601 timestamp parsing helpers.

Layer-agnostic utilities — domain and services may both import from here.
"""

from __future__ import annotations

from datetime import datetime


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware datetime, or None on failure.

    Handles a trailing "Z" defensively (older datetime.fromisoformat versions
    reject it). Returns None for empty/None input or any parse failure — the
    caller decides how to interpret that.
    """
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None


def parse_iso_to_epoch(value: str | None) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds (UTC), or None on failure."""
    dt = parse_iso(value)
    return dt.timestamp() if dt is not None else None
