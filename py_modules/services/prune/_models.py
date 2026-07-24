"""Shared in-memory state for the vanished-ROM cleanup bounded context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


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


@dataclass(frozen=True)
class PendingAction:
    """The one frontend Steam action the backend is awaiting."""

    run_id: str
    token: str
    kind: str
    app_id: int | None
    future: object
