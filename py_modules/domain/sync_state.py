"""SyncState enum — shared between LibraryService and consumers."""

from enum import Enum


class SyncState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"


class SyncCancelled(Exception):
    """Cooperative cancel signal for the library sync subsystem.

    Raised by ``LibraryFetcher._check_cancelling`` (mid per-unit
    pagination) and the ``SyncOrchestrator.sync_preview`` per-unit
    checkpoint once a user cancel has flipped ``sync_state`` to
    ``CANCELLING``. Caught only by the cooperative handlers (the
    orchestrator's unit-loop and ``sync_preview`` ``except
    SyncCancelled``), which route it into the graceful finalize. It is a
    DISTINCT type from ``asyncio.CancelledError`` so a cooperative sync
    cancel is never conflated with a real ``asyncio`` task cancellation
    (the sync task is never ``task.cancel()``'d — ``cancel_sync`` only
    sets the state flag), and the real-cancel handlers
    (``build_work_queue``, the wait-loop sleep) keep
    ``except asyncio.CancelledError`` so a genuine cancel still
    propagates.

    A plain ``Exception`` (not ``BaseException``): the only generic
    ``except Exception`` on its propagation path (the fetcher's
    ``list_roms`` guard) re-raises and carries an explicit
    ``except SyncCancelled: raise`` so a cancel is never logged as a
    fetch failure — so the signal reaches the cooperative handlers
    untouched without relying on ``BaseException`` propagation.
    """
