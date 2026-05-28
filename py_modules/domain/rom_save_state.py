"""RomSaveState — per-ROM save-sync state for one tracked ROM.

The active slot and whether the user confirmed it, the emulator/system the ROM
runs under, the last core synced, our upload attribution, the merged slot
listing the UI reads, and the per-file sync baselines the newest-wins matrix
uses to detect drift. References its Rom by id (the registry key). The merge
logic that produces the slot listing lives in a service; this aggregate accepts
the result and guards the slot/file invariants.

Invariants enforced here:

1. Every tracked file (an entry in ``files``) carries both a hash baseline and a
   server save id — :meth:`adopt_baseline` is the only way in and requires both.
2. A non-legacy active slot always has its key present in ``slots`` (the legacy
   ``None`` slot uses the ``""`` key).
3. ``own_upload_ids`` never grows by mutating ``None`` — :meth:`track_own_upload`
   starts a list when attribution was previously unknown.

``FileSyncState`` is the immutable per-file value object the aggregate builds
whole; it has no behaviour of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain._aggregate import cosmic_aggregate


@dataclass(frozen=True, slots=True)
class FileSyncState:
    """Per-file sync baseline — last-observed hash, sizes, and timestamps.

    Immutable value object owned by :class:`RomSaveState`; the aggregate builds
    one whole on :meth:`RomSaveState.adopt_baseline` so the newest-wins matrix
    can detect drift against it on the next sync.
    """

    tracked_save_id: int | None = None
    last_sync_hash: str | None = None
    last_sync_at: str = ""
    last_sync_server_updated_at: str = ""
    last_sync_server_save_id: int | None = None
    last_sync_server_size: int | None = None
    last_sync_local_mtime: float | None = None
    last_sync_local_size: int | None = None


@cosmic_aggregate
class RomSaveState:
    """Save-sync state for one ROM — slot config, attribution, per-file baselines."""

    active_slot: str | None = None
    slot_confirmed: bool = False
    emulator: str = "retroarch"
    system: str = ""
    last_synced_core: str | None = None
    # ``None`` means "uploader attribution unknown / legacy"; ``[]`` means "we
    # definitely uploaded nothing". Both are meaningful — the distinction lets
    # the UI hide the attribution badge for legacy entries instead of asserting
    # "not yours".
    own_upload_ids: list[int] | None = None
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, FileSyncState] = field(default_factory=dict)
    last_sync_check_at: str | None = None

    def adopt_baseline(
        self,
        filename: str,
        *,
        tracked_save_id: int,
        last_sync_hash: str,
        last_sync_at: str = "",
        last_sync_server_updated_at: str = "",
        last_sync_server_save_id: int | None = None,
        last_sync_server_size: int | None = None,
        last_sync_local_mtime: float | None = None,
        last_sync_local_size: int | None = None,
    ) -> None:
        """Record ``filename``'s sync baseline, replacing any existing entry.

        The only way to add a file to ``files`` — enforces that every tracked
        file carries both a server save id and a hash baseline (invariant 1).
        Re-calling with an existing filename re-adopts the baseline under the
        known ``tracked_save_id``. Raises ``ValueError`` if the id is not
        positive or the hash is empty.
        """
        if tracked_save_id <= 0:
            raise ValueError("tracked_save_id must be positive")
        if not last_sync_hash:
            raise ValueError("last_sync_hash is required to adopt a baseline")
        self.files[filename] = FileSyncState(
            tracked_save_id=tracked_save_id,
            last_sync_hash=last_sync_hash,
            last_sync_at=last_sync_at,
            last_sync_server_updated_at=last_sync_server_updated_at,
            last_sync_server_save_id=last_sync_server_save_id,
            last_sync_server_size=last_sync_server_size,
            last_sync_local_mtime=last_sync_local_mtime,
            last_sync_local_size=last_sync_local_size,
        )

    def track_own_upload(self, save_id: int) -> None:
        """Attribute ``save_id`` to an upload we made (idempotent).

        Starts the attribution list when it was previously unknown (``None``)
        rather than mutating ``None`` (invariant 3). Already-tracked ids are
        ignored.
        """
        if self.own_upload_ids is None:
            self.own_upload_ids = [save_id]
        elif save_id not in self.own_upload_ids:
            self.own_upload_ids.append(save_id)

    def confirm_slot(self, name: str | None) -> None:
        """Confirm ``name`` as the user-chosen active slot.

        Normalizes the legacy empty string to ``None``, marks the slot
        confirmed, and ensures the slot's key exists in ``slots`` (invariant 2 —
        legacy ``None`` uses the ``""`` key).
        """
        normalized = name or None
        self.active_slot = normalized
        self.slot_confirmed = True
        self.slots.setdefault(normalized or "", {"source": "local", "count": 0, "latest_updated_at": None})

    def switch_active_slot(self, name: str | None) -> None:
        """Switch the active slot to ``name`` without confirming it.

        Same empty-string normalization and slots-key guarantee as
        :meth:`confirm_slot`, but leaves ``slot_confirmed`` untouched — a switch
        is not a confirmation.
        """
        normalized = name or None
        self.active_slot = normalized
        self.slots.setdefault(normalized or "", {"source": "local", "count": 0, "latest_updated_at": None})

    def mark_sync_evaluated(self, at: str) -> None:
        """Record that the sync matrix was last evaluated at ISO timestamp ``at``."""
        self.last_sync_check_at = at

    def record_synced_core(self, core: str, emulator: str) -> None:
        """Stamp the core and emulator the last sync ran under."""
        self.last_synced_core = core
        self.emulator = emulator

    def refresh_slot_listing(self, merged: dict[str, dict[str, Any]]) -> None:
        """Replace the slot listing with the service-computed ``merged`` view."""
        self.slots = merged

    def clear_baselines(self) -> None:
        """Drop all per-file baselines (the active slot changed, invalidating them)."""
        self.files = {}
