"""Shared mutable state for the library sync pipeline.

Owned by :class:`LibraryService`; each sub-service receives a reference
so they can coordinate without back-refs to the façade. The contract:
sub-services mutate the box's fields directly (it is the single source
of truth for in-flight sync run state); the façade exposes property
accessors over the box so external callers see the same shape that
preceded the decomposition.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from domain.sync_state import SyncState

if TYPE_CHECKING:
    from domain.preview_delta import PreviewDelta
    from domain.work_unit import WorkUnit


def _default_progress() -> dict:
    return {
        "running": False,
        "phase": "",
        "current": 0,
        "total": 0,
        "message": "",
    }


@dataclass(frozen=True)
class PrefetchedUnit:
    """One work unit with its ROMs already fetched.

    Cached on :class:`LibrarySyncStateBox` between ``sync_preview`` and
    ``sync_apply_delta`` so the apply phase can dispatch the per-unit
    pipeline without re-fetching ROMs from RomM. ``skipped`` mirrors the
    second return of :meth:`LibraryFetcher.fetch_platform_unit` — when
    True, the artwork-download step is bypassed because cover paths are
    already populated from the registry. ``all_collection_rom_ids`` is
    only set for collection units; platform units leave it ``None``.
    """

    unit: WorkUnit
    roms: list[dict]
    skipped: bool
    all_collection_rom_ids: list[int] | None = None


@dataclass
class LibrarySyncStateBox:
    """In-memory state for one library sync run, plus held preview/apply data.

    Holds the current ``SyncState`` (idle/running/cancelling), the
    generation id used to invalidate stale background work after the
    run ends, the heartbeat timestamp, the live progress dict emitted
    to the frontend, and the apply-staging dicts populated during
    ``sync_preview`` / ``sync_apply_delta`` and consumed by
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
    # Skip Preview OFF apply cache: ``sync_preview`` builds the work
    # queue + prefetches each unit's ROMs upfront so the user-confirmed
    # ``sync_apply_delta`` dispatches the per-unit pipeline without
    # re-fetching from RomM. Cleared on apply success, apply error,
    # cancel, and on every new ``sync_preview`` call.
    pending_prefetched_units: list[PrefetchedUnit] | None = None
