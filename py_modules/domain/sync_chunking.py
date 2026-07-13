"""Per-unit apply chunking — split a unit's emitted shortcuts into commit chunks.

A platform/collection apply unit can emit thousands of shortcuts; sending them
all in one ``sync_apply_unit`` round-trip means a mid-unit CEF crash loses the
whole unit. This module slices a unit's emitted entries into bounded chunks the
orchestrator emits and commits one at a time, so a crash only forfeits the chunk
in flight — every already-committed chunk survives. Pure compute, no I/O, stdlib
only (``domain`` may not import adapters/services/_vendor).

A chunk carries the emitted entries the frontend applies plus the ``roms`` rows
the backend commits for those entries' sibling groups, so a chunk is a
self-contained commit unit: apply its shortcuts, ack, persist its rows.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from domain.sync_diff import BIND_ROM_ID_KEY

# Keys an emitted entry carries for the backend's own bookkeeping but which the
# frontend never reads, so they are stripped from the wire payload:
#   - ``cover_path``: the staged per-ROM cache cover path. The commit's grid write
#     reads it from the box's ``pending_sync`` (kept intact); the frontend fetches
#     a created shortcut's cover through ``get_artwork_base64(rom_id)`` instead, so
#     the path never needs to cross the wire.
#   - ``bind_rom_id``: a rebind entry's binding target. The commit resolves it from
#     ``pending_sync``; the frontend reuses the shortcut by the entry's own
#     ``rom_id`` (a rebind lands on the update path) and applies no cover there, so
#     the target is irrelevant to the frontend.
_WIRE_STRIPPED_KEYS = frozenset({"cover_path", BIND_ROM_ID_KEY})


def wire_shortcuts(emitted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project a chunk's emitted entries to the frontend wire shape.

    Returns a fresh list of shallow-copied entries with the backend-internal keys
    in :data:`_WIRE_STRIPPED_KEYS` removed, so a ``sync_apply_unit`` frame carries
    only what the frontend applies. The source entries are left untouched — they
    stay in the box's ``pending_sync`` / ``pending_all_roms`` state where the
    per-chunk commit reads the stripped keys.
    """
    return [{k: v for k, v in entry.items() if k not in _WIRE_STRIPPED_KEYS} for entry in emitted]


class UnitChunk(NamedTuple):
    """One commit chunk of a unit's apply.

    ``emitted`` is the slice of the unit's emitted shortcut entries the frontend
    applies for this chunk; ``offset`` is the number of emitted entries in all
    prior chunks (the running count the frontend renders unit-wide progress
    against); ``rom_ids`` is every fetched ROM whose sibling group has an emitted
    entry in this chunk — the rows the backend commits when this chunk is acked.
    """

    emitted: list[dict[str, Any]]
    offset: int
    rom_ids: list[int]


