"""Deterministic sibling-group representative resolution + canonical naming (ADR-0021 §3).

A sibling group is one game with several released dumps (region / language /
revision variants). Two questions are answered here, both a pure, shuffle-stable
compute over the group's fetched members:

* **Which version does the group bind to** — :func:`resolve_group_representative`.
  The resolution chain::

      installed sibling > existing binding > RomM default (is_main_sibling) >
      region priority > alphabetical fs_name_no_ext > rom_id

  The first three are membership *filters*; the last three are the total order
  applied to whatever survived the highest non-empty filter. The alphabetical
  leg is exactly RomM's own grouped-view fallback, so an ungroomed library with
  no region signal resolves identically to the RomM web UI.

* **What the group's Steam shortcut is named** — :func:`canonical_group_name`.
  The name follows the *pure* order (region priority > alphabetical > rom_id)
  over ALL members, ignoring the installed/binding/default filters. This decides
  the mint-time shortcut name only; a shortcut's name is sticky forever after
  (ADR-0021 §2), so this is never re-applied to an already-bound group.

Region priority ranks a version by its best region against a build-time default
(:data:`DEFAULT_REGION_PRIORITY`), which the user may re-head with a single
``preferred_region`` choice. The preference enters as an explicit parameter —
reading it from settings is the service layer's job; this module stays pure,
stdlib only, with no I/O and no service/adapter imports.
"""

from __future__ import annotations

from typing import Any

# Build-time region ranking: the order a group's representative and canonical
# name fall back to once no installed / binding / default leg decides. World >
# USA > Europe > Japan (RomM's full-word region vocabulary) — World first for
# explicit multi-region international releases; USA before Europe to match the
# 1G1R community convention and avoid PAL-50Hz dumps being the silent default on
# older consoles. Every other named region ranks after these, alphabetically
# among themselves, and a version with no region at all ranks last. This is a
# fixed constant, NOT a language/system detection. A single ``preferred_region``
# override lifts one region to the very top — the order below then continues
# behind it.
DEFAULT_REGION_PRIORITY: tuple[str, ...] = ("World", "USA", "Europe", "Japan")
_DEFAULT_REGION_INDEX: dict[str, int] = {name.casefold(): i for i, name in enumerate(DEFAULT_REGION_PRIORITY)}

# Sentinel the ``preferred_region`` setting holds when the user expressed no
# preference — the ranking is then the pure build-time order.
AUTO_REGION = "auto"

# Region-rank buckets (lower wins), the first element of a region's sort key:
# 0 = the user's preferred region, 1 = a build-time default region, 2 = any
# other named region, 3 = the version carries no region.
_BUCKET_PREFERRED = 0
_BUCKET_DEFAULT = 1
_BUCKET_OTHER = 2
_BUCKET_NONE = 3


def _single_region_rank(region: str, preferred_region: str) -> tuple[int, int, str]:
    """Priority key for ONE region string (lower wins).

    The user's ``preferred_region`` (when set) ranks top; otherwise a region in
    the build-time default order ranks by that order; any other named region
    ranks after the defaults, alphabetically (case-folded). Case-folding both
    sides makes the match and the alphabetical sort collation-stable.
    """
    folded = region.casefold()
    if preferred_region != AUTO_REGION and folded == preferred_region.casefold():
        return (_BUCKET_PREFERRED, 0, "")
    default_idx = _DEFAULT_REGION_INDEX.get(folded)
    if default_idx is not None:
        return (_BUCKET_DEFAULT, default_idx, "")
    return (_BUCKET_OTHER, 0, folded)


def _best_region_rank(member: dict[str, Any], preferred_region: str) -> tuple[int, int, str]:
    """Rank a member by its BEST (lowest-ranked) region; no regions ⇒ ranks last."""
    regions = member.get("regions") or []
    if not regions:
        return (_BUCKET_NONE, 0, "")
    return min(_single_region_rank(str(r), preferred_region) for r in regions)


def _rank_key(member: dict[str, Any], preferred_region: str) -> tuple[tuple[int, int, str], str, int]:
    """Total pure order over a group's members: region priority > alphabetical > rom_id.

    ``fs_name_no_ext`` is RomM's own alphabetical fallback key (case-folded to
    match MariaDB's default collation — RomM parity); ``rom_id`` is the final
    disambiguator so two dumps with an identical filename stem still order
    deterministically, independent of fetch order.
    """
    return (
        _best_region_rank(member, preferred_region),
        str(member.get("fs_name_no_ext") or "").lower(),
        int(member["rom_id"]),
    )


def resolve_group_representative(
    members: list[dict[str, Any]],
    installed_rom_ids: set[int],
    bound_rom_ids: set[int],
    preferred_region: str = AUTO_REGION,
) -> int:
    """Return the representative ``rom_id`` for one sibling group's *members*.

    *members* are the group's fetched ROM dicts (each carrying ``rom_id``,
    ``is_main_sibling``, ``regions`` and ``fs_name_no_ext``). The resolution
    chain (ADR-0021 §3): the first non-empty *filter* leg wins, and inside that
    leg :func:`_rank_key` (region priority > alphabetical > rom_id) decides —

    1. an **installed** sibling (``rom_id`` in *installed_rom_ids*),
    2. else a sibling with an **existing binding** (``rom_id`` in *bound_rom_ids*),
    3. else RomM's per-user **default** (``is_main_sibling`` truthy),
    4. else all members, ordered by region priority then alphabetically.

    *preferred_region* re-heads the region ranking (``"auto"`` = no preference).

    Raises ``ValueError`` on an empty *members* — a group always has at least one
    fetched member at the call sites here.
    """
    if not members:
        raise ValueError("cannot resolve a representative for an empty sibling group")

    for leg in (
        [m for m in members if int(m["rom_id"]) in installed_rom_ids],
        [m for m in members if int(m["rom_id"]) in bound_rom_ids],
        [m for m in members if m.get("is_main_sibling")],
        members,
    ):
        if leg:
            return int(min(leg, key=lambda m: _rank_key(m, preferred_region))["rom_id"])

    # Unreachable — the final leg is the full member list, always non-empty here.
    raise AssertionError("resolution chain fell through a non-empty member list")


def canonical_group_name(members: list[dict[str, Any]], preferred_region: str = AUTO_REGION) -> str:
    """Return the group's canonical Steam-shortcut name (ADR-0021 §2/§3).

    The ``name`` of the member ranked first by the **pure** order (region
    priority > alphabetical ``fs_name_no_ext`` > ``rom_id``) — the
    installed/binding/default filters of :func:`resolve_group_representative` are
    deliberately ignored, so the name reflects the region-preferred dump even
    when the *bound* version is forced elsewhere (a Japanese default still yields
    the USA member's name; two Japan dumps + one USA yield the USA member's name
    — never majority voting).

    This is a mint-time-only decision: a shortcut's name is sticky forever after
    creation, so an already-bound group carries its persisted name and never
    calls this. *preferred_region* re-heads the region ranking as above.

    Raises ``ValueError`` on an empty *members*.
    """
    if not members:
        raise ValueError("cannot derive a canonical name for an empty sibling group")
    winner = min(members, key=lambda m: _rank_key(m, preferred_region))
    return str(winner.get("name") or "")
