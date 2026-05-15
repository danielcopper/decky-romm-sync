"""Determinism seam Protocols for services.

Services obtain wall-clock time, monotonic readings, UUIDs, and async
sleeps through these Protocols so tests can pin time, freeze IDs, and
collapse delays without monkey-patching ``time``, ``datetime``,
``uuid``, or ``asyncio``. Concrete implementations live in adapters;
fakes live in ``tests.fakes``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Sole source of wall-clock and monotonic time for services.

    Services must obtain timestamps through a Clock so deterministic tests
    can pin time without monkey-patching ``time`` or ``datetime``. Concrete
    implementations belong in adapters (``adapters.system_clock``); fakes
    live in ``tests.fakes.system_time``.
    """

    def now(self) -> datetime:
        """Return the current UTC-aware wall-clock instant."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic clock reading in seconds.

        Suitable for measuring elapsed durations; not comparable to ``time``.
        """
        ...

    def time(self) -> float:
        """Return the current Unix timestamp in seconds since the epoch."""
        ...


class UuidGen(Protocol):
    """Sole source of UUID values for services.

    Services consume this Protocol instead of ``uuid.uuid4`` directly so
    tests can supply deterministic IDs.
    """

    def uuid4(self) -> str:
        """Return a new random UUID4 in canonical string form (e.g. ``"a1b2…"``)."""
        ...


class Sleeper(Protocol):
    """Sole async-sleep seam for services.

    Services await ``sleeper.sleep(seconds)`` instead of ``asyncio.sleep``
    so tests can collapse delays and assert on requested durations.
    """

    async def sleep(self, seconds: float) -> None:
        """Suspend the current coroutine for ``seconds`` seconds."""
        ...
