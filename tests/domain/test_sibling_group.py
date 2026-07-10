"""Tests for domain/sibling_group.py — client-side sibling-group key derivation."""

from __future__ import annotations

import itertools
from typing import Any

from domain.sibling_group import (
    compute_component_group_keys,
    compute_sibling_group_key,
    target_in_sibling_group,
)


def _rom(rom_id: int, *, platform_id: int = 57, siblings: tuple[int, ...] = (), **ids: int) -> dict[str, Any]:
    """Build a raw RomM ROM dict with ``sibling_roms`` edges and metadata ids."""
    rom: dict[str, Any] = {"id": rom_id, "platform_id": platform_id, "sibling_roms": [{"id": s} for s in siblings]}
    rom.update(ids)
    return rom


class TestCoalesceOrder:
    """The metadata ids coalesce in RomM's fixed order; the first non-null wins."""

    def test_igdb_wins_over_all_others(self):
        rom = {
            "id": 1,
            "platform_id": 57,
            "igdb_id": 3404,
            "ss_id": 9,
            "moby_id": 8,
            "ra_id": 7,
            "hasheous_id": 6,
            "launchbox_id": 5,
            "tgdb_id": 4,
            "flashpoint_id": 3,
        }
        assert compute_sibling_group_key(rom) == "igdb:3404:57"

    def test_ss_wins_when_igdb_absent(self):
        rom = {"id": 1, "platform_id": 57, "ss_id": 22, "moby_id": 8, "ra_id": 7}
        assert compute_sibling_group_key(rom) == "ss:22:57"

    def test_moby_wins_over_ra(self):
        rom = {"id": 1, "platform_id": 57, "moby_id": 88, "ra_id": 7}
        assert compute_sibling_group_key(rom) == "moby:88:57"

    def test_ra_wins_over_hasheous(self):
        rom = {"id": 1, "platform_id": 57, "ra_id": 70, "hasheous_id": 6}
        assert compute_sibling_group_key(rom) == "ra:70:57"

    def test_hasheous_wins_over_launchbox(self):
        rom = {"id": 1, "platform_id": 57, "hasheous_id": 60, "launchbox_id": 5}
        assert compute_sibling_group_key(rom) == "hasheous:60:57"

    def test_launchbox_wins_over_tgdb(self):
        rom = {"id": 1, "platform_id": 57, "launchbox_id": 50, "tgdb_id": 4}
        assert compute_sibling_group_key(rom) == "launchbox:50:57"

    def test_tgdb_wins_over_flashpoint(self):
        rom = {"id": 1, "platform_id": 57, "tgdb_id": 40, "flashpoint_id": 3}
        assert compute_sibling_group_key(rom) == "tgdb:40:57"

    def test_flashpoint_is_last_matched_source(self):
        rom = {"id": 1, "platform_id": 57, "flashpoint_id": 30}
        assert compute_sibling_group_key(rom) == "flashpoint:30:57"


class TestPlatformScoping:
    """The same metadata id on two platforms yields two distinct groups."""

    def test_same_igdb_different_platform_is_different_key(self):
        base = {"id": 1, "igdb_id": 3404}
        key_a = compute_sibling_group_key({**base, "platform_id": 57})
        key_b = compute_sibling_group_key({**base, "platform_id": 12})
        assert key_a == "igdb:3404:57"
        assert key_b == "igdb:3404:12"
        assert key_a != key_b

    def test_same_igdb_same_platform_is_same_key(self):
        a = compute_sibling_group_key({"id": 1, "platform_id": 57, "igdb_id": 3404})
        b = compute_sibling_group_key({"id": 2, "platform_id": 57, "igdb_id": 3404})
        assert a == b == "igdb:3404:57"


