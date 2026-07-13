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

from domain.sibling_resolution import AUTO_REGION, canonical_group_name, resolve_group_representative

# Marker key a rebind entry carries so the per-unit commit moves the DB binding
# from the vanished bound sibling (the entry's ``rom_id``, kept so the frontend
# reuses its existing shortcut) onto the surviving representative. Absent on
# normal / grandfathered entries. See :func:`collapse_sibling_groups`.
BIND_ROM_ID_KEY = "bind_rom_id"


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
    entry exists but a *persisted* identity field — name, platform_slug, or
    fs_name — differs, OR the target ``launch_options`` differs from the
    recorded ``applied_launch_options``), unchanged_ids (registry matches
    exactly), stale (in registry but not in the current fetch), and the count of
    stale ROMs whose stored platform no longer appears in fetched_platform_names.

    ``applied_launch_options`` is the launch command last written to the ROM's
    Steam shortcut (recorded by the five writer sites, #1383). Comparing the
    built target ``launch_options`` against it is what lets the delta-restricted
    apply skip a content-correct shortcut rather than re-touching it: an identity
    match with a launch-options match is genuinely unchanged, while an
    install/uninstall (or core/disc pin change) that leaves identity untouched
    still flips the item to "changed". A NULL recorded value (``None`` — a
    pre-migration-015 row, or a freshly created row not yet recorded) never
    matches a target string, so such a row is always "changed" and re-applied
    once; the writer sites then record the value and the next sync skips it. No
    skip is ever taken on unknown recorded state — no data is invented.

    ``platform_name`` is deliberately excluded from the changed comparison:
    it is a derived display field, never persisted on the ``roms`` row. The
    registry side resolves it from the enabled-platforms-only slug→name map
    (falling back to the bare slug for a disabled platform), while the fetch
    side gets RomM's full display name, so a divergence between the two can
    never be healed by an apply (the upsert writes only
    platform_slug/name/fs_name) — comparing it produced a permanent phantom
    "changed" delta (#1292). Platform display renames are diffed separately by
    ``compute_platform_collection_diff``. ``platform_name`` is still read here
    for the disabled-platform stale count, which intentionally keys on the
    live-resolved display name.

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
        # Compare the persisted identity fields plus the recorded applied
        # launch command. platform_name is a derived display field (never on the
        # roms row) — comparing it produced a permanent phantom "changed" delta
        # (#1292). applied_launch_options catches an install/uninstall or pin
        # change that leaves identity untouched; a NULL recorded value never
        # matches, so an unknown-state row is always "changed" (#1383). See the
        # docstring.
        elif (
            reg.get("name") != sd["name"]
            or reg.get("platform_slug") != sd.get("platform_slug")
            or reg.get("fs_name") != sd.get("fs_name", "")
            or reg.get("applied_launch_options") != sd.get("launch_options", "")
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


def collapse_sibling_groups(
    shortcuts_data: list[dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    installed_rom_ids: set[int],
    *,
    complete_group_view: bool,
    preferred_region: str = AUTO_REGION,
) -> list[dict[str, Any]]:
    """Collapse per-ROM shortcut entries to one Steam shortcut per sibling group.

    A sibling group (same ``sibling_group_key``) is one game = at most one NEW
    Steam shortcut (ADR-0021 §2). *shortcuts_data* is the built entry for every
    fetched ROM; *registry* is the bound ``roms`` rows keyed by ``str(rom_id)``
    (each carrying ``app_id``, ``name``, ``fs_name``, ``platform_slug`` and
    ``sibling_group_key``); *installed_rom_ids* are the ROMs currently on disk.

    *complete_group_view* asserts whether *shortcuts_data* holds the WHOLE
    membership of every group a bound row belongs to — the load-bearing safety
    input. A sibling group is per-platform, so a **platform** unit fetch and the
    whole-library **preview** union are complete views; a **collection** unit is
    a PARTIAL view (it spans platforms and may fetch just one unbound sibling of
    a group whose other members — including the bound one — were never in this
    fetch). Only a complete view may conclude "bound sibling absent from members
    ⇒ vanished from the server"; inferring that from a partial view would rebind
    a live installed game's shortcut onto an uninstalled sibling (#1296 CRITICAL).

    Returns the subset of entries to actually emit as shortcuts, one lane per
    group:

    * **Grandfathered** — the group has ≥1 bound sibling still fetched: every
      surviving bound sibling keeps its own entry (ADR-0021 §5). No new shortcut
      is minted; an unbound fetched sibling becomes a tracked row with no
      shortcut, and (complete view only) a vanished bound sibling is torn down by
      the stale path.
    * **Rebind** (complete view only) — every bound sibling vanished from the
      server but the group still has fetched members: one synthetic entry (see
      :func:`_rebind_entry`) keeps the vanished sibling's shortcut and moves its
      binding to the surviving representative.
    * **New** — no binding anywhere in the group: emit the single representative
      chosen by :func:`resolve_group_representative`, but with its display
      ``name`` replaced by :func:`canonical_group_name` — the region-preferred
      member's name mints the sticky Steam shortcut (ADR-0021 §2/§3), while the
      representative's ``rom_id`` / launch bake stay the bind target. This is the
      only lane that renames: an already-bound group (grandfathered / rebind)
      carries its persisted name verbatim, so a live shortcut is never renamed.
    * **Grandfathered untouched** (partial view only) — the group holds a binding
      that is not in THIS fetch: emit nothing. Absence from a partial view is not
      absence from the server, so the binding is left alone (no rebind, no second
      shortcut). The group's real representative rides its own platform unit in
      the same run; the unbound fetched members still persist via
      ``pending_all_roms`` and the reporter's collection group-fallback places the
      group's shortcut in the Steam collection.

    Bound rows are grouped by their FETCHED sibling key when the row is in this
    fetch (so a legacy row whose stored key is still NULL is placed in its real
    group and grandfathered rather than churned), falling back to the stored key
    for a vanished bound row.

    *preferred_region* re-heads the region ranking that both the representative
    and the canonical name fall back to (``"auto"`` = the build-time default
    order); it is read from settings by the caller and must be the same value at
    every collapse call site within one sync run.
    """
    # Keys are ``str | None``: a built entry always carries a real key, but a
    # legacy bound row may still hold NULL (its own solo/unmatched group).
    by_group: dict[str | None, list[dict[str, Any]]] = {}
    for sd in shortcuts_data:
        by_group.setdefault(sd.get("sibling_group_key"), []).append(sd)

    fetched_key_by_id: dict[Any, str | None] = {sd["rom_id"]: sd.get("sibling_group_key") for sd in shortcuts_data}
    bound_by_group: dict[str | None, list[tuple[int, dict[str, Any]]]] = {}
    bound_rom_ids: set[int] = set()
    for rid_str, reg in registry.items():
        if not reg.get("app_id"):
            continue
        rid = int(rid_str)
        bound_rom_ids.add(rid)
        group_key = fetched_key_by_id.get(rid, reg.get("sibling_group_key"))
        bound_by_group.setdefault(group_key, []).append((rid, reg))

    emitted: list[dict[str, Any]] = []
    for group_key, members in by_group.items():
        fetched_ids = {m["rom_id"] for m in members}
        bound_here = bound_by_group.get(group_key, [])
        surviving_ids = {rid for rid, _ in bound_here if rid in fetched_ids}
        if surviving_ids:
            # ≥1 bound sibling still fetched → grandfather every surviving one.
            emitted.extend(m for m in members if m["rom_id"] in surviving_ids)
        elif not bound_here:
            # No binding anywhere in the group → mint the single representative,
            # named after the region-preferred (canonical) member. The bind
            # target stays the representative's rom_id + launch bake; only the
            # sticky Steam name follows the pure region ranking (ADR-0021 §2/§3).
            # Copy so the original member dict (also staged in pending_all_roms
            # for the per-sibling identity upsert) keeps its own real name.
            rep_id = resolve_group_representative(members, installed_rom_ids, bound_rom_ids, preferred_region)
            emitted.append({**_member_by_id(members, rep_id), "name": canonical_group_name(members, preferred_region)})
        elif complete_group_view:
            # Complete view: every bound sibling truly vanished from the server →
            # rebind the shortcut onto the surviving representative (ADR-0021 §2,
            # never remove). The rebind entry keeps the vanished sibling's
            # persisted name (sticky) — canonical naming is mint-only.
            rep = _member_by_id(
                members, resolve_group_representative(members, installed_rom_ids, bound_rom_ids, preferred_region)
            )
            kept_rom_id, kept_reg = min(bound_here, key=lambda item: item[0])
            emitted.append(_rebind_entry(rep, kept_rom_id, kept_reg))
        # else: partial view (collection unit) — the group holds a binding not in
        # THIS fetch, so its absence here is not absence from the server.
        # Grandfather it untouched (no rebind, no new shortcut); its real
        # representative is owned by the group's platform unit in the same run.
    return emitted


def _member_by_id(members: list[dict[str, Any]], rom_id: int) -> dict[str, Any]:
    """Return the group member whose ``rom_id`` matches (the resolver picks from *members*)."""
    return next(m for m in members if m["rom_id"] == rom_id)


def _rebind_entry(rep: dict[str, Any], kept_rom_id: int, kept_reg: dict[str, Any]) -> dict[str, Any]:
    """Synthesize the emitted entry for a rebinding group (ADR-0021 §2).

    Keyed by the vanished bound sibling (*kept_rom_id*) so the frontend REUSES
    that sibling's existing Steam shortcut (its appId is already in the
    rom_id→appId map) and the preview diff reads the group as unchanged. The
    entry carries the representative's launch bake (its installed path, or the
    empty placeholder when the representative is uninstalled) plus a
    ``bind_rom_id`` marker so the per-unit commit moves the DB binding onto the
    representative — the shortcut's appId, artwork, collections and playtime all
    survive; only the active version changes. Identity fields stay the vanished
    sibling's (sticky name — never rename automatically, which would change the
    appId).
    """
    return {
        **rep,
        "rom_id": kept_rom_id,
        "name": kept_reg.get("name", rep.get("name", "")),
        "fs_name": kept_reg.get("fs_name", ""),
        "platform_slug": kept_reg.get("platform_slug", rep.get("platform_slug", "")),
        BIND_ROM_ID_KEY: rep["rom_id"],
    }


def select_stale_removals(
    candidate_stale: list[tuple[int, int]],
    synced_app_ids: set[int],
) -> list[tuple[int, int]]:
    """Stale ``(rom_id, app_id)`` removals minus any app_id re-bound this run.

    Safety invariant: never remove a Steam shortcut whose appId was bound by a
    ROM synced this run. A new server-issued ``rom_id`` can reuse an old appId
    (the appId is ``CRC32(exe + name)``, unchanged across a server switch /
    re-import — #1036). The old colliding ``rom_id`` then looks stale, but its
    still-live appId now belongs to the freshly-synced row, so emitting it for
    removal would wipe the shortcut the run just created/updated.
    """
    return [(rid, aid) for rid, aid in candidate_stale if aid not in synced_app_ids]


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
