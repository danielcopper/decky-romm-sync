"""Device-level single-owner serialization gate for save-sync.

One in-flight save-sync run per device: a second trigger waits (queue
semantics) for the in-flight run to finish rather than running alongside
it, and the wait is bounded so a stuck run never traps the launch path —
on expiry the caller gets a timeout it can turn into its busy
fallthrough. The :class:`asyncio.Lock` IS the serializer/queue; this
module owns only the bounded-acquire discipline around it.

Run-lifecycle state (run ids, sync-state boxes) and cancellation are out
of scope here — those belong to a later phase. What lives here is the
narrow gate that admits exactly one run at a time and times out the rest.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = [
    "POST_EXIT_GATE_TIMEOUT",
    "PRE_LAUNCH_GATE_TIMEOUT",
    "SYNC_ALL_GATE_TIMEOUT",
    "SYNC_ROM_GATE_TIMEOUT",
    "SaveSyncGate",
    "SaveSyncTimeoutError",
]

# Per-trigger bounded-wait budgets (seconds). The launch-path triggers
# carry the tightest budgets so a queued run never stalls the Play button
# longer than the user would tolerate; the manual full-library sweep gets
# the most slack because it legitimately runs longest.
PRE_LAUNCH_GATE_TIMEOUT: float = 30.0
POST_EXIT_GATE_TIMEOUT: float = 60.0
SYNC_ROM_GATE_TIMEOUT: float = 15.0
SYNC_ALL_GATE_TIMEOUT: float = 60.0


class SaveSyncTimeoutError(Exception):
    """Raised when a save-sync run cannot acquire the device gate in time.

    Signals that another run is in flight and the bounded wait elapsed — the
    caller turns this into its trigger-specific busy fallthrough. It is a
    LOCAL scheduling outcome: no caller may report it as a server verdict.
    """


class SaveSyncGate:
    """Admits one save-sync run per device, queuing and bounding the rest.

    A single :class:`asyncio.Lock` serializes every save-sync run on the
    device: the first caller holds it for the duration of its run, later
    callers wait their turn (queue semantics) up to a per-call timeout. The
    gate sits OUTSIDE the per-ROM lock — it is the device-wide single-owner
    seam, not a per-ROM one. It holds no run-lifecycle state and offers no
    cancellation; those belong to a later phase.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def bounded_run(self, *, max_wait: float) -> AsyncIterator[None]:
        """Acquire the device gate within *max_wait* seconds, holding it for the body.

        Waits up to *max_wait* seconds to acquire the gate (queuing behind any
        in-flight run). On expiry it raises :class:`SaveSyncTimeoutError`
        WITHOUT holding the lock, so a timed-out caller never traps the next
        run. On success the lock is released in ``finally`` even if the body
        raises.
        """
        acquired = False
        try:
            async with asyncio.timeout(max_wait):
                await self._lock.acquire()
                acquired = True
        except TimeoutError as exc:
            # The acquire can win a photo-finish with the deadline; if it did,
            # release here so a timed-out caller never leaks the held lock.
            if acquired:
                self._lock.release()
            raise SaveSyncTimeoutError(f"save-sync gate not acquired within {max_wait}s") from exc
        try:
            yield
        finally:
            self._lock.release()

    def is_in_flight(self) -> bool:
        """Whether a save-sync run currently holds the gate."""
        return self._lock.locked()
