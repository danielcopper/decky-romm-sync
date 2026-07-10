"""Property-based tests for the component sibling-group kernel (issue #1368).

``compute_component_group_keys`` partitions a fetched unit's fresh ROMs by
connected components over RomM's ``sibling_roms`` edges and keys each component by
its canonical source. The safety invariants stated directly:

* **Determinism** — the partition can never depend on the order ROMs arrive in
  (fetch order is not stable), so any permutation of the same input yields the
  same keys.
* **Well-defined partition** — every fresh ROM receives exactly one non-empty key.
* **Unanimity guard / never-merge-on-canonical-conflict** — two ROMs share a
  (non-``romm``) key only if every member carrying the canonical id agrees on its
  value; a component whose canonical source holds conflicting values never merges.

See ``tests.domain.test_sync_action_property`` for the convention note on
``xfail(strict=True)`` pinning of properties that encode an open bug. These
properties hold today, so they run live.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from domain.sibling_group import compute_component_group_keys

_Rom = dict[str, Any]

# The coalesce order mirrored locally (source label → RomM dict field), so the
# properties never import the module's private map.
_FIELD_BY_SOURCE = {
    "igdb": "igdb_id",
    "ss": "ss_id",
    "moby": "moby_id",
    "ra": "ra_id",
    "hasheous": "hasheous_id",
    "launchbox": "launchbox_id",
    "tgdb": "tgdb_id",
    "flashpoint": "flashpoint_id",
}
_SOURCES = list(_FIELD_BY_SOURCE.values())


@st.composite
def _fresh_units(draw: st.DrawFn) -> list[_Rom]:
    """Generate a unit of fresh ROMs with random metadata ids + symmetric edges.

    A small id value pool (1-3) and a small ROM count keep collisions - and thus
    genuine cross-game bridges — frequent, so the generated space exercises both
    the merge and the no-merge branches. Every ROM shares platform 57 (siblings are
    per-platform), so a shared value always means a real merge candidate.
    """
    n = draw(st.integers(min_value=1, max_value=5))
    roms: list[_Rom] = []
    for rom_id in range(1, n + 1):
        rom: _Rom = {"id": rom_id, "platform_id": 57, "sibling_roms": []}
        for field in _SOURCES:
            if draw(st.booleans()):
                rom[field] = draw(st.integers(min_value=1, max_value=3))
        roms.append(rom)
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                roms[i]["sibling_roms"].append({"id": j + 1})
                roms[j]["sibling_roms"].append({"id": i + 1})
    return roms


@st.composite
def _units_with_residents(draw: st.DrawFn) -> tuple[list[_Rom], dict[int, str]]:
    """A fresh unit plus DB-resident siblings (outside the unit) and edges into them.

    Resident ids live in a disjoint range (100+), each keyed either as a parseable
    ``{source}:{value}:57`` summary (a real canonical candidate) or a ``romm:``
    fallback (contributes nothing) — so a fresh member edging into a resident
    exercises both the seed-from-resident merge and the ignore-the-fallback branch.
    """
    roms = draw(_fresh_units())
    resident_ids = list(range(100, 100 + draw(st.integers(min_value=1, max_value=3))))
    resident_keys: dict[int, str] = {}
    for resident_id in resident_ids:
        if draw(st.booleans()):
            source = draw(st.sampled_from(list(_FIELD_BY_SOURCE)))
            value = draw(st.integers(min_value=1, max_value=3))
            resident_keys[resident_id] = f"{source}:{value}:57"
        else:
            resident_keys[resident_id] = f"romm:{resident_id}:57"
    for rom in roms:
        for resident_id in resident_ids:
            if draw(st.booleans()):
                rom["sibling_roms"].append({"id": resident_id})
    return roms, resident_keys


@given(roms=_fresh_units(), data=st.data())
def test_determinism_under_permutation(roms: list[_Rom], data: st.DataObject) -> None:
    baseline = compute_component_group_keys(roms, {})
    permuted = list(data.draw(st.permutations(roms)))
    assert compute_component_group_keys(permuted, {}) == baseline


@given(unit=_units_with_residents(), data=st.data())
def test_determinism_under_permutation_with_residents(
    unit: tuple[list[_Rom], dict[int, str]], data: st.DataObject
) -> None:
    # Input-order independence must also hold when fresh members edge into
    # DB-resident siblings — the resident candidates and romm-fallbacks must not
    # make the partition depend on fetch order.
    roms, resident_keys = unit
    baseline = compute_component_group_keys(roms, resident_keys)
    permuted = list(data.draw(st.permutations(roms)))
    assert compute_component_group_keys(permuted, resident_keys) == baseline


@given(_fresh_units())
def test_every_fresh_rom_gets_exactly_one_nonempty_key(roms: list[_Rom]) -> None:
    keys = compute_component_group_keys(roms, {})
    assert set(keys) == {rom["id"] for rom in roms}
    assert all(isinstance(value, str) and value for value in keys.values())


@given(_fresh_units())
def test_assigned_key_never_contradicts_members_own_canonical_id(roms: list[_Rom]) -> None:
    # never-merge-on-canonical-conflict, per member: the key a ROM receives never
    # overrides an explicit, conflicting id it carries at the key's canonical source.
    keys = compute_component_group_keys(roms, {})
    by_id = {rom["id"]: rom for rom in roms}
    for rom_id, key in keys.items():
        source, value, _platform = key.split(":")
        if source == "romm":
            continue
        own = by_id[rom_id].get(_FIELD_BY_SOURCE[source])
        assert own is None or str(own) == value


@given(_fresh_units())
def test_merged_group_is_unanimous_at_canonical_source(roms: list[_Rom]) -> None:
    # unanimity guard: every ROM sharing a (non-romm) key that carries the canonical
    # id agrees on its value — a component with conflicting canonical values can
    # never collapse onto one key.
    keys = compute_component_group_keys(roms, {})
    by_id = {rom["id"]: rom for rom in roms}
    grouped: dict[str, list[int]] = defaultdict(list)
    for rom_id, key in keys.items():
        grouped[key].append(rom_id)
    for key, members in grouped.items():
        source, value, _platform = key.split(":")
        if source == "romm":
            continue
        field = _FIELD_BY_SOURCE[source]
        present = {str(by_id[m][field]) for m in members if by_id[m].get(field) is not None}
        assert present <= {value}


# ── Pinned examples (issue #1368) — the concrete cases the properties generalize ──


def test_pinned_uneven_coverage_merges_on_igdb() -> None:
    # rom A igdb+ss+hasheous+launchbox; rom B ss+hasheous+launchbox with the same
    # ss/hasheous but a DIFFERENT launchbox and NO igdb → both key on igdb.
    a = {
        "id": 1,
        "platform_id": 57,
        "igdb_id": 1001,
        "ss_id": 2002,
        "hasheous_id": 3003,
        "launchbox_id": 4004,
        "sibling_roms": [{"id": 2}],
    }
    b = {
        "id": 2,
        "platform_id": 57,
        "ss_id": 2002,
        "hasheous_id": 3003,
        "launchbox_id": 4005,
        "sibling_roms": [{"id": 1}],
    }
    assert compute_component_group_keys([a, b], {}) == {1: "igdb:1001:57", 2: "igdb:1001:57"}


def test_pinned_launchbox_divergence_does_not_block() -> None:
    # Two dumps agreeing on the canonical igdb but with DIFFERENT launchbox ids
    # (per regional release) still merge — the low-priority conflict is ignored.
    a = {"id": 1, "platform_id": 57, "igdb_id": 100, "launchbox_id": 4004, "sibling_roms": [{"id": 2}]}
    b = {"id": 2, "platform_id": 57, "igdb_id": 100, "launchbox_id": 4005, "sibling_roms": [{"id": 1}]}
    assert compute_component_group_keys([a, b], {}) == {1: "igdb:100:57", 2: "igdb:100:57"}


def test_pinned_chain_smuggled_canonical_conflict_splits() -> None:
    # A igdb:1+ss:5, B ss:5+moby:9, C igdb:2+moby:9 — chained by ss then moby into
    # one component whose canonical (igdb) holds {1, 2} → every member falls back.
    a = {"id": 1, "platform_id": 57, "igdb_id": 1, "ss_id": 5, "sibling_roms": [{"id": 2}]}
    b = {"id": 2, "platform_id": 57, "ss_id": 5, "moby_id": 9, "sibling_roms": [{"id": 1}, {"id": 3}]}
    c = {"id": 3, "platform_id": 57, "igdb_id": 2, "moby_id": 9, "sibling_roms": [{"id": 2}]}
    assert compute_component_group_keys([a, b, c], {}) == {1: "igdb:1:57", 2: "ss:5:57", 3: "igdb:2:57"}


def test_pinned_no_edge_fallback() -> None:
    # An unmatched, un-edged ROM is its own solo romm: group.
    assert compute_component_group_keys([{"id": 9, "platform_id": 57, "sibling_roms": []}], {}) == {9: "romm:9:57"}