class TestUnmatchedFallback:
    """An unmatched ROM falls back to its own id — a solo group."""

    def test_no_metadata_ids_falls_back_to_rom_id(self):
        rom = {"id": 4409, "platform_id": 57}
        assert compute_sibling_group_key(rom) == "romm:4409:57"

    def test_all_metadata_ids_none_falls_back(self):
        rom = {
            "id": 4409,
            "platform_id": 57,
            "igdb_id": None,
            "ss_id": None,
            "moby_id": None,
            "ra_id": None,
            "hasheous_id": None,
            "launchbox_id": None,
            "tgdb_id": None,
            "flashpoint_id": None,
        }
        assert compute_sibling_group_key(rom) == "romm:4409:57"

    def test_two_unmatched_roms_are_distinct_solo_groups(self):
        a = compute_sibling_group_key({"id": 100, "platform_id": 57})
        b = compute_sibling_group_key({"id": 200, "platform_id": 57})
        assert a == "romm:100:57"
        assert b == "romm:200:57"
        assert a != b


class TestMissingAndNoneIds:
    """A None id is skipped as if absent; a later non-null id still wins."""

    def test_none_igdb_skips_to_next_present_source(self):
        rom = {"id": 1, "platform_id": 57, "igdb_id": None, "moby_id": 88}
        assert compute_sibling_group_key(rom) == "moby:88:57"

    def test_missing_igdb_key_skips_to_next(self):
        rom = {"id": 1, "platform_id": 57, "ra_id": 70}
        assert compute_sibling_group_key(rom) == "ra:70:57"

    def test_zero_id_is_treated_as_present(self):
        # 0 is a legitimate (if unusual) id — only None means "unmatched".
        rom = {"id": 1, "platform_id": 57, "igdb_id": 0}
        assert compute_sibling_group_key(rom) == "igdb:0:57"


class TestTargetInSiblingGroupLocal:
    """A LOCAL target is judged by key equality — the component keys encode group
    membership, so a differing persisted key is a different group (a NULL bound key
    accepts any eligible target)."""

    def test_local_target_same_group_is_member(self):
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:100:57",
                target_group_key="igdb:100:57",
                target_is_local=True,
                target_is_server_sibling=True,
            )
            is True
        )

    def test_local_target_different_group_is_not_member(self):
        # The #1359 bug: a RomM sibling that is locally synced under a DIFFERENT
        # key (a conflicting metadata match) is not switchable.
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:1156:57",
                target_group_key="ss:19274:57",
                target_is_local=True,
                target_is_server_sibling=True,
            )
            is False
        )

    def test_null_bound_key_accepts_any_local_target(self):
        # An unbackfilled / solo bound row (NULL key) never blocks a local target.
        assert (
            target_in_sibling_group(
                bound_group_key=None,
                target_group_key="ss:19274:57",
                target_is_local=True,
                target_is_server_sibling=False,
            )
            is True
        )

    def test_local_target_group_check_ignores_server_sibling_flag(self):
        # For a LOCAL target the server-sibling flag is irrelevant — the local key
        # decides. A cross-group local target stays a non-member even if RomM lists
        # it as a sibling.
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:1156:57",
                target_group_key="ss:19274:57",
                target_is_local=True,
                target_is_server_sibling=True,
            )
            is False
        )


