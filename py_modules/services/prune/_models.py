"""Shared in-memory state for the vanished-ROM cleanup bounded context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from models.prune import SourceClaim, SteamRecoverySnapshot


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
