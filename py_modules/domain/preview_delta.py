"""Pending sync-preview snapshot held between ``sync_preview`` and ``sync_apply_delta``.

Owns the typed shape of the data ``LibraryService`` stashes after a preview
run so the subsequent apply call can act on the exact snapshot the user saw.
Pure data — construction, attribute reads, no I/O, no clock or randomness.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewDelta:
    """Snapshot produced by ``sync_preview`` and consumed by ``sync_apply_delta``.

    ``preview_id`` ties the snapshot to the frontend's apply call; mismatched
    ids cause the apply to be rejected as stale. ``created_at`` is the wall
    clock at preview time so apply can reject snapshots older than the TTL.
    ``platforms_count`` and ``total_roms`` are persisted into ``sync_stats``
    on apply so ``get_sync_stats`` and the stale-removal pass see the
    apply's intended counts. The apply phase fetches ROM data live per
    unit; this snapshot carries only the pre-flight counts, never ROM
    payloads.
    """

    preview_id: str
    created_at: float
    platforms_count: int
    total_roms: int
