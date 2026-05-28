"""SyncRun — one sync operation modelled as a single-shot state machine.

A run starts ``running`` and transitions exactly once into a terminal state
(``completed`` / ``cancelled`` / ``errored``); the terminal transition is
irreversible. Replaces the scattered last-sync scalars (last_sync timestamp,
sync_stats, last_synced_platforms, last_synced_collections) with one record
that carries the plan, the outcome, and the timestamps of a sync. The id and
all timestamps are injected by the caller — domain owns no clock or id source.
"""

from __future__ import annotations

from typing import Literal

from domain._aggregate import cosmic_aggregate

SyncRunStatus = Literal["running", "completed", "cancelled", "errored"]


@cosmic_aggregate
class SyncRun:
    """One sync operation: its plan, its lifecycle status, and its outcome."""

    id: str
    started_at: str
    status: SyncRunStatus
    platforms_planned: int
    roms_planned: int
    finished_at: str | None = None
    platforms_completed: list[str] | None = None
    collections_completed: list[str] | None = None
    error: str | None = None

    @classmethod
    def start(
        cls,
        *,
        id: str,
        at: str,
        platforms_planned: int,
        roms_planned: int,
    ) -> SyncRun:
        """Begin a run at ISO timestamp ``at`` with the planned counts to sync."""
        if not id:
            raise ValueError("id is required")
        if platforms_planned < 0:
            raise ValueError("platforms_planned must be non-negative")
        if roms_planned < 0:
            raise ValueError("roms_planned must be non-negative")
        return cls(
            id=id,
            started_at=at,
            status="running",
            platforms_planned=platforms_planned,
            roms_planned=roms_planned,
        )

    def complete(self, at: str, platforms: list[str], collections: list[str]) -> None:
        """Finish the run successfully, recording which platforms/collections synced."""
        self._require_running()
        self.status = "completed"
        self.finished_at = at
        self.platforms_completed = platforms
        self.collections_completed = collections

    def mark_cancelled(self, at: str, reason: str) -> None:
        """Terminate the run as cancelled, recording the human-readable reason."""
        self._require_running()
        self.status = "cancelled"
        self.finished_at = at
        self.error = reason

    def mark_errored(self, at: str, error: str) -> None:
        """Terminate the run as errored, recording the human-readable error detail."""
        self._require_running()
        self.status = "errored"
        self.finished_at = at
        self.error = error

    def _require_running(self) -> None:
        if self.status != "running":
            raise ValueError("run is not running")
