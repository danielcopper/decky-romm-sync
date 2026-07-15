"""Property-based tests for the sibling-group representative chain (ADR-0021 §3).

The resolver must be a **total, shuffle-stable** order over a group's members:
the chosen representative can never depend on the order the members arrive in
(fetch order is not stable), and the chain priority — installed > existing
binding > RomM default > prerelease demotion > region priority > revision
(newest) > alphabetical — must hold exactly. A representative that flips under a
reshuffle would flip which sibling a group's shortcut binds to on every sync
(churn), so these properties are live regression guards. ``canonical_group_name``
shares the same pure order (ignoring the installed/binding/default filters) and
carries the same determinism guarantee.

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

# Revision strings: empty (base dump), numeric (natural compare), alphanumeric.
_revisions = st.sampled_from(["", "1", "2", "3", "10", "A", "B"])

# Bare, unambiguous prerelease markers vs. neutral tags. Bare markers let the
# local ``_is_prerelease`` oracle mirror the module's classifier exactly (an exact
# casefold match), so these properties never re-implement the prefix/numbered
# logic — that is the unit tests' job.
_PRERELEASE_TAGS = ["Alpha", "Beta", "Proto", "Sample", "Demo"]
_NEUTRAL_TAGS = ["Unl", "Aftermarket", "Rumble Version"]
_tags = st.lists(st.sampled_from([*_PRERELEASE_TAGS, *_NEUTRAL_TAGS]), max_size=2, unique=True)


def _is_prerelease(member) -> bool:
    """Oracle: a member is prerelease iff it carries a bare prerelease marker.

    Valid only because the generators draw ``tags`` from ``_PRERELEASE_TAGS`` /
    ``_NEUTRAL_TAGS`` (all bare), where the module's classifier reduces to an
    exact-match membership test.
    """
    return any(tag in _PRERELEASE_TAGS for tag in member["tags"])


@st.composite
def _group(draw):
    """A non-empty group: unique rom_ids, each with a stem, region list, revision,
    tag list, unique name + default flag, plus an installed subset and a bound
    subset drawn from the group's rom_ids."""
    rom_ids = draw(st.lists(st.integers(min_value=1, max_value=9999), min_size=1, max_size=8, unique=True))
    members = [
        {
            "rom_id": rid,
            "fs_name_no_ext": draw(_stems),
            "regions": draw(_regions),
            "revision": draw(_revisions),
            "tags": draw(_tags),
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
    """A drawn group reduced so ONLY the region/revision/alphabetical order can
    decide: no RomM default (``is_main_sibling`` forced False) and every member
    forced retail (``tags`` cleared). Clearing tags neutralizes the prerelease leg
    that ranks BEFORE region, so region-leg properties are not confounded by a
    retail-vs-prerelease demotion; revision is left varied because it ranks AFTER
    region and so never disturbs a region-vs-region or region-vs-none outcome. The
    caller still passes empty installed/bound sets to defeat the filter legs."""
    members, _installed, _bound = group
    return [{**m, "is_main_sibling": False, "tags": []} for m in members]


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
def test_retail_beats_prerelease_region_independent(group, preferred):
    """With no filter leg, a retail dump is ALWAYS the representative when both a
    retail and a prerelease member exist — prerelease demotion ranks before region,
    so this holds for any region layout and any ``preferred_region``."""
    members, _installed, _bound = group
    members = [{**m, "is_main_sibling": False} for m in members]  # defeat the default filter
    assume(any(not _is_prerelease(m) for m in members) and any(_is_prerelease(m) for m in members))
    rep = resolve_group_representative(members, set(), set(), preferred)
    rep_member = next(m for m in members if m["rom_id"] == rep)
    assert not _is_prerelease(rep_member)


@st.composite
def _numeric_rev_same_region_group(draw):
    """A group where every member shares one region and is retail, varying only the
    revision (numeric or empty). Region + prerelease tie, so the revision leg alone
    decides — with pure-numeric revisions natural order == integer order (empty =
    lowest), a simple oracle for 'newest wins'."""
    rom_ids = draw(st.lists(st.integers(min_value=1, max_value=9999), min_size=1, max_size=6, unique=True))
    return [
        {
            "rom_id": rid,
            "fs_name_no_ext": draw(_stems),
            "regions": ["USA"],
            "revision": draw(st.sampled_from(["", "1", "2", "3", "10"])),
            "tags": [],
            "is_main_sibling": False,
            "name": f"n{rid}",
        }
        for rid in rom_ids
    ]


@given(_numeric_rev_same_region_group())
def test_newest_numeric_revision_wins(members):
    """Within one region + retail status, the newest revision wins. Pure-numeric
    revisions make integer order the oracle; the empty (base) revision is lowest."""
    rep = resolve_group_representative(members, set(), set(), "auto")
    rep_member = next(m for m in members if m["rom_id"] == rep)
    max_rev = max(int(m["revision"]) if m["revision"] else -1 for m in members)
    assert (int(rep_member["revision"]) if rep_member["revision"] else -1) == max_rev


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


@st.composite
def _group_with_preferred_retail_member(draw):
    """A drawn group plus a non-"auto" preferred region where at least one RETAIL
    member carries the preferred region — the exact domain of the preferred-region
    naming theorem, built by construction: one drawn member is rewritten into the
    witness, its regions re-drawn around a forced ``preferred`` entry (any
    position, any companions) and its tags re-drawn from the neutral pool only
    (retail = no prerelease marker). assume()-filtering this domain instead would
    reject most draws — enough to degenerate generation into rejection sampling —
    while construction accepts every draw over the same domain."""
    members, _installed, _bound = draw(_group())
    preferred = draw(st.sampled_from(_region_pool))
    companions = draw(st.lists(st.sampled_from([r for r in _region_pool if r != preferred]), max_size=2, unique=True))
    regions = list(companions)
    regions.insert(draw(st.integers(min_value=0, max_value=len(regions))), preferred)
    tags = draw(st.lists(st.sampled_from(_NEUTRAL_TAGS), max_size=2, unique=True))
    idx = draw(st.integers(min_value=0, max_value=len(members) - 1))
    members[idx] = {**members[idx], "regions": regions, "tags": tags}
    return members, preferred


@given(_group_with_preferred_retail_member())
def test_canonical_name_winner_carries_preferred_region(group_and_preferred):
    members, preferred = group_and_preferred
    # A RETAIL member carrying the preferred region is (retail, preferred-region) =
    # the global minimum of the pure order, so nothing outranks it (prerelease
    # ranks before region, so a demoted preferred-region member could otherwise
    # lose to a retail non-preferred one).
    name = canonical_group_name(members, preferred)
    winner = next(m for m in members if m["name"] == name)
    assert preferred in winner["regions"]


@given(_group(), _preferred)
def test_canonical_name_prefers_retail_over_prerelease(group, preferred):
    """The canonical name never comes from a prerelease member when a retail member
    exists — the naming order shares the resolver's prerelease demotion."""
    members, _installed, _bound = group
    assume(any(not _is_prerelease(m) for m in members) and any(_is_prerelease(m) for m in members))
    name = canonical_group_name(members, preferred)
    winner = next(m for m in members if m["name"] == name)
    assert not _is_prerelease(winner)
