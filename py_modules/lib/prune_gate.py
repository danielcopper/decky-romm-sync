"""Decorator that blocks conflicting Decky callables during explicit prune."""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass, field
from typing import Any

_BLOCKED_MESSAGE = "A removed-game cleanup is in progress; wait for it to finish before changing local game data."
_OPERATION_BLOCKED_MESSAGE = (
    "Another local-data operation is in progress; wait for it to finish before starting cleanup."
)


@dataclass
class _PruneAdmissionGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    conflicting_operations: int = 0
    leases: dict[str, int] = field(default_factory=dict)


def _gate(owner: object) -> _PruneAdmissionGate:
    gate = getattr(owner, "_prune_admission_gate", None)
    if gate is None:
        gate = _PruneAdmissionGate()
        owner.__dict__["_prune_admission_gate"] = gate
    return gate


def prune_active_blocked(method):
    """Return a canonical failure while the wired prune service owns its run claim."""

    @functools.wraps(method)
    async def wrapper(self, *args: Any, **kwargs: Any):
        service = getattr(self, "_prune_service", None)
        if service is None:
            raise RuntimeError(
                f"@prune_active_blocked on {method.__name__!r}: _prune_service is unwired; refusing to bypass the gate."
            )
        gate = _gate(self)
        async with gate.lock:
            if service.is_active():
                return {"success": False, "reason": "prune_active", "message": _BLOCKED_MESSAGE}
            gate.conflicting_operations += 1
        try:
            return await method(self, *args, **kwargs)
        finally:
            async with gate.lock:
                gate.conflicting_operations -= 1

    wrapper._prune_active_blocked = True  # type: ignore[attr-defined]
    return wrapper


def prune_exclusive_start(method):
    """Atomically refuse prune admission while a conflicting callable is active."""

    @functools.wraps(method)
    async def wrapper(self, *args: Any, **kwargs: Any):
        gate = _gate(self)
        async with gate.lock:
            if gate.conflicting_operations:
                return {
                    "success": False,
                    "reason": "operation_active",
                    "message": _OPERATION_BLOCKED_MESSAGE,
                }
            return await method(self, *args, **kwargs)

    wrapper._prune_exclusive_start = True  # type: ignore[attr-defined]
    return wrapper


async def retain_prune_conflict(owner: object, task: asyncio.Task[Any]) -> None:
    """Transfer a callable's conflict claim to detached work until its task ends."""
    gate = _gate(owner)
    async with gate.lock:
        gate.conflicting_operations += 1

    async def release() -> None:
        async with gate.lock:
            gate.conflicting_operations -= 1

    def done(_task: asyncio.Task[Any]) -> None:
        asyncio.get_running_loop().create_task(release())

    task.add_done_callback(done)


async def acquire_prune_conflict_lease(owner: object, key: str) -> None:
    """Hold a conflict claim across a frontend-owned multi-call operation."""
    gate = _gate(owner)
    async with gate.lock:
        gate.leases[key] = gate.leases.get(key, 0) + 1
        gate.conflicting_operations += 1


async def release_prune_conflict_lease(owner: object, key: str) -> None:
    """Release a previously acquired frontend-operation conflict claim."""
    gate = _gate(owner)
    async with gate.lock:
        count = gate.leases.get(key, 0)
        if count:
            if count == 1:
                del gate.leases[key]
            else:
                gate.leases[key] = count - 1
            gate.conflicting_operations -= 1
