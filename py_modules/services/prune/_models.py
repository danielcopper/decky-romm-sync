"""Shared in-memory state for the vanished-ROM cleanup bounded context."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from models.prune import SourceClaim, SteamRecoverySnapshot


# One frontend Steam action request: run id, kind, payload, expected bound rom,
# repoint target, and the group the claim must still match.
ActionRequester = Callable[[str, str, dict[str, object], int | None, int | None, set[int]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PrunePreview:
    """Ephemeral local candidate snapshot that authorizes one explicit start."""

    preview_id: str
    scope: Literal["bulk", "rom"]
    explicit_rom_id: int | None
    candidate_ids: frozenset[int]
    fingerprint: tuple[tuple[object, ...], ...]
    entries: tuple[dict[str, Any], ...]
    free_bytes: int
    server_namespace: str


@dataclass(frozen=True)
class PruneOptions:
    """One-run user choices; never persisted."""

    repoint_shortcuts: bool
    remove_rows: bool
    remove_fully_vanished: bool
    create_recovery_bundle: bool
    include_installed_rom_ids: frozenset[int]


@dataclass
class PendingAction:
    """The one frontend Steam action the backend is awaiting."""

    run_id: str
    token: str
    kind: str
    app_id: int | None
    expected_bound_rom_id: int | None
    target_rom_id: int | None
    group_rom_ids: frozenset[int]
    future: object
    claimed: bool = False
    claim_event: object | None = None
    expires_at: float = 0.0


@dataclass
class InstalledSelection:
    """One preview-bound, incrementally staged installed-content selection."""

    preview_id: str
    selection_id: str
    rom_ids: set[int]
    finalized: bool = False


@dataclass(frozen=True)
class RecoveryHandle:
    """Sealed recovery state that finalization must revalidate and consume."""

    bundle_path: str
    snapshot: dict[str, object]
    save_inventory: dict[str, Any]
    steam_backend: SteamRecoverySnapshot | None
    source_claims: dict[str, SourceClaim]
    bundle_digest: str


@dataclass
class PruneCancellationState:
    """Result state captured while propagating one cleanup cancellation."""

    action_result: dict[str, Any] | None = None
    group_result: dict[str, Any] | None = None
    child_result: Any = None
    child_completed: bool = False
    child_fault: BaseException | None = None


_CANCELLATION_STATE_ATTR = "_prune_cancellation_state"


def cancellation_state(error: BaseException) -> PruneCancellationState:
    """Return the cleanup state attached to the original cancellation."""
    state = getattr(error, _CANCELLATION_STATE_ATTR, None)
    if isinstance(state, PruneCancellationState):
        return state
    state = PruneCancellationState()
    setattr(error, _CANCELLATION_STATE_ATTR, state)
    return state


async def shielded(awaitable: Awaitable[Any]) -> Any:
    """Run *awaitable* to its own end even when this task is cancelled.

    A cancellation arriving mid-mutation must not leave the group unable to say
    what happened, so the child is awaited to completion and its outcome is
    recorded on the cancellation the caller will re-raise.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        state = cancellation_state(exc)
        try:
            state.child_result = await task
            state.child_completed = True
        except asyncio.CancelledError:
            # The child was cancelled too. Its CancelledError carries no
            # captured state, and callers read that state off whatever
            # propagates to decide what the group actually did — so the
            # original cancellation is re-raised instead of this one.
            pass
        except BaseException as child_fault:
            state.child_fault = child_fault
        raise
