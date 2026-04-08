"""Unified sync-action logic for save sync v2.

Extends the existing conflict detection (save_conflicts.py) with
RomM v4.7 device sync awareness (is_current flag from device_syncs).

Includes server-save matching: maps local files to their server counterparts
using tracked_save_id → filename → slot-fallback priority chain.

No I/O, no service/adapter/lib imports. Pure functions only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.save_conflicts import check_server_changes_fast, determine_action

# ---------------------------------------------------------------------------
# Server-save matching
# ---------------------------------------------------------------------------


@dataclass
class MatchedSave:
    """A local file matched to a server save."""

    local_file: dict | None  # {"filename": str, "path": str} or None for server-only
    server_save: dict | None  # server save dict or None for local-only
    filename: str  # the LOCAL filename to use (not the server's timestamp name)
    match_method: str  # "tracked_id" | "filename" | "slot_fallback" | "local_only" | "server_only"
    newer_save_in_slot: dict | None = None  # Newer save in same slot from another device


@dataclass
class MatchResult:
    """Result of matching local files to server saves."""

    matched: list[MatchedSave] = field(default_factory=list)
    matched_server_ids: set[int] = field(default_factory=set)
    new_tracked_ids: dict[str, int] = field(default_factory=dict)  # filename -> save_id to persist


def _mark_older_versions_in_slot(
    server: dict,
    server_saves: list[dict],
    matched_server_ids: set[int],
) -> None:
    """Mark all server saves in the same slot up to the matched timestamp as matched."""
    matched_ts = server.get("updated_at", "")
    matched_slot = server.get("slot")
    for ss in server_saves:
        ss_id = ss.get("id")
        if ss_id is not None and ss.get("slot") == matched_slot and ss.get("updated_at", "") <= matched_ts:
            matched_server_ids.add(ss_id)


def _find_slot_fallback(
    server_saves: list[dict],
    active_slot: str | None,
    matched_server_ids: set[int],
) -> dict | None:
    """Find the newest server save in the active slot that hasn't been matched yet."""
    candidates = [
        ss
        for ss in server_saves
        if ss.get("id") not in matched_server_ids
        and (ss.get("slot") == active_slot or (active_slot and ss.get("slot") is None))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.get("updated_at", ""))


def _apply_slot_fallback(
    fn: str,
    candidates: list[dict],
    newest: dict,
    result: MatchResult,
) -> None:
    """Record fallback match and mark all slot candidates as matched."""
    result.new_tracked_ids[fn] = newest["id"]
    for sc in candidates:
        sc_id = sc.get("id")
        if sc_id is not None:
            result.matched_server_ids.add(sc_id)


def _is_save_from_our_device(save: dict, device_id: str | None) -> bool:
    """Check if our device is current with this save (meaning we uploaded or already synced it)."""
    if not device_id:
        return False
    return any(str(ds.get("device_id")) == device_id and ds.get("is_current") for ds in save.get("device_syncs", []))


def _find_newer_in_slot(
    server: dict,
    tracked_id: int,
    server_saves: list[dict],
    result: MatchResult,
    device_id: str | None,
) -> dict | None:
    """Find the newest foreign save in the same slot as *server*, if any.

    Mutates ``result.matched_server_ids`` to mark newer candidates so they are
    not later emitted as phantom server-only entries.

    Returns the newest foreign (not from our device) save, or ``None``.
    """
    tracked_slot = server.get("slot")
    tracked_updated = server.get("updated_at", "")
    all_newer = [
        ss
        for ss in server_saves
        if ss.get("slot") == tracked_slot and ss.get("id") != tracked_id and ss.get("updated_at", "") > tracked_updated
    ]
    # Mark all newer candidates to prevent phantom server-only entries
    for c in all_newer:
        cid = c.get("id")
        if cid is not None:
            result.matched_server_ids.add(cid)
    # Only flag saves not from our own device as newer_in_slot
    foreign = [s for s in all_newer if not _is_save_from_our_device(s, device_id)]
    if foreign:
        return max(foreign, key=lambda s: s.get("updated_at", ""))
    return None


