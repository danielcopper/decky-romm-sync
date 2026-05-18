"""In-memory ``DownloadQueueAdapter`` implementation for service tests."""

from __future__ import annotations


class FakeDownloadQueueAdapter:
    """In-memory ``DownloadQueueAdapter`` for tests.

    Backed by a single ``entries`` list so ``poll_and_clear`` is
    deterministic. Tests pre-populate ``entries`` to stage queued
    requests and inspect ``poll_count`` / ``last_path`` for behaviour
    assertions. ``set_missing(True)`` makes the next ``poll_and_clear``
    behave as if the file were missing (returns ``[]`` without clearing).
    """

    def __init__(self, entries: list[dict] | None = None) -> None:
        self.entries: list[dict] = list(entries) if entries else []
        self.poll_count: int = 0
        self.last_path: str | None = None
        self.missing: bool = False

    def set_missing(self, missing: bool) -> None:
        self.missing = missing

    def poll_and_clear(self, path: str) -> list[dict]:
        self.poll_count += 1
        self.last_path = path
        if self.missing:
            return []
        out = list(self.entries)
        self.entries.clear()
        return out
