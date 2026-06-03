"""Shared mutable state for the library sync pipeline.

Owned by :class:`LibraryService`; each sub-service receives a reference
so they can coordinate without back-refs to the façade. The contract:
sub-services mutate the box's fields directly (it is the single source
of truth for in-flight sync run state); the façade exposes property
accessors over the box so external callers see a flat shape rather
than reaching through ``service._state.x``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from domain.sync_state import SyncState

if TYPE_CHECKING:
    import asyncio

    from domain.preview_delta import PreviewDelta


def _default_progress() -> dict[str, Any]:
    return {
        "running": False,
        "stage": "",
        "current": 0,
        "total": 0,
        "message": "",
        "step": 0,
        "totalSteps": 0,
    }


@dataclass
class LibrarySyncStateBox:
    """In-memory state for one library sync run, plus held preview data.

    Holds the current ``SyncState`` (idle/running/cancelling), the
    generation id used to invalidate stale background work after the
    run ends, the heartbeat timestamp, the live progress dict emitted
    to the frontend, and the apply-staging dicts populated during
    ``sync_preview`` / ``sync_apply_delta`` and consumed by the
    per-unit pipeline.
    """

    sync_state: SyncState = SyncState.IDLE
    current_sync_id: str | None = None
    sync_last_heartbeat: float = 0.0
    sync_progress: dict[str, Any] = field(default_factory=_default_progress)
    pending_sync: dict[int, dict[str, Any]] = field(default_factory=dict)
    pending_delta: PreviewDelta | None = None
    pending_collection_memberships: dict[str, list[int]] = field(default_factory=dict)
    pending_platform_rom_ids: set[int] | None = None
    # Per-unit pipeline coordination. ``unit_complete_event`` is set by
    # :meth:`SyncReporter.report_unit_results` when the frontend reports
    # back for the active unit; the orchestrator awaits it (with a
    # heartbeat-based timeout) before dispatching the next unit. Cleared
    # back to None between units.
    unit_complete_event: asyncio.Event | None = None
    # Holds the frontend-supplied ``rom_id_to_app_id`` mapping reported
    # for the active unit. Surfaces the result so the orchestrator can
    # accumulate the per-unit registry into the cross-run accumulators.
    last_unit_results: dict[str, int] | None = None

    def is_cancelling(self) -> bool:
        """True while a cancel has been requested for the in-flight run."""
        return self.sync_state is SyncState.CANCELLING
