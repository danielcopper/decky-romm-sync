"""Pure save-sync display computation.

No I/O, no service/adapter imports. Stateless functions only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lib.iso_time import parse_iso


def _format_time_ago(iso_timestamp: str) -> str | None:
    """Format an ISO timestamp as a human-readable time-ago label, or None on error."""
    check_dt = parse_iso(iso_timestamp)
    if check_dt is None:
        return None
    if check_dt.tzinfo is None:
        check_dt = check_dt.replace(tzinfo=UTC)
    diff_min = int((datetime.now(UTC) - check_dt).total_seconds() // 60)
    if diff_min < 1:
        return "Just now"
    if diff_min < 60:
        return f"{diff_min}m ago"
    if diff_min < 1440:
        return f"{diff_min // 60}h ago"
    return f"{diff_min // 1440}d ago"


def compute_save_sync_display(
    files: list[dict] | None,
    last_sync_check_at: str | None,
) -> dict:
    """Compute save sync display status and label.

    Returns dict with 'status' ('synced' | 'conflict' | 'none') and 'label' (str).
    """
    if not files:
        return {"status": "none", "label": "No saves"}

    if any(f.get("status") == "conflict" for f in files):
        return {"status": "conflict", "label": "Conflict"}

    has_local = any(f.get("local_path") or f.get("status") in ("synced", "upload") for f in files)
    if has_local:
        if last_sync_check_at:
            label = _format_time_ago(last_sync_check_at)
            if label:
                return {"status": "synced", "label": label}
        return {"status": "synced", "label": "Not synced"}

    return {"status": "none", "label": "No local saves"}
