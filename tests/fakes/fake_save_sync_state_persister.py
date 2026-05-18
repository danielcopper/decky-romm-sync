"""In-memory ``SaveSyncStatePersister`` implementation for service tests."""

from __future__ import annotations

import copy


class FakeSaveSyncStatePersister:
    """In-memory ``SaveSyncStatePersister`` for tests.

    Keeps the most recently saved dict in ``self.last_saved`` and the
    canned payload returned by ``load`` in ``self.canned_load``. Tests
    that don't care about persistence can use the default (no canned
    payload, returns None) and rely on ``last_saved`` for assertions.
    """

    def __init__(self, *, canned_load: dict | None = None) -> None:
        self.canned_load = canned_load
        self.last_saved: dict | None = None
        self.save_count = 0
        self.load_count = 0

    def save(self, data: dict) -> None:
        self.save_count += 1
        # Snapshot a deep-ish copy so later in-memory mutations don't
        # silently change what the test inspects.
        self.last_saved = copy.deepcopy(data)

    def load(self) -> dict | None:
        self.load_count += 1
        return self.canned_load
