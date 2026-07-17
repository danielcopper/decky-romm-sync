"""Pure decision logic for picking a single sync action per save file.

Given (local_file, server_saves_in_slot, files_state, device_id, local_hash)
this module returns one ``SyncAction`` describing what the service should do
for that file: ``Skip``, ``Upload``, ``Download``, or ``Conflict``.

Why the design looks the way it does
------------------------------------
Newest-server-save-in-slot is picked deterministically by ``max(updated_at)``
so concurrent decisions on the same data converge on the same target.

Hash-based divergence detection requires both a recorded baseline
(``last_sync_hash``) AND a freshly computed ``local_hash``. Without a baseline
we cannot claim drift: while we are still current we adopt a baseline
(``Skip(adopt_baseline=True)``); when the server has moved past us and the
present local content is not provably identical to the server's, we route to
``Conflict`` rather than silently overwriting an unbacked local edit (#1276).

When our device is flagged ``is_current=true`` but local diverges from the
baseline, we emit ``Upload`` — the offline edit gets pushed at the decision
point rather than being deferred to a later phase. "Nobody else moved the
server forward" is the expectation (we're still flagged current), but it is a
*heuristic checked at write time*, not a guarantee: the executor POSTs with
``overwrite=false`` so RomM's 409 backstops a stale-current race (ADR-0017,
``resolve_upload_conflict`` below). The one exception decided up front here is a
local save that looks corrupt — 0-byte or implausibly shrunk versus the
recorded baseline size (a crashed emulator / full disk); rather than push the
only good copy over the cliff we route that case to ``Conflict``
(``domain/save_size.is_implausibly_shrunken``).

When ``is_current=false`` (the server moved past us) AND a present local either
diverges from the baseline or has no baseline yet and does not match the
server's content, both sides moved independently — a true ``Conflict`` that
requires a user choice via ``resolve_sync_conflict``.

Recovery: ``is_current=true`` + no local file means our last upload is
still tracked on the server but the local copy disappeared. We download to
recover the canonical content.

When our device has never touched the picked save (no entry in
``device_syncs``) and the local file is present: first, if the local content is
byte-identical to that server save — RomM stamps each save with a
``content_hash``, so ``server.content_hash == local_hash`` proves identity
without any I/O — we adopt it as the baseline (``Skip(adopt_baseline=True)``)
rather than POSTing a duplicate of bytes the server already holds (copied SD
card, restored backup, fresh reinstall). Otherwise, if we hold a baseline
(``last_sync_hash``) and local has diverged from it, both sides moved — the
chosen head is a save we never synced — so that is a ``Conflict``, the same as
branch 5. And if we hold no baseline at all but the present local is not
byte-identical to that head, its provenance is unknown and it collides with a
save we never synced — likewise the "user decides" case, a ``Conflict`` in both
mtime directions (branch-5 parity, #1276). The trailing mtime comparison against
``server.updated_at`` therefore fires only when ``local_hash`` is unknown
(``None``) or a held baseline proves local unchanged: local-newer-or-equal means
``Upload`` (POST a new save), older means ``Download``. Known fallback gap: when
a server save lacks ``content_hash`` (older / migrated saves) AND local matches a
held baseline (so the mtime path is reached), the dedup check is skipped and the
mtime path can still POST a byte-identical duplicate — no slow-path content fetch
is attempted here.

``resolve_upload_conflict`` is the second entry point: given a 409 the executor
got when POSTing an upload (RomM's write-time currency guard), it re-decides the
fallback purely from hashes — a provably-unchanged local adopts the server via
download, anything else surfaces a ``Conflict``. It exists because the decision
above is made against a ``list_saves`` snapshot that can be stale by the time
the POST lands; the 409 is the authority, this maps it to an action.

No I/O. No imports from services or adapters. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from domain.iso_time import parse_iso_to_epoch
from domain.save_size import is_implausibly_shrunken

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
    server: dict[str, Any],
    local_file: dict[str, Any] | None,
    local_hash: str | None,
    last_sync_hash: str | None,
    last_sync_local_size: int | None,
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
        if is_implausibly_shrunken(local_file.get("size"), last_sync_local_size):
            # 0-byte / truncated local (crashed emulator, full disk). Rather than
            # push the corrupt local to the server — an automatic POST would make
            # it a new slot head, and even a 409 downgrade can't recover the
            # truncated bytes — let the user decide instead (#1062).
            return Conflict(server_save=server)
        # Played offline since last sync; server unchanged by our reckoning —
        # POST the diverged local content as a new save. ``target_save_id`` only
        # echoes the head we believe we're superseding; the dispatch POSTs with
        # ``overwrite=false`` so a stale-current race 409s (ADR-0017).
        return Upload(target_save_id=server.get("id"))
    return Skip(reason="synced")


def _decide_when_not_current(
    server: dict[str, Any], local_file: dict[str, Any] | None, local_hash: str | None, last_sync_hash: str | None
) -> SyncAction:
    """Branch 5: ``our_entry`` exists but ``is_current=False`` (server moved past us)."""
    if local_file is None:
        # Server moved, nothing local to protect — adopt the server save.
        return Download(server_save=server)
    if not last_sync_hash:
        # Local present but no baseline to prove it's unchanged. If it is
        # byte-identical to this server save (RomM content_hash), adopting via
        # download is harmless and re-establishes the baseline + is_current.
        # Otherwise a present local of unknown provenance colliding with a
        # moved-past head is the "no assumptions, user decides" case (#1276) —
        # not a safe silent download.
        server_hash = server.get("content_hash")
        if server_hash and local_hash and server_hash == local_hash:
            return Download(server_save=server)
        return Conflict(server_save=server)
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
    # #1013: local content is byte-identical to this server save (RomM-provided
    # content_hash) → adopt it as the baseline instead of POSTing a duplicate.
    server_hash = server.get("content_hash")
    if server_hash and local_hash and server_hash == local_hash:
        return Skip(reason="synced", adopt_baseline=True)
    if last_sync_hash and local_hash and local_hash != last_sync_hash:
        # Both sides moved — the chosen head is a save we never synced while
        # local diverged from the baseline. Mirrors branch 5: a true Conflict.
        return Conflict(server_save=server)
    if not last_sync_hash and local_hash:
        # Branch-5 parity (#1276): a present local of unknown provenance that is
        # not byte-identical to a server head we never synced is the "user
        # decides" case, both mtime directions — not a silent mtime-based pick.
        # (The former mtime-newer -> Upload(POST) already 409'd to a conflict,
        # since we hold no device_syncs entry on the head; this only drops the
        # wasted round-trip while closing the mtime-older silent Download.)
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
    - `local_hash`: pre-computed RomM-parity content hash of local_file
      (zip-aware; a plain MD5 for a single-file save, the per-entry combined
      hash for a zip), or None when unknown. Must be computed the same way as
      the server's ``content_hash`` so the byte-identity checks against it can
      match for zip saves too.
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
    last_sync_local_size = files_state.get("last_sync_local_size")

    if our_entry and our_entry.get("is_current"):
        return _decide_when_is_current(server, local_file, local_hash, last_sync_hash, last_sync_local_size)
    if our_entry is not None:
        return _decide_when_not_current(server, local_file, local_hash, last_sync_hash)
    return _decide_when_no_entry(server, local_file, local_hash, last_sync_hash)


def resolve_upload_conflict(
    local_hash: str | None,
    last_sync_hash: str | None,
    server_content_hash: str | None = None,
) -> Literal["download", "conflict"]:
    """Decide the fallback when an upload POST is rejected by RomM's 409.

    RomM's ``add_save`` returns 409 ("slot has a newer save since your last
    sync") on an ``overwrite=false`` POST when this device is not current on the
    slot's newest save — i.e. the server head moved past what we last synced.
    Two provably-safe outcomes, else a user decision:

    - local is unchanged since our own recorded baseline
      (``local_hash == last_sync_hash``), or byte-identical to what the server
      now holds (``local_hash == server_content_hash``) → nothing of ours to
      protect; adopt the server save via ``"download"``.
    - otherwise local carries changes AND the server independently moved (which
      is exactly what the 409 proves) → genuine two-sided divergence →
      ``"conflict"`` for the user to resolve.

    Missing or empty information (``local_hash`` / ``last_sync_hash`` ``None`` or
    ``""``) never yields ``"download"`` — an empty-string hash can't read as
    "provably unchanged" (the truthiness guards mirror the kernel's
    ``not last_sync_hash`` convention) — the safe default under uncertainty is
    ``"conflict"``.
    """
    if local_hash and last_sync_hash and local_hash == last_sync_hash:
        return "download"
    if local_hash and server_content_hash and local_hash == server_content_hash:
        return "download"
    return "conflict"
