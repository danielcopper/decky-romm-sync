"""Property-based tests for the sibling-group representative chain (ADR-0021 §3).

The resolver must be a **total, shuffle-stable** order over a group's members:
the chosen representative can never depend on the order the members arrive in
(fetch order is not stable), and the chain priority — installed > existing
binding > RomM default > region priority > alphabetical — must hold exactly. A
representative that flips under a reshuffle would flip which sibling a group's
shortcut binds to on every sync (churn), so these properties are live regression
guards. ``canonical_group_name`` shares the same pure order (ignoring the
installed/binding/default filters) and carries the same determinism guarantee.

See ``tests.domain.test_sync_action_property`` for the convention note on the
``xfail(strict=True)`` pinning of properties that encode an open bug. These
properties encode a TRUE invariant that holds today, so they run live.
"""

from __future__ import annotations

import random

from hypothesis import assume, given
from hypothesis import strategies as st

from domain.sibling_resolution import (
    DEFAULT_REGION_PRIORITY,
    canonical_group_name,
    resolve_group_representative,
)

# A small pool of filename stems (with repeats likely across a group, to exercise
# the fs_name_no_ext tie-break and its rom_id final disambiguator).
_stems = st.sampled_from(["alpha", "Alpha", "beta", "mario", "Zelda", "game", "game (usa)"])

# Known (in the build-time default order) + unknown regions + the empty list, so
# the strategy exercises every region bucket (preferred / default / other / none).
_region_pool = ["Europe", "USA", "World", "Japan", "Germany", "Brazil", "Korea"]
_regions = st.lists(st.sampled_from(_region_pool), max_size=3, unique=True)

# "auto" (no preference) plus any pool region as a user override.
_preferred = st.sampled_from(["auto", *_region_pool])


@st.composite
def _group(draw):
    """A non-empty group: unique rom_ids, each with a stem, region list, unique
    name + default flag, plus an installed subset and a bound subset drawn from
    the group's rom_ids."""
    rom_ids = draw(st.lists(st.integers(min_value=1, max_value=9999), min_size=1, max_size=8, unique=True))
    members = [
        {
            "rom_id": rid,
            "fs_name_no_ext": draw(_stems),
            "regions": draw(_regions),
            "is_main_sibling": draw(st.booleans()),
            "name": f"n{rid}",  # unique per member (rom_ids are unique)
        }
        for rid in rom_ids
    ]
    id_set = set(rom_ids)
    installed = draw(st.sets(st.sampled_from(sorted(id_set)), max_size=len(id_set))) if id_set else set()
    bound = draw(st.sets(st.sampled_from(sorted(id_set)), max_size=len(id_set))) if id_set else set()
    return members, installed, bound


@given(_group(), _preferred)
def test_result_is_always_a_member(group, preferred):
    members, installed, bound = group
    rep = resolve_group_representative(members, installed, bound, preferred)
    assert rep in {m["rom_id"] for m in members}


@given(_group(), st.integers(), _preferred)
def test_shuffle_invariant(group, seed, preferred):
    members, installed, bound = group
    shuffled = list(members)
    random.Random(seed).shuffle(shuffled)
    assert resolve_group_representative(members, installed, bound, preferred) == resolve_group_representative(
        shuffled, installed, bound, preferred
    )


@given(_group(), _preferred)
def test_chain_priority_holds(group, preferred):
    members, installed, bound = group
    rep = resolve_group_representative(members, installed, bound, preferred)
    ids = {m["rom_id"] for m in members}
    installed_here = installed & ids
    bound_here = bound & ids
    defaults = {m["rom_id"] for m in members if m["is_main_sibling"]}

    if installed_here:
        assert rep in installed_here
    elif bound_here:
        assert rep in bound_here
    elif defaults:
        assert rep in defaults
    # else: the region/alphabetical fallback leg — covered below.


def _region_fallback_group(group):
    """The subset of a drawn group with NO filter leg active: empty installed +
    bound sets and no RomM default, so only the region/alphabetical order decides.
    Returns the members (all is_main_sibling forced False)."""
    members, _installed, _bound = group
    return [{**m, "is_main_sibling": False} for m in members]


@given(_group(), _preferred)
def test_region_override_respected_in_fallback(group, preferred):
    """With no filter leg and an override set, a member carrying the preferred
    region always wins over one that doesn't."""
    members = _region_fallback_group(group)
    assume(preferred != "auto")
    has_pref = [m for m in members if preferred in m["regions"]]
    assume(bool(has_pref))
    rep = resolve_group_representative(members, set(), set(), preferred)
    rep_member = next(m for m in members if m["rom_id"] == rep)
    assert preferred in rep_member["regions"]


@given(_group())
def test_no_region_member_never_wins_over_a_regioned_one(group):
    """With no filter leg, a version with no region ranks last — it is never the
    representative when some member carries a region (auto ranking)."""
    members = _region_fallback_group(group)
    assume(any(m["regions"] for m in members) and not all(m["regions"] for m in members))
    rep = resolve_group_representative(members, set(), set(), "auto")
    rep_member = next(m for m in members if m["rom_id"] == rep)
    assert rep_member["regions"]


@given(_group())
def test_known_region_beats_unknown_in_fallback(group):
    """A build-time default region outranks any other named region (auto)."""
    members = _region_fallback_group(group)
    default_set = {r.casefold() for r in DEFAULT_REGION_PRIORITY}
    has_known = [m for m in members if any(r.casefold() in default_set for r in m["regions"])]
    assume(bool(has_known))
    rep = resolve_group_representative(members, set(), set(), "auto")
    rep_member = next(m for m in members if m["rom_id"] == rep)
    assert any(r.casefold() in default_set for r in rep_member["regions"])


@given(_group(), _preferred)
def test_canonical_name_is_always_a_member_name(group, preferred):
    members, _installed, _bound = group
    name = canonical_group_name(members, preferred)
    assert name in {m["name"] for m in members}


@given(_group(), st.integers(), _preferred)
def test_canonical_name_shuffle_invariant(group, seed, preferred):
    members, _installed, _bound = group
    shuffled = list(members)
    random.Random(seed).shuffle(shuffled)
    assert canonical_group_name(members, preferred) == canonical_group_name(shuffled, preferred)


@given(_group(), _preferred)
def test_canonical_name_ignores_filter_legs(group, preferred):
    """The canonical name is a pure function of members + preference — the
    installed/binding/default inputs of the resolver cannot change it."""
    members, installed, bound = group
    baseline = canonical_group_name(members, preferred)
    # Flipping every member's default flag / claiming everything installed+bound
    # must not move the canonical name.
    flipped = [{**m, "is_main_sibling": not m["is_main_sibling"]} for m in members]
    assert canonical_group_name(flipped, preferred) == baseline
    del installed, bound


@given(_group(), _preferred)
def test_canonical_name_winner_carries_preferred_region(group, preferred):
    members, _installed, _bound = group
    assume(preferred != "auto" and any(preferred in m["regions"] for m in members))
    name = canonical_group_name(members, preferred)
    winner = next(m for m in members if m["name"] == name)
    assert preferred in winner["regions"]