def build_unit_chunks(
    emitted: list[dict[str, Any]],
    shortcuts_data: list[dict[str, Any]],
    chunk_size: int,
) -> list[UnitChunk]:
    """Split a unit's *emitted* entries into commit chunks of ~*chunk_size*.

    *emitted* is the collapsed one-shortcut-per-sibling-group list (group-
    clustered — a group's entries are adjacent, see
    :func:`domain.sync_diff.collapse_sibling_groups`); *shortcuts_data* is the
    full pre-collapse built entry for every fetched ROM. Both carry a
    ``sibling_group_key`` (a real key on a built entry, possibly ``None`` on a
    legacy row) and a ``rom_id``.

    Chunks greedy-fill to *chunk_size* emitted entries but cut **only** at
    sibling-group boundaries: a chunk overflows past *chunk_size* to finish an
    adjacent run of entries sharing one non-``None`` key, so a multi-version
    game's entries never straddle two chunks (an entry with a ``None`` key is a
    singleton, never merged with its neighbours).

    Each chunk's ``rom_ids`` are every *shortcuts_data* ROM whose group has an
    emitted entry in that chunk (matched by ``sibling_group_key``; a keyless
    emitted entry matches by its binding target — ``bind_rom_id`` when present,
    else its own ``rom_id``), so the chunk commits exactly the rows its shortcuts
    bind or grandfather. Groups present in *shortcuts_data*
    with no emitted entry anywhere — plus any unmatched leftover ROMs — ride
    chunk 0's ``rom_ids`` (they need no frontend work; committing them with the
    first chunk keeps the row partition exact). An empty *emitted* yields exactly
    one empty chunk carrying every ROM id, preserving the empty-unit round-trip
    (the event still fires; the empty ack commits the unbound rows).

    Invariants: concatenating every chunk's ``emitted`` reproduces *emitted* in
    order; the union of every chunk's ``rom_ids`` is exactly the set of
    *shortcuts_data* ROM ids with no duplicate across chunks; and every emitted
    entry's binding target (its ``bind_rom_id`` when present, else its ``rom_id``)
    lands in its own chunk's ``rom_ids``.
    """
    all_rom_ids = [sd["rom_id"] for sd in shortcuts_data]
    if not emitted:
        return [UnitChunk(emitted=[], offset=0, rom_ids=all_rom_ids)]

    chunk_emitted = _partition_emitted(emitted, chunk_size)
    key_to_chunk, romid_to_chunk = _index_emitted_groups(chunk_emitted)

    chunk_rom_ids: list[list[int]] = [[] for _ in chunk_emitted]
    leftover: list[int] = []
    for sd in shortcuts_data:
        rid = sd["rom_id"]
        key = sd.get("sibling_group_key")
        if key is not None and key in key_to_chunk:
            chunk_rom_ids[key_to_chunk[key]].append(rid)
        elif rid in romid_to_chunk:
            chunk_rom_ids[romid_to_chunk[rid]].append(rid)
        else:
            leftover.append(rid)
    # Groups with no emitted entry (partial-view grandfather-untouched) + any
    # unmatched leftover commit with the first chunk.
    chunk_rom_ids[0].extend(leftover)

    chunks: list[UnitChunk] = []
    offset = 0
    for entries, rom_ids in zip(chunk_emitted, chunk_rom_ids, strict=True):
        chunks.append(UnitChunk(emitted=entries, offset=offset, rom_ids=rom_ids))
        offset += len(entries)
    return chunks


def _partition_emitted(emitted: list[dict[str, Any]], chunk_size: int) -> list[list[dict[str, Any]]]:
    """Group-clustered greedy split of *emitted* into chunks, cutting only at group runs.

    A new chunk begins only once the current one has reached *chunk_size* entries,
    so a group run added while the current chunk is still under the limit finishes
    inside it (the deliberate overflow that keeps a group whole).
    """
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for run in _group_runs(emitted):
        if current and len(current) >= chunk_size:
            chunks.append(current)
            current = []
        current.extend(run)
    if current:
        chunks.append(current)
    return chunks


def _group_runs(emitted: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Maximal runs of adjacent entries sharing one non-``None`` group key.

    A ``None``-key entry is always its own run (a singleton never merges), so a
    cut is legal before or after it.
    """
    runs: list[list[dict[str, Any]]] = []
    for entry in emitted:
        key = entry.get("sibling_group_key")
        if runs and key is not None and key == runs[-1][0].get("sibling_group_key"):
            runs[-1].append(entry)
        else:
            runs.append([entry])
    return runs


def _index_emitted_groups(
    chunk_emitted: list[list[dict[str, Any]]],
) -> tuple[dict[str, int], dict[int, int]]:
    """Map each emitted group to the chunk that owns it.

    Returns ``(key_to_chunk, romid_to_chunk)``: non-``None`` keys route by key,
    keyless entries route by their **binding target** — ``bind_rom_id`` when
    present (a rebind entry: its own ``rom_id`` is the vanished sibling, absent
    from ``shortcuts_data``, while the target is the surviving representative
    whose row must commit in this chunk), else their own ``rom_id`` (a plain
    keyless entry). Routing a keyless rebind by its own ``rom_id`` would strand
    the representative's row in chunk 0's leftover while the entry sits in a later
    chunk, so the binding is never persisted and the rebind re-fires every sync.
    ``setdefault`` keeps the first chunk to carry a key as its owner, so the
    group-clustered contract yields one chunk per group.
    """
    key_to_chunk: dict[str, int] = {}
    romid_to_chunk: dict[int, int] = {}
    for index, entries in enumerate(chunk_emitted):
        for entry in entries:
            key = entry.get("sibling_group_key")
            if key is None:
                romid_to_chunk.setdefault(int(entry.get(BIND_ROM_ID_KEY, entry["rom_id"])), index)
            else:
                key_to_chunk.setdefault(key, index)
    return key_to_chunk, romid_to_chunk
