"""In-memory ``Clock`` / ``UuidGen`` / ``Sleeper`` fakes for service tests.

These fakes let tests pin time, UUIDs, and async-sleep delays without
monkey-patching the standard library. Each fake records call activity so
tests can assert on what the service requested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_DEFAULT_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_DEFAULT_UUID = "00000000-0000-4000-8000-000000000000"


class FakeClock:
    """In-memory ``Clock`` for tests.

    Parameters
    ----------
    now:
        Initial wall-clock instant. Defaults to ``2026-01-01T00:00:00Z``
        so test assertions can use stable timestamps. Must be timezone-aware
        if supplied.
    monotonic:
        Initial monotonic reading. Defaults to ``0.0``; tests rarely need
        to override this directly — call :meth:`advance` instead.
    """

    def __init__(self, now: datetime | None = None, monotonic: float = 0.0) -> None:
        self._now: datetime = now if now is not None else _DEFAULT_NOW
        self._monotonic: float = monotonic
        self.now_calls = 0
        self.monotonic_calls = 0
        self.time_calls = 0

    def now(self) -> datetime:
        self.now_calls += 1
        return self._now

    def monotonic(self) -> float:
        self.monotonic_calls += 1
        return self._monotonic

    def time(self) -> float:
        self.time_calls += 1
        return self._now.timestamp()

    def advance(self, seconds: float) -> None:
        """Bump both ``monotonic`` and ``now`` forward by ``seconds``."""
        self._monotonic += seconds
        self._now = self._now + timedelta(seconds=seconds)


class FakeUuidGen:
    """In-memory ``UuidGen`` for tests.

    Parameters
    ----------
    values:
        Optional FIFO queue of canned UUID values. Each :meth:`uuid4` call
        pops from the front; once empty, a deterministic default UUID is
        returned so tests don't need to count exact call counts.
    """

    def __init__(self, values: list[str] | None = None) -> None:
        self._values: list[str] = list(values) if values else []
        self.call_count = 0

    def uuid4(self) -> str:
        self.call_count += 1
        if self._values:
            return self._values.pop(0)
        return _DEFAULT_UUID


class FakeSleeper:
    """In-memory ``Sleeper`` for tests.

    Records the requested durations in :attr:`calls` and returns immediately
    so tests don't pay real wall-clock waits.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
