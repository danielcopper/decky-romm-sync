"""Pure save-sync display computation.

No I/O, no service/adapter imports. Stateless functions only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SaveSyncStatus = Literal["synced", "conflict", "none"]


@dataclass(frozen=True)
class SaveSyncDisplay:
    """Backend-computed save-sync display fields shipped to the frontend.

    Time-relative formatting of ``last_sync_check_at`` is intentionally a
    frontend concern: the backend cannot keep an "Xm ago" label fresh
    between fetches. For the only time-relative case (``synced`` with a
    recorded sync check), ``label`` is ``None`` and the frontend renders
    a time-ago label from ``last_sync_check_at``. For every other case
    ``label`` is a fully-formed static string and ``last_sync_check_at``
    is ``None``.
    """

    status: SaveSyncStatus
    label: str | None
    last_sync_check_at: str | None


def compute_save_sync_display(
    files: list[dict[str, Any]] | None,
    last_sync_check_at: str | None,
    *,
    server_query_failed: bool = False,
) -> SaveSyncDisplay:
    """Compute save sync display status and label.

    Returns ``SaveSyncDisplay`` with ``status`` ('synced' | 'conflict' |
    'none'), ``label`` (static text or ``None`` when the frontend formats
    a time-ago label), and ``last_sync_check_at`` (passthrough for the
    time-ago case).

    When *server_query_failed* is True the server's save list could not
    be fetched, so the matrix verdict on each file is unreliable. The
    display collapses to ``status="none"`` with a "Server unreachable"
    label rather than reporting the false "synced" / "ready to upload"
    state a stale-but-empty file list would produce.
    """
    if server_query_failed:
        return SaveSyncDisplay(status="none", label="Server unreachable", last_sync_check_at=None)

    if not files:
        return SaveSyncDisplay(status="none", label="No saves", last_sync_check_at=None)

    if any(f.get("status") == "conflict" for f in files):
        return SaveSyncDisplay(status="conflict", label="Conflict", last_sync_check_at=None)

    has_local = any(f.get("local_path") or f.get("status") in ("synced", "upload") for f in files)
    if has_local:
        if last_sync_check_at:
            return SaveSyncDisplay(status="synced", label=None, last_sync_check_at=last_sync_check_at)
        return SaveSyncDisplay(status="synced", label="Not synced", last_sync_check_at=None)

    return SaveSyncDisplay(status="none", label="No local saves", last_sync_check_at=None)
