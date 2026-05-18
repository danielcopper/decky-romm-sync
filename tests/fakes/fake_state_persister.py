"""In-memory ``StatePersister`` implementation for service tests."""

from __future__ import annotations


class FakeStatePersister:
    """In-memory ``StatePersister`` for tests.

    Counts how many times ``save_state()`` was invoked. Tests use
    ``save_count`` to assert the persister was triggered without
    standing up a real on-disk write.
    """

    def __init__(self) -> None:
        self.save_count = 0

    def save_state(self) -> None:
        self.save_count += 1
