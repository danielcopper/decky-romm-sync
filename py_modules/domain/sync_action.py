"""The vocabulary a save-sync decision is answered in.

One ``SyncAction`` — ``Skip``, ``Upload``, ``Download`` or ``Conflict`` — says what
the service must do for a single ``(rom, filename, slot)`` triple. Services dispatch
on these variants and the compiled gavel core answers in them, reached through the
``ComputeSyncActionFn`` / ``ResolveUploadConflictFn`` seams. The decision itself is
not made here: see ``docs/architecture/save-file-sync-architecture.md``.

No I/O. No imports from services or adapters. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Skip:
    """Nothing to do.

    ``reason`` is one of: ``"synced"``, ``"nothing_to_sync"``.

    ``adopt_baseline`` signals that the service must persist the current
    ``local_hash`` as the file's ``last_sync_hash`` (state mutation only —
    no network I/O). Used for the "is_current=true, local exists, no
    baseline" recovery case where we want subsequent runs to detect drift.
    """

    reason: str
    adopt_baseline: bool = False


@dataclass(frozen=True)
class Upload:
    """Push local to server.

    ``target_save_id`` is retained only as a status-display echo (the save we
    believe we're superseding); it no longer selects an HTTP verb. Every
    automatic upload dispatch POSTs a new save in the slot and relies on RomM's
    409 as the cross-device currency backstop (ADR-0017). ``None`` when no
    specific server save is in view.
    """

    target_save_id: int | None


@dataclass(frozen=True)
class Download:
    """Adopt the chosen server save (raw RomM API dict)."""

    server_save: dict[str, Any]


@dataclass(frozen=True)
class Conflict:
    """Both sides changed. User must decide via the resolve callable."""

    server_save: dict[str, Any]


SyncAction = Skip | Upload | Download | Conflict
