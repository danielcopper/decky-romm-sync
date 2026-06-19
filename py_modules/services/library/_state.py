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
    # back to None between units. On a heartbeat **timeout** (not a user
    # cancel) the orchestrator does NOT clear ``pending_sync`` or null
    # ``unit_complete_event`` — it flags ``unit_abandoned`` instead, so a
    # late ``report_unit_results`` can still commit the delivered bindings
    # that the frontend already created Steam shortcuts for (#1052).
    unit_complete_event: asyncio.Event | None = None
    # Holds the frontend-supplied ``rom_id_to_app_id`` mapping reported
    # for the active unit. Surfaces the result so the orchestrator can
    # accumulate the per-unit registry into the cross-run accumulators.
    last_unit_results: dict[str, int] | None = None
    # Set True when a per-unit wait times out on a stale heartbeat (not a
    # user cancel): the orchestrator abandoned the unit but the frontend
    # may still ack it. A late :meth:`SyncReporter.report_unit_results`
    # observes this flag and drives the per-unit commit itself so the
    # delivered bindings are persisted rather than discarded (#1052).
    unit_abandoned: bool = False
    # The abandoned unit's live RomM fetch (the source of each ROM's
    # ``metadatum``), stashed so a late ack can rebuild ``acked_roms`` for
    # the commit it drives. Reset between units alongside ``last_unit_results``.
    pending_unit_roms: list[dict[str, Any]] = field(default_factory=list)
    # Every Steam appId bound by a ``commit_unit_results`` this run, across
    # BOTH the happy path and the heartbeat-timeout late-ack path (#1052).
    # The stale-removal scan excludes these so a new server-issued rom_id that
    # reuses an old appId (CRC32 of unchanged exe+name) can't wipe the shortcut
    # the run just bound (#1036). Reset at the start of each run.
    committed_app_ids: set[int] = field(default_factory=set)

    def is_cancelling(self) -> bool:
        """True while a cancel has been requested for the in-flight run."""
        return self.sync_state is SyncState.CANCELLING
