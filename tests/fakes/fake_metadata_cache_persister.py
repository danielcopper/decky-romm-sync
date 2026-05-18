"""In-memory ``MetadataCachePersister`` implementation for service tests."""

from __future__ import annotations


class FakeMetadataCachePersister:
    """In-memory ``MetadataCachePersister`` for tests.

    Counts how many times ``save_metadata()`` was invoked. Tests use
    ``save_count`` to assert the persister was triggered without
    standing up a real on-disk write.
    """

    def __init__(self) -> None:
        self.save_count = 0

    def save_metadata(self) -> None:
        self.save_count += 1
