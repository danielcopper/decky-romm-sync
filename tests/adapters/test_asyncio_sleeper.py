"""Tests for the AsyncioSleeper adapter — wraps asyncio.sleep."""

from __future__ import annotations

import time

import pytest

from adapters.asyncio_sleeper import AsyncioSleeper


class TestAsyncioSleeper:
    @pytest.mark.asyncio
    async def test_sleep_actually_awaits_requested_duration(self):
        sleeper = AsyncioSleeper()
        start = time.monotonic()
        await sleeper.sleep(0.01)
        elapsed = time.monotonic() - start
        # Generous bounds: at least ~5ms, at most 500ms (CI scheduler jitter).
        assert 0.005 <= elapsed <= 0.5
