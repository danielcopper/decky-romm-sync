"""Argosy-style sync action computation (Phase 1 of save-sync rewrite).

Pure-domain decision logic mapped from a (local_file, server_saves_in_slot,
files_state, device_id, local_hash) tuple to a single `SyncAction` outcome.

Modeled after the official RomM clients (Argosy/Grout) with one notable
adaptation: when our device's `device_syncs` entry says `is_current=true` but
the local file's hash diverges from `last_sync_hash`, we emit ``Upload`` with
a PUT target (the existing server save id) so the diverged local content is
pushed up to the server. Argosy's pre-launch resolver returns ``Skip`` in
this case and lets its post-session step do the upload; we collapse those
two phases by uploading at the decision point. The end state is the same as
Argosy's: the local content lands on the server.

Baseline-recovery behaviour
---------------------------
``last_sync_hash`` is the only safety net against silent overwrite of locally
edited content. When it is missing (first-ever sync, migrated state, or an
intermediate state mutation that lost the field) we cannot meaningfully claim
divergence:

- ``is_current=true`` + local exists + no baseline → ``Skip`` with
  ``adopt_baseline=True``: the service writes the current ``local_hash`` as
  the baseline so subsequent runs can detect drift. No I/O.
- ``is_current=false`` + local exists + no baseline → ``Download``: server
  has clearly moved past us and we have nothing to claim divergence with,
  so the server wins.

Recovery
--------
``is_current=true`` + no local file means our last upload disappeared from
disk while the server still tracks it for us. We download to recover.

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

    # 3. Find our device's device_syncs entry on the chosen server save.
    device_syncs = server.get("device_syncs") or []
    our_entry = next(
        (ds for ds in device_syncs if ds.get("device_id") == device_id),
        None,
    )
    last_sync_hash = files_state.get("last_sync_hash")

    # 4. Our device is flagged current on the chosen save.
    if our_entry and our_entry.get("is_current"):
        if local_file is None:
            # Recovery: server still tracks our last version, local is gone.
            return Download(server_save=server)
        if not last_sync_hash:
            # No baseline yet — adopt local_hash as the baseline so future
            # runs can detect drift. Pure state mutation, no I/O.
            return Skip(reason="synced", adopt_baseline=True)
        if local_hash and local_hash != last_sync_hash:
            # Played offline since last sync; server unchanged — push the
            # diverged local content (PUT against the existing save id).
            return Upload(target_save_id=server.get("id"))
        # Steady state.
        return Skip(reason="synced")

    # 5. Our device exists but is not flagged current — server moved past us.
    if our_entry is not None:
        if local_file is None:
            # Server moved, nothing local to protect.
            return Download(server_save=server)
        if not last_sync_hash:
            # No baseline → cannot claim drift; server wins.
            return Download(server_save=server)
        if local_hash and local_hash != last_sync_hash:
            # Both sides changed — the only true Conflict.
            return Conflict(server_save=server)
        # Server changed, local untouched → adopt.
        return Download(server_save=server)

    # 6. No entry for our device on the chosen save (never synced this save).
    if local_file is None:
        return Download(server_save=server)
    if _local_mtime_ge_server_updated_at(local_file, server):
        # POST our local as a new save in the slot.
        return Upload(target_save_id=None)
    return Download(server_save=server)