class TestTargetInSiblingGroupServerOnly:
    """A SERVER-ONLY target (no local row yet) is judged by canonical compatibility:
    RomM must list it as a sibling AND its id at the bound key's canonical source
    must be absent-or-equal — the persisted key doubles as the group's canonical
    summary (#1360, #1368)."""

    def test_matching_canonical_value_is_member(self):
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:100:57",
                target_ids={"igdb_id": 100, "ss_id": 22},
                target_is_local=False,
                target_is_server_sibling=True,
            )
            is True
        )

    def test_absent_canonical_value_is_member(self):
        # #1368 uneven coverage: the bound group keys on igdb; the sibling lacks an
        # igdb id (matched only on ss/hasheous) → absent-at-canonical → in-group. It
        # joins under the bound key and the next sync re-canonicalizes the component.
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:1001:57",
                target_ids={"ss_id": 2002, "hasheous_id": 3003, "launchbox_id": 4005},
                target_is_local=False,
                target_is_server_sibling=True,
            )
            is True
        )

    def test_conflicting_canonical_value_is_not_member(self):
        # #1360: the sibling carries a DIFFERENT id at the canonical source (a
        # genuine cross-game bridge) → rejected.
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:100:57",
                target_ids={"igdb_id": 999, "ss_id": 22},
                target_is_local=False,
                target_is_server_sibling=True,
            )
            is False
        )

    def test_higher_priority_source_is_member(self):
        # The bound group keys on ss (no igdb). The sibling agrees on ss AND carries
        # a higher-priority igdb — in-group; the switch persists the bound key and
        # the next sync re-canonicalizes the whole component onto igdb.
        assert (
            target_in_sibling_group(
                bound_group_key="ss:2002:57",
                target_ids={"igdb_id": 100, "ss_id": 2002},
                target_is_local=False,
                target_is_server_sibling=True,
            )
            is True
        )

    def test_romm_fallback_bound_key_admits_no_server_target(self):
        # A ``romm:`` bound key has no metadata source to compare against, so no
        # server-only target can be proven compatible (preserves today's blocking).
        assert (
            target_in_sibling_group(
                bound_group_key="romm:4409:57",
                target_ids={"igdb_id": 100},
                target_is_local=False,
                target_is_server_sibling=True,
            )
            is False
        )

    def test_unfetched_detail_is_not_member(self):
        # ``target_ids`` None (a transient detail-fetch miss) can't be judged →
        # non-switchable, exactly as a missing would-be key blocked before.
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:100:57",
                target_ids=None,
                target_is_local=False,
                target_is_server_sibling=True,
            )
            is False
        )

    def test_null_bound_key_accepts_any_server_only_target(self):
        # An unbackfilled / solo bound row can't discriminate — any RomM sibling is
        # accepted, even before its detail is fetched.
        assert (
            target_in_sibling_group(
                bound_group_key=None,
                target_ids=None,
                target_is_local=False,
                target_is_server_sibling=True,
            )
            is True
        )

    def test_not_a_sibling_is_not_member(self):
        assert (
            target_in_sibling_group(
                bound_group_key="igdb:100:57",
                target_ids={"igdb_id": 100},
                target_is_local=False,
                target_is_server_sibling=False,
            )
            is False
        )


