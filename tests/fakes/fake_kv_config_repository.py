"""In-memory ``KvConfigRepository`` implementation for service tests."""

from __future__ import annotations


class FakeKvConfigRepository:
    """Dict-backed ``KvConfigRepository`` — a flat string-keyed TEXT surface."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self.set_count = 0

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self.set_count += 1
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)

    def _snapshot(self) -> dict[str, str]:
        # Values are immutable strings; a shallow copy of the mapping is enough.
        return dict(self._values)

    def _restore(self, state: dict[str, str]) -> None:
        self._values = state