def _match_single_local_file(
    lf: dict,
    server_by_id: dict[int, dict],
    server_by_name: dict[str, dict],
    server_saves: list[dict],
    active_slot: str | None,
    file_state: dict,
    result: MatchResult,
    device_id: str | None = None,
) -> MatchedSave:
    """Match one local file to a server save using the priority chain.

    Mutates result.matched_server_ids and result.new_tracked_ids as side effects.
    """
    fn = lf["filename"]
    server: dict | None = None
    method = "local_only"

    # Priority 1: tracked_save_id
    tracked_id = file_state.get("tracked_save_id")
    newer_in_slot: dict | None = None
    if tracked_id and tracked_id in server_by_id:
        server = server_by_id[tracked_id]
        method = "tracked_id"
        newer_in_slot = _find_newer_in_slot(server, tracked_id, server_saves, result, device_id)

    # Priority 2: filename match
    if not server:
        server = server_by_name.get(fn)
        if server:
            method = "filename"

    # Priority 3: fallback to newest in active slot
    if not server and server_saves:
        newest = _find_slot_fallback(server_saves, active_slot, result.matched_server_ids)
        if newest:
            server = newest
            method = "slot_fallback"
            slot_candidates = [
                ss
                for ss in server_saves
                if ss.get("id") not in result.matched_server_ids
                and (ss.get("slot") == active_slot or (active_slot and ss.get("slot") is None))
            ]
            _apply_slot_fallback(fn, slot_candidates, newest, result)

    if server and server.get("id") is not None:
        result.matched_server_ids.add(server["id"])
        _mark_older_versions_in_slot(server, server_saves, result.matched_server_ids)

    return MatchedSave(
        local_file=lf,
        server_save=server,
        filename=fn,
        match_method=method,
        newer_save_in_slot=newer_in_slot,
    )


def _collect_server_only_saves(
    server_saves: list[dict],
    matched_server_ids: set[int],
    local_by_name: dict[str, dict],
    rom_name: str | None,
) -> list[MatchedSave]:
    """Collect server saves that have no local counterpart.

    Groups by base name, picks newest per group, and computes the local
    filename to use when downloading.
    """
    unmatched = [
        ss
        for ss in server_saves
        if ss.get("id") not in matched_server_ids and ss.get("file_name", "") not in local_by_name
    ]
    if not unmatched:
        return []

    groups: dict[str, list[dict]] = {}
    for ss in unmatched:
        base = ss.get("file_name_no_tags") or ss.get("file_name", "unknown")
        groups.setdefault(base, []).append(ss)

    server_only: list[MatchedSave] = []
    for _base, group in groups.items():
        newest = max(group, key=lambda s: s.get("updated_at", ""))
        dl_filename = (
            (rom_name + "." + newest.get("file_extension", "srm"))
            if rom_name
            else (_base + "." + newest.get("file_extension", "srm"))
        )
        server_only.append(
            MatchedSave(
                local_file=None,
                server_save=newest,
                filename=dl_filename,
                match_method="server_only",
            )
        )
        for ss in group:
            ss_id = ss.get("id")
            if ss_id is not None:
                matched_server_ids.add(ss_id)

    return server_only


