"""In-memory ``PlatformSyncStateRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.platform_sync_state import PlatformSyncState


class FakePlatformSyncStateRepository:
    """Dict-backed ``PlatformSyncStateRepository`` keyed by ``platform_slug``."""

    def __init__(self) -> None:
        self._stamps: dict[str, PlatformSyncState] = {}
        self.save_count = 0

    def get(self, platform_slug: str) -> PlatformSyncState | None:
        return copy.deepcopy(self._stamps.get(platform_slug))

    def save(self, state: PlatformSyncState) -> None:
        self.save_count += 1
        self._stamps[state.platform_slug] = copy.deepcopy(state)

    def delete(self, platform_slug: str) -> None:
        self._stamps.pop(platform_slug, None)

    def clear(self) -> None:
        self._stamps = {}

    def _snapshot(self) -> dict[str, PlatformSyncState]:
        return copy.deepcopy(self._stamps)

    def _restore(self, state: dict[str, PlatformSyncState]) -> None:
        self._stamps = state
