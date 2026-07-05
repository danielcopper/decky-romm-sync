"""Property-based tests for the sibling-group representative chain (ADR-0021 §3).

The resolver must be a **total, shuffle-stable** order over a group's members:
the chosen representative can never depend on the order the members arrive in
(fetch order is not stable), and the chain priority — installed > existing
binding > RomM default > alphabetical — must hold exactly. A representative that
flips under a reshuffle would flip which sibling a group's shortcut binds to on
every sync (churn), so these properties are live regression guards.

See ``tests.domain.test_sync_action_property`` for the convention note on the
``xfail(strict=True)`` pinning of properties that encode an open bug. These
properties encode a TRUE invariant that holds today, so they run live.
"""

from __future__ import annotations

import random

from hypothesis import given
from hypothesis import strategies as st

from domain.sibling_resolution import resolve_group_representative

# A small pool of filename stems (with repeats likely across a group, to exercise
# the fs_name_no_ext tie-break and its rom_id final disambiguator).
_stems = st.sampled_from(["alpha", "Alpha", "beta", "mario", "Zelda", "game", "game (usa)"])


@st.composite
def _group(draw):
    """A non-empty group: unique rom_ids, each with a stem + a default flag, plus
    an installed subset and a bound subset drawn from the group's rom_ids."""
    rom_ids = draw(st.lists(st.integers(min_value=1, max_value=9999), min_size=1, max_size=8, unique=True))
    members = [
        {
            "rom_id": rid,
            "fs_name_no_ext": draw(_stems),
            "is_main_sibling": draw(st.booleans()),
        }
        for rid in rom_ids
    ]
    id_set = set(rom_ids)
    installed = draw(st.sets(st.sampled_from(sorted(id_set)), max_size=len(id_set))) if id_set else set()
    bound = draw(st.sets(st.sampled_from(sorted(id_set)), max_size=len(id_set))) if id_set else set()
    return members, installed, bound


@given(_group())
def test_result_is_always_a_member(group):
    members, installed, bound = group
    rep = resolve_group_representative(members, installed, bound)
    assert rep in {m["rom_id"] for m in members}


@given(_group(), st.integers())
def test_shuffle_invariant(group, seed):
    members, installed, bound = group
    shuffled = list(members)
    random.Random(seed).shuffle(shuffled)
    assert resolve_group_representative(members, installed, bound) == resolve_group_representative(
        shuffled, installed, bound
    )


@given(_group())
def test_chain_priority_holds(group):
    members, installed, bound = group
    rep = resolve_group_representative(members, installed, bound)
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
    # else: any member is admissible — the alphabetical/rom_id fallback, covered
    # by the shuffle-invariance + determinism properties above.
