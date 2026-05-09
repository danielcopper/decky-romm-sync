"""System clock adapter — concrete ``Clock`` Protocol implementation.

Wraps :mod:`time` and :mod:`datetime` so services can stay free of direct
stdlib clock imports. The class holds no state; all methods read live values
from the standard library on each call.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime


class SystemClock:
    """Real-time ``Clock`` backed by :mod:`time` and :mod:`datetime`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()
