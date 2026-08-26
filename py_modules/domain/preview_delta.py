"""Pending sync-preview snapshot held between ``sync_preview`` and ``sync_apply_delta``.

Owns the typed shape of the data ``LibraryService`` stashes after a preview
run so the subsequent apply call can act on the exact snapshot the user saw,
and so a panel that lost its card can ask for it back. Pure data —
construction, attribute reads, the TTL predicate; no I/O, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def preview_expires_at(created_at: float, max_age_seconds: float) -> float:
    """The absolute wall-clock epoch a snapshot taken at *created_at* stops being appliable."""
    return created_at + max_age_seconds


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

    ``answer`` is the exact dict ``sync_preview`` returned to the frontend, so
    a panel that was navigated away from and remounted can be handed back what
    the user was shown rather than a second assembly of it that can drift from
    the first.
    """

    preview_id: str
    created_at: float
    platforms_count: int
    total_roms: int
    answer: Mapping[str, Any]

    def is_expired(self, now: float, max_age_seconds: float) -> bool:
        """Whether the snapshot is past its TTL at wall-clock *now*.

        The single expression of "this delta is too old" — the apply path
        refuses an expired snapshot, and the pending-preview read drops one, on
        this one predicate.
        """
        return now > preview_expires_at(self.created_at, max_age_seconds)
