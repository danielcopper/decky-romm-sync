"""In-memory ``FirmwareCacheRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.firmware_cache import FirmwareCacheEntry


class FakeFirmwareCacheRepository:
    """Dict-backed ``FirmwareCacheRepository`` keyed by ``(platform_slug, name)``.

    ``replace_all`` mirrors the wholesale-refresh contract: the prior cache is
    dropped before the new entries are stored. ``replace_count`` lets tests
    assert refreshes.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], FirmwareCacheEntry] = {}
        self.replace_count = 0

    def get(self, platform_slug: str, name: str) -> FirmwareCacheEntry | None:
        return copy.deepcopy(self._entries.get((platform_slug, name)))

    def iter_all(self) -> Iterator[FirmwareCacheEntry]:
        return iter([copy.deepcopy(e) for e in self._entries.values()])

    def replace_all(self, entries: list[FirmwareCacheEntry]) -> None:
        self.replace_count += 1
        self._entries = {(e.platform_slug, e.name): copy.deepcopy(e) for e in entries}

    def clear(self) -> None:
        self._entries = {}

    def get_cache_epoch(self) -> float | None:
        for entry in self._entries.values():
            return entry.cached_at
        return None

    def _snapshot(self) -> dict[tuple[str, str], FirmwareCacheEntry]:
        return copy.deepcopy(self._entries)

    def _restore(self, state: dict[tuple[str, str], FirmwareCacheEntry]) -> None:
        self._entries = state
