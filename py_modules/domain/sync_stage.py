"""SyncStage enum — the on-the-wire stage label for a library sync run.

The single vocabulary the backend uses to describe where a sync run
is, shared by the live ``sync_progress`` event and the
``get_sync_status`` snapshot query. Each member's value is the exact
string the frontend's ``SyncProgress.stage`` discriminant expects, so
``str``-based members serialize straight onto the wire without a lookup
table. Anything that needs to name a phase of the sync lifecycle uses a
member here rather than a bare string literal.
"""

from enum import StrEnum


class SyncStage(StrEnum):
    DISCOVERING = "discovering"
    FETCHING = "fetching"
    APPLYING = "applying"
    FINALIZING = "finalizing"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"