def match_local_to_server_saves(
    local_files: list[dict],
    server_saves: list[dict],
    files_state: dict,
    active_slot: str | None,
    rom_name: str | None = None,
    device_id: str | None = None,
) -> MatchResult:
    """Match local save files to server saves.

    Priority per local file:
    1. tracked_save_id from state → exact server ID match
    2. Filename match → server has same filename
    3. Fallback: newest server save in active slot (recovery after state reset)

    Server-only saves (no local match) are included if they are the newest
    in their group AND not an older version of an already-matched save.

    Parameters
    ----------
    local_files:
        List of {"filename": str, "path": str} dicts from _find_save_files.
    server_saves:
        List of server save dicts from list_saves API.
    files_state:
        Per-file state dict from save_sync_state["saves"][rom_id]["files"].
    active_slot:
        The currently active slot for this game, or None.
    rom_name:
        The ROM base name (e.g. "Mario Golf - Advance Tour (USA)") for
        computing the expected local filename when downloading server-only saves.
    device_id:
        This device's ID. When provided, newer saves already current on our
        device are not flagged as newer_save_in_slot.

    Returns
    -------
    MatchResult with matched pairs and bookkeeping sets.
    """
    result = MatchResult()

    # Filter server saves to active slot (when slot is configured).
    # Saves with slot=None (v4.6 / pre-slot) are included in any active slot.
    # Treat empty string same as None (legacy/no-slot mode).
    if active_slot:
        filtered_saves = [ss for ss in server_saves if ss.get("slot") == active_slot or ss.get("slot") is None]
    else:
        # Legacy mode (no slot): only match saves without a slot assignment
        filtered_saves = [ss for ss in server_saves if not ss.get("slot")]

    # Build indexes
    server_by_id: dict[int, dict] = {}
    server_by_name: dict[str, dict] = {}
    for ss in filtered_saves:
        sid = ss.get("id")
        if sid is not None:
            server_by_id[sid] = ss
        fn = ss.get("file_name", "")
        if fn:
            server_by_name[fn] = ss

    local_by_name = {lf["filename"]: lf for lf in local_files}

    # --- Pass 1: Match each local file to a server save ---
    for lf in sorted(local_files, key=lambda x: x["filename"]):
        file_state = files_state.get(lf["filename"], {})
        matched = _match_single_local_file(
            lf, server_by_id, server_by_name, filtered_saves, active_slot, file_state, result, device_id
        )
        result.matched.append(matched)

    # --- Pass 2: Server-only saves (not matched by any local file) ---
    server_only = _collect_server_only_saves(filtered_saves, result.matched_server_ids, local_by_name, rom_name)
    result.matched.extend(server_only)

    return result


def check_server_changed_v47(device_sync_info: dict | None) -> bool | None:
    """Check server change using v4.7 device_syncs info.

    Parameters
    ----------
    device_sync_info:
        A single device sync record from the server's device_syncs array,
        or None if not available (v4.6 / no device registered).

    Returns
    -------
    bool | None
        True if server changed (is_current == False),
        False if server unchanged (is_current == True),
        None if indeterminate (no device_sync_info provided, or is_current is
        absent/None).
    """
    if device_sync_info is None:
        return None
    is_current = device_sync_info.get("is_current")
    if is_current is None:
        return None
    return not is_current


def determine_sync_action(
    local_changed: bool,
    server_save: dict | None,
    device_sync_info: dict | None = None,
    file_state: dict | None = None,
) -> str:
    """Determine the sync action combining local and server change detection.

    Parameters
    ----------
    local_changed:
        Whether the local file has changed since last sync (caller computes this).
    server_save:
        Server save metadata dict, or None if no server save exists.
    device_sync_info:
        v4.7 device sync record (has "is_current" key), or None for v4.6 fallback.
    file_state:
        Per-file sync state dict (used for v4.6 fallback via check_server_changes_fast).
        Only needed when device_sync_info is None.

    Returns
    -------
    str
        One of "skip", "upload", "download", "conflict", "initial_upload",
        "initial_download".
    """
    # No server save at all
    if server_save is None:
        return "initial_upload" if local_changed else "skip"

    # Server save exists — determine whether server has changed
    server_changed: bool

    # Priority 1: v4.7 device_sync_info
    v47_result = check_server_changed_v47(device_sync_info)
    if v47_result is not None:
        server_changed = v47_result
    else:
        # Priority 2: v4.6 fast-path using file_state
        if file_state is not None:
            v46_result = check_server_changes_fast(file_state, server_save)
            server_changed = v46_result if v46_result is not None else True
        else:
            # Priority 3: no info at all — safe default: assume server changed
            server_changed = True

    return determine_action(local_changed, server_changed)
