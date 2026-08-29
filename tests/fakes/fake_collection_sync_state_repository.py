"""In-memory ``CollectionSyncStateRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.collection_sync_state import CollectionSyncState


class FakeCollectionSyncStateRepository:
    """Dict-backed ``CollectionSyncStateRepository`` keyed by ``(collection_id, collection_kind)``."""

    def __init__(self) -> None:
        self._stamps: dict[tuple[str, str], CollectionSyncState] = {}
        self.save_count = 0

    def get(self, collection_id: str, collection_kind: str) -> CollectionSyncState | None:
        return copy.deepcopy(self._stamps.get((collection_id, collection_kind)))

    def save(self, state: CollectionSyncState) -> None:
        self.save_count += 1
        self._stamps[(state.collection_id, state.collection_kind)] = copy.deepcopy(state)

    def delete(self, collection_id: str, collection_kind: str) -> None:
        self._stamps.pop((collection_id, collection_kind), None)

    def iter_all(self) -> Iterator[CollectionSyncState]:
        return iter([copy.deepcopy(state) for state in self._stamps.values()])

    def has_any(self) -> bool:
        return bool(self._stamps)

    def clear(self) -> None:
        self._stamps = {}

    def _snapshot(self) -> dict[tuple[str, str], CollectionSyncState]:
        return copy.deepcopy(self._stamps)

    def _restore(self, state: dict[tuple[str, str], CollectionSyncState]) -> None:
        self._stamps = state
