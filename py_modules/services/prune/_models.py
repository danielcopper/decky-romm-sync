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

# Rom ids the last complete fetch returned, excluding a set and capped: the
# control subjects a liveness round falls back on when nothing answered live.
CanaryRomIdsFn = Callable[[set[int], int], list[int]]


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


class BackupControl:
    """Cooperative stop flag for one group's pre-commit backup phase.

    Set on the event-loop thread when the run is cancelled; polled on the
    executor worker thread between artifacts and between copy/hash chunks, so a
    multi-hundred-megabyte copy stops within a chunk instead of running to
    completion. Only ever moves from not-aborted to aborted, and only before
    anything has been mutated — a committed phase is shielded and never consults
    it.

    Plain bool — not ``threading.Event`` — because the import-linter
    ``no-stdlib-io-in-services`` contract forbids ``threading`` in services, and
    under the GIL a one-way set-once flip needs no synchronisation.
    """

    __slots__ = ("aborted",)

    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        """Ask the in-flight backup worker to stop at its next check."""
        self.aborted = True

    def is_aborted(self) -> bool:
        """Whether the backup worker has been asked to stop."""
        return self.aborted


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


async def shielded(awaitable: Awaitable[Any], *, on_cancel: Callable[[], None] | None = None) -> Any:
    """Run *awaitable* to its own end even when this task is cancelled.

    A cancellation arriving mid-mutation must not leave the group unable to say
    what happened, so the child is awaited to completion and its outcome is
    recorded on the cancellation the caller will re-raise.

    *on_cancel* runs the moment the cancellation is observed, before the child
    is awaited. It is how a child that is **not** yet committed — the backup
    phase — is told to stop early: the wait still completes, but it completes in
    a chunk rather than in minutes. A committed phase passes nothing and is
    awaited to its natural end, unchanged.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        if on_cancel is not None:
            on_cancel()
        state = cancellation_state(exc)
        try:
            state.child_result = await task
            state.child_completed = True
        except asyncio.CancelledError as child_cancel:
            # The child was cancelled too, and its own CancelledError carries
            # whatever state happens to be attached to it — never what this run
            # captured. This run's record is attached to it here, overwriting a
            # foreign one, so the cancellation that propagates says what this
            # group did. Callers read the attached state, never the instance.
            setattr(child_cancel, _CANCELLATION_STATE_ATTR, state)
            raise
        except Exception as child_fault:
            # Only a fault the run can report: an interpreter-level exit
            # (KeyboardInterrupt, SystemExit) is not this group's to absorb into
            # a result, so it stays uncaught and takes the process down.
            state.child_fault = child_fault
        raise
