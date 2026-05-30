"""In-memory ``SyncRunRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.sync_run import SyncRun


class FakeSyncRunRepository:
    """Dict-backed ``SyncRunRepository`` keyed by the run's string UUID.

    ``get_latest_completed`` orders by ``started_at`` descending and
    ``get_running`` returns any ``running`` row, matching the adapter queries.
    """

    def __init__(self) -> None:
        self._runs: dict[str, SyncRun] = {}
        self.save_count = 0

    def get(self, run_id: str) -> SyncRun | None:
        return copy.deepcopy(self._runs.get(run_id))

    def save(self, run: SyncRun) -> None:
        self.save_count += 1
        self._runs[run.id] = copy.deepcopy(run)

    def get_latest_completed(self) -> SyncRun | None:
        completed = [run for run in self._runs.values() if run.status == "completed"]
        if not completed:
            return None
        return copy.deepcopy(max(completed, key=lambda run: run.started_at))

    def get_running(self) -> SyncRun | None:
        for run in self._runs.values():
            if run.status == "running":
                return copy.deepcopy(run)
        return None

    def delete_completed(self) -> None:
        self._runs = {run_id: run for run_id, run in self._runs.items() if run.status != "completed"}

    def _snapshot(self) -> dict[str, SyncRun]:
        return copy.deepcopy(self._runs)

    def _restore(self, state: dict[str, SyncRun]) -> None:
        self._runs = state
