"""Argosy-style sync action computation (Phase 1 of save-sync rewrite).

Pure-domain decision logic mapped from a (local_file, server_saves_in_slot,
files_state, device_id, local_hash) tuple to a single `SyncAction` outcome.

Modeled after the official RomM clients (Argosy/Grout) with one deliberate
deviation: when our device's `device_syncs` entry says `is_current=true` but
the local file's hash diverges from `last_sync_hash`, we surface a Conflict
eagerly instead of skipping. This catches offline edits at pre-launch sync
time rather than waiting for post-session detection.

Hash-divergence guard
---------------------
The Argosy model says "if local hash differs from last_sync_hash, surface a
Conflict." We require BOTH `last_sync_hash` and `local_hash` to be set as a
precondition — when either is None we cannot meaningfully claim divergence
(no baseline, or no current hash to compare). In the `is_current=true` branch
this defaults to Skip("synced"); in the `is_current=false` branch it defaults
to Download. This avoids spurious Conflict on first-ever sync where state has
no `last_sync_hash` yet.

No I/O. No imports from services or adapters. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# ---------------------------------------------------------------------------
# SyncAction variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Skip:
    """Nothing to do. `reason` is one of:
    "synced", "deferred_unchanged", "local_newer_no_entry", "nothing_to_sync".
    """

    reason: str


@dataclass(frozen=True)
class Upload:
    """Push local to server. `target_save_id=None` means POST as new save;
    an int means PUT to that existing save id.
    """

    target_save_id: int | None


@dataclass(frozen=True)
class Download:
    """Adopt the chosen server save (raw RomM API dict)."""

    server_save: dict


@dataclass(frozen=True)
class Conflict:
    """Both sides changed. User must decide via the resolve callable."""

    server_save: dict


SyncAction = Skip | Upload | Download | Conflict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso_to_epoch(value: str) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds.

    Handles a trailing "Z" defensively (older datetime.fromisoformat versions
    reject it). Returns None on any parse failure — the caller decides how
    to interpret that.
    """
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _local_mtime_ge_server_updated_at(local_file: dict, server: dict) -> bool:
    """Return True iff local mtime is at-or-after the server save's updated_at.

    On any parse failure (missing/garbled timestamps) we conservatively return
    False so the server effectively wins — better to download a known-good
    server save than to skip based on broken metadata.
    """
    local_mtime = local_file.get("mtime")
    if not isinstance(local_mtime, int | float):
        return False
    server_epoch = _parse_iso_to_epoch(server.get("updated_at", ""))
    if server_epoch is None:
        return False
    return local_mtime >= server_epoch


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------


def compute_sync_action(
    local_file: dict | None,
    server_saves_in_slot: list[dict],
    files_state: dict,
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

    # 2. Pick newest server save by updated_at.
    server = max(server_saves_in_slot, key=lambda s: s.get("updated_at", ""))

    # 3. Defer-state check: caller previously chose "defer" on the same
    #    server save (id + updated_at). If nothing changed server-side,
    #    keep skipping. Any change → fall through and re-evaluate.
    deferred = files_state.get("deferred")
    if (
        isinstance(deferred, dict)
        and deferred.get("server_save_id") == server.get("id")
        and deferred.get("server_updated_at") == server.get("updated_at")
    ):
        return Skip(reason="deferred_unchanged")

    # 4. Find our device's device_syncs entry on the chosen server save.
    device_syncs = server.get("device_syncs") or []
    our_entry = next(
        (ds for ds in device_syncs if ds.get("device_id") == device_id),
        None,
    )
    last_sync_hash = files_state.get("last_sync_hash")

    # 5a. We are flagged as current. Argosy would skip outright; we deviate
    #     and detect local divergence eagerly when we can.
    if our_entry and our_entry.get("is_current"):
        if local_file and last_sync_hash and local_hash and local_hash != last_sync_hash:
            return Conflict(server_save=server)
        return Skip(reason="synced")

    # 5b. Server timeline moved past our last upload. If local also changed,
    #     it's a conflict; otherwise silently adopt the server save.
    if our_entry is not None:
        if local_file and last_sync_hash and local_hash and local_hash != last_sync_hash:
            return Conflict(server_save=server)
        return Download(server_save=server)

    # 5c. No entry for our device on the chosen save. Fall back to mtime
    #     comparison vs server.updated_at.
    if not local_file:
        return Download(server_save=server)
    if _local_mtime_ge_server_updated_at(local_file, server):
        return Skip(reason="local_newer_no_entry")
    return Download(server_save=server)
