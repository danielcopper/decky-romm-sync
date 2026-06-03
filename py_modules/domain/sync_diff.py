"""Sync diffs — pure delta computations between current state and last-synced state.

Anything that compares "what the next sync should produce" against "what we
recorded last sync" lives here: per-ROM bucketing (new / changed / unchanged /
stale), enabled-collection add/remove diffs, and the platform-collection
membership predicate that drives the platform-collection diff.

State reads (registry, last_synced_*), setting reads, network fetches, and
Steam interaction stay in LibraryService; this module receives the relevant
slices as primitive parameters and returns primitives or NamedTuple results.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class ClassificationResult(NamedTuple):
    new: list[dict[str, Any]]
    changed: list[dict[str, Any]]
    unchanged_ids: list[int]
    stale: list[int]
    disabled_count: int


def classify_roms(
    shortcuts_data: list[dict[str, Any]],
    registry: dict[str, Any],
    fetched_platform_names: set[str],
) -> ClassificationResult:
    """Bucket fetched ROMs against the saved shortcut registry.

    Returns the ROMs split into new (not in registry), changed (registry
    entry exists but name/platform_name/platform_slug/fs_name differs),
    unchanged_ids (registry matches exactly), stale (in registry but not in
    the current fetch), and the count of stale ROMs whose stored platform
    no longer appears in fetched_platform_names.

    Changed ROMs are returned as fresh dicts with an added ``existing_app_id``
    key — the caller's shortcuts_data is not mutated.
    """
    new: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged_ids: list[int] = []

    for sd in shortcuts_data:
        reg = registry.get(str(sd["rom_id"]))
        if not reg or not reg.get("app_id"):
            new.append(sd)
        elif (
            reg.get("name") != sd["name"]
            or reg.get("platform_name") != sd.get("platform_name")
            or reg.get("platform_slug") != sd.get("platform_slug")
            or reg.get("fs_name") != sd.get("fs_name", "")
        ):
            changed.append({**sd, "existing_app_id": reg["app_id"]})
        else:
            unchanged_ids.append(sd["rom_id"])

    current_ids = {sd["rom_id"] for sd in shortcuts_data}
    stale = [int(rid) for rid in registry if int(rid) not in current_ids]
    disabled_count = sum(
        1 for rid in stale if registry.get(str(rid), {}).get("platform_name") not in fetched_platform_names
    )
    return ClassificationResult(new, changed, unchanged_ids, stale, disabled_count)


def compute_collection_diff(
    collection_memberships: dict[str, list[int]],
    last_synced_collections: list[str],
) -> dict[str, Any]:
    """Diff enabled collections (by name) against the last-synced set.

    Returns ``{"has_changes": bool, "added": [...], "removed": [...]}``.
    ``has_changes`` is True if there are any added/removed collections, or
    if there are any current collections at all (covers first-sync case).
    """
    current = set(collection_memberships.keys())
    previous = set(last_synced_collections)
    added = sorted(current - previous)
    removed = sorted(previous - current)
    return {
        "has_changes": bool(added or removed or current),
        "added": added,
        "removed": removed,
    }


def should_include_in_platform_collection(
    rom_id: int,
    platform_rom_ids: set[int] | None,
    create_platform_groups: bool,
) -> bool:
    """Predicate: should this ROM appear in platform-grouped collections?

    If ``create_platform_groups`` is True, every ROM qualifies. Otherwise:
    platform_rom_ids=None means no tracking (legacy sync) so include all;
    platform_rom_ids=set() means no platforms enabled so exclude all;
    otherwise membership in platform_rom_ids decides.
    """
    if create_platform_groups:
        return True
    if platform_rom_ids is None:
        return True
    return rom_id in platform_rom_ids


def compute_platform_collection_diff(
    shortcuts_data: list[dict[str, Any]],
    platform_rom_ids: set[int] | None,
    last_synced_platforms: list[str],
    create_platform_groups: bool,
) -> dict[str, Any]:
    """Diff future platform-group collections against last-synced platforms.

    Returns ``{"has_changes": bool, "added_count": int, "removed_count": int}``.
    Uses ``should_include_in_platform_collection`` to decide which ROMs
    qualify under the current ``create_platform_groups`` setting.
    """
    future_platforms: set[str] = set()
    for sd in shortcuts_data:
        rid = sd["rom_id"]
        if should_include_in_platform_collection(rid, platform_rom_ids, create_platform_groups):
            pname = sd.get("platform_name", "")
            if pname:
                future_platforms.add(pname)

    current_platforms = set(last_synced_platforms)
    added = sorted(future_platforms - current_platforms)
    removed = sorted(current_platforms - future_platforms)
    return {
        "has_changes": bool(added or removed),
        "added_count": len(added),
        "removed_count": len(removed),
    }
