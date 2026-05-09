"""Asyncio sleeper adapter — concrete ``Sleeper`` Protocol implementation.

Wraps :func:`asyncio.sleep` so services can stay free of direct
``asyncio`` imports for the sole purpose of sleeping.
"""

from __future__ import annotations

import asyncio


class AsyncioSleeper:
    """Real ``Sleeper`` that delegates to :func:`asyncio.sleep`."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
