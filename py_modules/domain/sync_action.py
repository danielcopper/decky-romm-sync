"""Pure decision logic for picking a single sync action per save file.

Given (local_file, server_saves_in_slot, files_state, device_id, local_hash)
this module returns one ``SyncAction`` describing what the service should do
for that file: ``Skip``, ``Upload``, ``Download``, or ``Conflict``.

Why the design looks the way it does
------------------------------------
Newest-server-save-in-slot is picked deterministically by ``max(updated_at)``
so concurrent decisions on the same data converge on the same target.

Hash-based divergence detection requires both a recorded baseline
(``last_sync_hash``) AND a freshly computed ``local_hash``. Without the
baseline we cannot meaningfully claim drift, so the decision falls back to
either ``Skip(adopt_baseline=True)`` (so the next run has a baseline) or
``Download`` (server wins) depending on ``is_current``.

When our device is flagged ``is_current=true`` but local diverges from the
baseline, we emit ``Upload`` with a PUT target — the offline edit gets
pushed at the decision point rather than being deferred to a later phase.
No user prompt is needed because nobody else can have moved the server
forward (we're still flagged current).

When ``is_current=false`` AND local diverges, both sides moved
independently — that is the only true ``Conflict`` and it requires a user
choice via ``resolve_sync_conflict``.

Recovery: ``is_current=true`` + no local file means our last upload is
still tracked on the server but the local copy disappeared. We download to
recover the canonical content.

When our device has never touched the picked save (no entry in
``device_syncs``) and the local file is present: if we hold a baseline
(``last_sync_hash``) and local has diverged from it, both sides moved — the
chosen head is a save we never synced — so that is a ``Conflict``, the same as
branch 5. Otherwise we fall back to comparing local mtime against
``server.updated_at``: local-newer-or-equal means ``Upload`` (POST a new save),
older means ``Download``.

No I/O. No imports from services or adapters. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.iso_time import parse_iso_to_epoch

# ---------------------------------------------------------------------------
# SyncAction variants
# ---------------------------------------------------------------------------


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
    """Push local to server. ``target_save_id=None`` means POST as new save;
    an int means PUT to that existing save id.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_mtime_ge_server_updated_at(local_file: dict[str, Any], server: dict[str, Any]) -> bool:
    """Return True iff local mtime is at-or-after the server save's updated_at.

    On any parse failure (missing/garbled timestamps) we conservatively return
    False so the server effectively wins — better to download a known-good
    server save than to skip based on broken metadata.
    """
    local_mtime = local_file.get("mtime")
    if not isinstance(local_mtime, int | float):
        return False
    server_epoch = parse_iso_to_epoch(server.get("updated_at", ""))
    if server_epoch is None:
        return False
    return local_mtime >= server_epoch


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------


def _decide_when_is_current(
    server: dict[str, Any], local_file: dict[str, Any] | None, local_hash: str | None, last_sync_hash: str | None
) -> SyncAction:
    """Branch 4: ``our_entry.is_current=True`` on the chosen save."""
    if local_file is None:
        # Recovery: server still tracks our last version, local is gone.
        return Download(server_save=server)
    if not last_sync_hash:
        # No baseline yet — adopt local_hash so future runs can detect drift.
        # Pure state mutation, no I/O.
        return Skip(reason="synced", adopt_baseline=True)
    if local_hash and local_hash != last_sync_hash:
        # Played offline since last sync; server unchanged — PUT the diverged
        # local content against the existing save id.
        return Upload(target_save_id=server.get("id"))
    return Skip(reason="synced")


def _decide_when_not_current(
    server: dict[str, Any], local_file: dict[str, Any] | None, local_hash: str | None, last_sync_hash: str | None
) -> SyncAction:
    """Branch 5: ``our_entry`` exists but ``is_current=False`` (server moved past us)."""
    if local_file is None or not last_sync_hash:
        # Server moved, nothing local to protect (or no baseline to claim drift).
        return Download(server_save=server)
    if local_hash and local_hash != last_sync_hash:
        # Both sides changed — the only true Conflict.
        return Conflict(server_save=server)
    return Download(server_save=server)


def _decide_when_no_entry(
    server: dict[str, Any], local_file: dict[str, Any] | None, local_hash: str | None, last_sync_hash: str | None
) -> SyncAction:
    """Branch 6: no ``device_syncs`` entry for our device on the chosen save."""
    if local_file is None:
        return Download(server_save=server)
    if last_sync_hash and local_hash and local_hash != last_sync_hash:
        # Both sides moved — the chosen head is a save we never synced while
        # local diverged from the baseline. Mirrors branch 5: a true Conflict.
        return Conflict(server_save=server)
    if _local_mtime_ge_server_updated_at(local_file, server):
        # POST our local as a new save in the slot.
        return Upload(target_save_id=None)
    return Download(server_save=server)


def compute_sync_action(
    local_file: dict[str, Any] | None,
    server_saves_in_slot: list[dict[str, Any]],
    files_state: dict[str, Any],
    device_id: str,
    local_hash: str | None,
) -> SyncAction:
    """Compute the sync action for a single (rom, filename, slot) triple.

    Inputs are raw shapes:
    - `local_file`: {"filename", "path", "size", "mtime"} or None
    - `server_saves_in_slot`: list of RomM API server-save dicts, already
      filtered by the caller to the relevant slot
    - `files_state`: the per-filename slice of saved sync state (may be empty)
    - `device_id`: this device's id (string)
    - `local_hash`: pre-computed MD5 of local_file, or None when unknown
    """
    # 1. No server saves in slot.
    if not server_saves_in_slot:
        if local_file:
            return Upload(target_save_id=None)
        return Skip(reason="nothing_to_sync")

    # 2. Pick newest server save by updated_at (epoch-keyed; unparseable
    # timestamps sort to the bottom so they can't beat a parseable one).
    server = max(
        server_saves_in_slot,
        key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0,
    )

    # 3. Find our device's entry on the chosen save and branch on it.
    device_syncs = server.get("device_syncs") or []
    our_entry = next((ds for ds in device_syncs if ds.get("device_id") == device_id), None)
    last_sync_hash = files_state.get("last_sync_hash")

    if our_entry and our_entry.get("is_current"):
        return _decide_when_is_current(server, local_file, local_hash, last_sync_hash)
    if our_entry is not None:
        return _decide_when_not_current(server, local_file, local_hash, last_sync_hash)
    return _decide_when_no_entry(server, local_file, local_hash, last_sync_hash)
