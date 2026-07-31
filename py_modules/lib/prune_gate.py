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
_LEASE_SECONDS = 300.0

# Plain-language names for the claim keys a user can actually be blocked behind.
# A key with no entry here falls back to the generic refusal text rather than
# leaking an internal token into the UI.
_HOLDER_NAMES = {
    "launch_reconfirm": "checking a game's launch settings",
    "installed_reconcile": "checking which games are installed",
    "sgdb_artwork": "downloading artwork",
    "version_switch": "switching versions",
    "shortcut_removal": "updating Steam shortcuts",
    "rom_uninstall": "uninstalling a game",
    "bulk_uninstall": "uninstalling games",
    "system_core": "changing an emulator core",
    "game_core": "changing an emulator core",
    "disc_selection": "changing the selected disc",
    "sync_complete": "finishing a library sync",
    "sync_stale": "finishing a library sync",
    "download_complete": "finishing a download",
    "prune_complete": "finishing a cleanup",
    "migration_relaunch_options": "finishing a RetroDECK move",
}


@dataclass(frozen=True)
class _Holder:
    """One live claim, named so a refusal can say who is holding the gate."""

    label: str
    kind: str
    acquired_at: float


@dataclass
class _PruneAdmissionGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Transient per-callable registrations, keyed by registration id.
    operations: dict[int, _Holder] = field(default_factory=dict)
    # Frontend-owned bounded claims, keyed by token; value carries its deadline.
    leases: dict[str, tuple[_Holder, float]] = field(default_factory=dict)
    prune_reservations: int = 0
    # Separate counters: a lease token is user-visible state the frontend round-
    # trips, so transient registrations must not shift the ids it sees.
    next_lease_id: int = 1
    next_operation_id: int = 1

    @property
    def conflicting_operations(self) -> int:
        """Total live claims — the count the admission decision is made on.

        Derived rather than tracked: a counter that drifts from the holder
        registry is exactly the state that made #1570 F13's holder impossible
        to identify.
        """
        return len(self.operations) + len(self.leases)

    def busy(self) -> bool:
        """Whether any conflicting claim is currently held."""
        return bool(self.operations) or bool(self.leases)

    def take_lease_id(self) -> int:
        value = self.next_lease_id
        self.next_lease_id += 1
        return value

    def take_operation_id(self) -> int:
        value = self.next_operation_id
        self.next_operation_id += 1
        return value


def _gate(owner: object) -> _PruneAdmissionGate:
    gate = getattr(owner, "_prune_admission_gate", None)
    if gate is None:
        gate = _PruneAdmissionGate()
        owner.__dict__["_prune_admission_gate"] = gate
    return gate


def _info(owner: object, message: str) -> None:
    """Log at INFO through the owner's wired logger, if it has one.

    Resolved off the owner rather than imported so ``lib`` keeps no runtime
    dependency, and so a bare ``Plugin()`` in a test fixture stays silent
    instead of raising.
    """
    logger = getattr(owner, "_prune_gate_logger", None)
    if logger is not None:
        logger.info(message)


def _debug(owner: object, message: str) -> None:
    """Log gate lifecycle through the owner's settings-filtered debug logger."""
    log = getattr(owner, "_log_debug", None)
    if callable(log):
        log(message)


def _describe_holders(gate: _PruneAdmissionGate, now: float) -> str:
    """One-line inventory of every current holder, for a refusal log line."""
    parts = [f"{holder.label} (operation, held {now - holder.acquired_at:.0f}s)" for holder in gate.operations.values()]
    parts += [
        f"{holder.label} (lease {token}, held {now - holder.acquired_at:.0f}s, expires in {deadline - now:.0f}s)"
        for token, (holder, deadline) in gate.leases.items()
    ]
    return ", ".join(parts) if parts else "none"


def _blocked_message(gate: _PruneAdmissionGate) -> str:
    """Name the holder in the refusal when its claim has a user-facing name.

    The oldest claim is the one actually standing in the way, and a lease
    outranks a transient registration because it is the one that can persist
    long enough for a person to notice being blocked.
    """
    leases = sorted(gate.leases.values(), key=lambda item: item[0].acquired_at)
    operations = sorted(gate.operations.values(), key=lambda holder: holder.acquired_at)
    for holder in [item[0] for item in leases] + operations:
        name = _HOLDER_NAMES.get(holder.label)
        if name is not None:
            return (
                f"Another local-data operation is in progress ({name}); wait for it to finish before starting cleanup."
            )
    return _OPERATION_BLOCKED_MESSAGE


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
            _expire_leases(self, gate)
            if gate.prune_reservations or service.is_active():
                return {"success": False, "reason": "prune_active", "message": _BLOCKED_MESSAGE}
            registration = gate.take_operation_id()
            gate.operations[registration] = _Holder(
                label=method.__name__, kind="operation", acquired_at=asyncio.get_running_loop().time()
            )
        try:
            return await method(self, *args, **kwargs)
        finally:
            async with gate.lock:
                gate.operations.pop(registration, None)

    wrapper._prune_active_blocked = True  # type: ignore[attr-defined]
    return wrapper


