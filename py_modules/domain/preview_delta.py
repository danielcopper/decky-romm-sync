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
    All other fields are the classified-ROM buckets, the full shortcut map
    (used by downstream artwork and result reporting), the ROM payloads
    needed for artwork download, and the collection/platform context that
    feeds the result-aggregation step.
    """

    preview_id: str
    created_at: float
    new: list[dict]
    changed: list[dict]
    unchanged_ids: list[int]
    remove_rom_ids: list[int]
    all_shortcuts: dict[int, dict]
    delta_roms: list[dict]
    platforms_count: int
    total_roms: int
    collection_memberships: dict[str, list[int]]
    platform_rom_ids: set[int]
