"""Shared in-memory state for the vanished-ROM cleanup bounded context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from models.prune import SteamRecoverySnapshot


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
    future: object
    claimed: bool = False
    claim_event: object | None = None
    expires_at: float = 0.0


@dataclass(frozen=True)
class RecoveryHandle:
    """Sealed recovery state that finalization must revalidate and consume."""

    bundle_path: str
    snapshot: dict[str, object]
    save_inventory: dict[str, Any]
    steam_backend: SteamRecoverySnapshot | None
