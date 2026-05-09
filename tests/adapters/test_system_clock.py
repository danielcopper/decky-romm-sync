"""Tests for the SystemClock adapter — wraps time/datetime stdlib calls."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from adapters.system_clock import SystemClock


class TestSystemClock:
    def test_now_returns_timezone_aware_utc_datetime(self):
        clock = SystemClock()
        result = clock.now()
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        assert result.utcoffset() == UTC.utcoffset(None)

    def test_monotonic_returns_float_that_increases(self):
        clock = SystemClock()
        first = clock.monotonic()
        second = clock.monotonic()
        assert isinstance(first, float)
        assert isinstance(second, float)
        assert second >= first

    def test_time_returns_float_close_to_real_wall_clock(self):
        clock = SystemClock()
        before = time.time()
        result = clock.time()
        after = time.time()
        assert isinstance(result, float)
        # Generous tolerance: within 1 second of the real wall clock.
        assert before - 1.0 <= result <= after + 1.0
