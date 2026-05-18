"""In-memory ``DownloadQueueCleanup`` implementation for service tests."""

from __future__ import annotations


class FakeDownloadQueueCleanup:
    """In-memory ``DownloadQueueCleanup`` for tests.

    Records ``evict`` calls in ``evicted`` (a list of rom_ids in call
    order) and counts ``clear()`` invocations in ``cleared``. Tests
    inspect either attribute to assert eviction behaviour without
    standing up a full DownloadService.
    """

    def __init__(self) -> None:
        self.evicted: list[int] = []
        self.cleared: int = 0

    def evict(self, rom_id: int) -> None:
        self.evicted.append(int(rom_id))

    def clear(self) -> None:
        self.cleared += 1