class TestComputeComponentGroupKeys:
    """The component kernel: connected components over ``sibling_roms`` edges keyed
    by canonical-source agreement, seeded by resident (persisted) keys."""

    def test_two_fresh_siblings_share_the_canonical_key(self):
        roms = [_rom(1, igdb_id=100, siblings=(2,)), _rom(2, igdb_id=100, siblings=(1,))]
        assert compute_component_group_keys(roms, {}) == {1: "igdb:100:57", 2: "igdb:100:57"}

    def test_uneven_coverage_merges_on_igdb_despite_launchbox_divergence(self):
        # rom A: igdb+ss+hasheous+launchbox; rom B: ss+hasheous+launchbox with the
        # SAME ss/hasheous but a DIFFERENT launchbox and NO igdb. Both key on the
        # canonical igdb — the low-priority launchbox divergence never blocks (#1368).
        a = _rom(1, igdb_id=1001, ss_id=2002, hasheous_id=3003, launchbox_id=4004, siblings=(2,))
        b = _rom(2, ss_id=2002, hasheous_id=3003, launchbox_id=4005, siblings=(1,))
        assert compute_component_group_keys([a, b], {}) == {1: "igdb:1001:57", 2: "igdb:1001:57"}

    def test_chain_smuggled_canonical_conflict_falls_back(self):
        # A igdb:1+ss:5, B ss:5+moby:9, C igdb:2+moby:9 — A-B (ss) and B-C (moby)
        # chain into one component whose canonical (igdb) holds two values {1,2}
        # → no assumption-merge, every member keeps its own coalesce-first key.
        a = _rom(1, igdb_id=1, ss_id=5, siblings=(2,))
        b = _rom(2, ss_id=5, moby_id=9, siblings=(1, 3))
        c = _rom(3, igdb_id=2, moby_id=9, siblings=(2,))
        assert compute_component_group_keys([a, b, c], {}) == {1: "igdb:1:57", 2: "ss:5:57", 3: "igdb:2:57"}

    def test_no_edge_matched_rom_keys_solo_by_own_top_source(self):
        assert compute_component_group_keys([_rom(1, ss_id=22)], {}) == {1: "ss:22:57"}

    def test_no_edge_no_ids_falls_back_to_romm(self):
        assert compute_component_group_keys([_rom(1)], {}) == {1: "romm:1:57"}

    def test_in_unit_resident_preserved_and_seeds_fresh_member(self):
        # A already carries a key (an incremental-reconstructed resident); B is fresh
        # and edges to A but has no igdb → B adopts A's canonical summary, and A is
        # NOT re-keyed (absent from the result).
        a = {"id": 1, "platform_id": 57, "sibling_group_key": "igdb:100:57", "sibling_roms": [{"id": 2}]}
        b = _rom(2, ss_id=22, siblings=(1,))
        assert compute_component_group_keys([a, b], {}) == {2: "igdb:100:57"}

    def test_db_resident_key_seeds_fresh_member(self):
        # B is fresh and edges to a DB-resident sibling (rom 1, not in the unit)
        # whose persisted key is igdb:100:57 → B adopts it.
        assert compute_component_group_keys([_rom(2, ss_id=22, siblings=(1,))], {1: "igdb:100:57"}) == {
            2: "igdb:100:57"
        }

    def test_romm_fallback_resident_contributes_nothing(self):
        # A DB-resident whose key is a romm: fallback offers no canonical candidate,
        # so B keys off its own id.
        assert compute_component_group_keys([_rom(2, ss_id=22, siblings=(1,))], {1: "romm:1:57"}) == {2: "ss:22:57"}

    def test_fresh_member_edging_conflicting_resident_falls_back(self):
        # B (igdb:999) edges to a DB-resident keyed igdb:100 → the component's
        # canonical (igdb) holds {100, 999} → B keeps its own key, no merge.
        assert compute_component_group_keys([_rom(2, igdb_id=999, ss_id=22, siblings=(1,))], {1: "igdb:100:57"}) == {
            2: "igdb:999:57"
        }

    def test_platform_scopes_the_merged_key(self):
        # A carries igdb, B has no ids but edges to A — both key on A's igdb, each
        # scoped by its own platform id (siblings share a platform).
        a = _rom(1, platform_id=12, igdb_id=100, siblings=(2,))
        b = _rom(2, platform_id=12, siblings=(1,))
        assert compute_component_group_keys([a, b], {}) == {1: "igdb:100:12", 2: "igdb:100:12"}

    def test_edge_to_unknown_rom_is_ignored(self):
        # An edge to a rom that is neither fresh nor resident (never fetched, not in
        # the DB) unions nothing and offers no candidate.
        assert compute_component_group_keys([_rom(2, ss_id=22, siblings=(99,))], {}) == {2: "ss:22:57"}

    def test_deterministic_under_input_permutation(self):
        a = _rom(1, igdb_id=1001, ss_id=2002, siblings=(2, 3))
        b = _rom(2, ss_id=2002, siblings=(1, 3))
        c = _rom(3, ss_id=2002, siblings=(1, 2))
        results = [compute_component_group_keys(list(p), {}) for p in itertools.permutations([a, b, c])]
        assert all(r == results[0] for r in results)
        assert results[0] == {1: "igdb:1001:57", 2: "igdb:1001:57", 3: "igdb:1001:57"}