def prune_exclusive_start(method):
    """Atomically refuse prune admission while a conflicting callable is active.

    The refusal check and the claim reservation share one lock hold, so no
    conflicting registration can slip between them. Admission itself then runs
    unlocked: the reservation already refuses every conflicting callable, and
    holding the lock across the run's preview rebuild would make each of them
    wait for that rebuild instead of learning its verdict immediately.

    A refusal logs the complete holder inventory at INFO. Without it a blocked
    cleanup is indistinguishable from a plugin that has stopped responding, and
    the holder cannot be identified after the fact (#1570 F13).
    """

    @functools.wraps(method)
    async def wrapper(self, *args: Any, **kwargs: Any):
        gate = _gate(self)
        async with gate.lock:
            _expire_leases(self, gate)
            if gate.busy():
                now = asyncio.get_running_loop().time()
                _info(self, f"Cleanup admission refused — gate held by: {_describe_holders(gate, now)}")
                return {
                    "success": False,
                    "reason": "operation_active",
                    "message": _blocked_message(gate),
                }
            gate.prune_reservations += 1
        try:
            return await method(self, *args, **kwargs)
        finally:
            # Released without the lock on purpose: the single-threaded loop
            # cannot interleave a bare decrement, while awaiting a contended
            # lock here could lose the release to a cancellation.
            gate.prune_reservations -= 1

    wrapper._prune_exclusive_start = True  # type: ignore[attr-defined]
    return wrapper


async def retain_prune_conflict(owner: object, task: asyncio.Task[Any], label: str) -> None:
    """Transfer a callable's conflict claim to detached work until its task ends."""
    gate = _gate(owner)
    async with gate.lock:
        registration = gate.take_operation_id()
        gate.operations[registration] = _Holder(
            label=label, kind="operation", acquired_at=asyncio.get_running_loop().time()
        )
    _debug(owner, f"[prune-gate] retained {label} for detached work (#{registration})")

    async def release() -> None:
        async with gate.lock:
            gate.operations.pop(registration, None)
        _debug(owner, f"[prune-gate] released {label} (#{registration})")

    def done(_task: asyncio.Task[Any]) -> None:
        asyncio.get_running_loop().create_task(release())

    task.add_done_callback(done)


async def acquire_prune_conflict_lease(owner: object, key: str) -> str:
    """Hold a bounded tokenized claim across a frontend-owned operation."""
    gate = _gate(owner)
    async with gate.lock:
        _expire_leases(owner, gate)
        now = asyncio.get_running_loop().time()
        token = f"{key}:{gate.take_lease_id()}"
        gate.leases[token] = (_Holder(label=key, kind="lease", acquired_at=now), now + _LEASE_SECONDS)
    _debug(owner, f"[prune-gate] acquired lease {token} ({key})")
    return token


async def release_orphaned_frontend_leases(owner: object) -> int:
    """Drop every frontend-owned lease and report how many were orphaned.

    A lease is released by the continuation that received it. A frontend whose
    JS context is torn down mid-call — the double mount at plugin load — never
    reaches that release, and never renews either, so the lease pins the gate
    for its full TTL with nobody behind it (#1570 F18).

    A newly mounted frontend is the proof that no earlier continuation can still
    be running: the context that owned them is gone. That makes mount the one
    moment an orphan is provably safe to drop, which is why this is called there
    and nowhere else. Run claims and callable registrations are untouched — only
    the frontend's own leases are the frontend's to disown.
    """
    gate = _gate(owner)
    async with gate.lock:
        _expire_leases(owner, gate)
        orphaned = list(gate.leases.items())
        gate.leases.clear()
    now = asyncio.get_running_loop().time()
    for token, (holder, _deadline) in orphaned:
        _info(
            owner,
            f"[prune-gate] released orphaned lease {token} ({holder.label}) held "
            f"{now - holder.acquired_at:.0f}s by a frontend that is no longer mounted",
        )
    return len(orphaned)


async def release_prune_conflict_lease(owner: object, token: str) -> None:
    """Release a previously acquired frontend-operation conflict claim."""
    gate = _gate(owner)
    async with gate.lock:
        _expire_leases(owner, gate)
        released = gate.leases.pop(token, None)
    if released is not None:
        held = asyncio.get_running_loop().time() - released[0].acquired_at
        _debug(owner, f"[prune-gate] released lease {token} after {held:.0f}s")


async def renew_prune_conflict_lease(owner: object, token: str) -> bool:
    """Extend a live frontend continuation lease; never revive an expired owner."""
    gate = _gate(owner)
    async with gate.lock:
        _expire_leases(owner, gate)
        current = gate.leases.get(token)
        if current is None:
            return False
        now = asyncio.get_running_loop().time()
        gate.leases[token] = (current[0], now + _LEASE_SECONDS)
        held = now - current[0].acquired_at
    _debug(owner, f"[prune-gate] renewed lease {token} (held {held:.0f}s)")
    return True


def _expire_leases(owner: object, gate: _PruneAdmissionGate) -> None:
    """Drop timed-out leases.

    An expiry is logged at INFO, not debug: a lease that reached its deadline
    means the owner never released it, which is a leak worth seeing without
    turning debug logging on.
    """
    now = asyncio.get_running_loop().time()
    expired = [token for token, (_holder, deadline) in gate.leases.items() if deadline <= now]
    for token in expired:
        holder, _deadline = gate.leases.pop(token)
        _info(
            owner,
            f"[prune-gate] lease {token} ({holder.label}) expired after "
            f"{now - holder.acquired_at:.0f}s without being released",
        )
