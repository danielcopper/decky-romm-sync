"""Shared mutable state for the library sync pipeline.

Owned by :class:`LibraryService`; each sub-service receives a reference
so they can coordinate without back-refs to the façade. The contract:
sub-services mutate the box's fields directly (it is the single source
of truth for in-flight sync run state); the façade exposes property
accessors over the box so external callers see the same shape that
preceded the decomposition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from domain.sync_state import SyncState

if TYPE_CHECKING:
    from domain.preview_delta import PreviewDelta


def _default_progress() -> dict:
    return {
        "running": False,
        "phase": "",
        "current": 0,
        "total": 0,
        "message": "",
    }


@dataclass
class LibrarySyncStateBox:
    """In-memory state for one library sync run, plus held preview/apply data.

    Holds the current ``SyncState`` (idle/running/cancelling), the
    generation id used by the safety-timeout guard, the heartbeat
    timestamp, the live progress dict emitted to the frontend, and the
    apply-staging dicts populated during ``sync_preview`` /
    ``sync_apply_delta`` / ``_do_sync`` and consumed by
    ``report_sync_results``.
    """

    sync_state: SyncState = SyncState.IDLE
    current_sync_id: str | None = None
    sync_last_heartbeat: float = 0.0
    sync_progress: dict = field(default_factory=_default_progress)
    pending_sync: dict = field(default_factory=dict)
    pending_delta: PreviewDelta | None = None
    pending_collection_memberships: dict = field(default_factory=dict)
    pending_platform_rom_ids: set[int] | None = None
