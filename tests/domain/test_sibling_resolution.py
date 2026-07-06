"""Tests for domain.sibling_resolution — the sibling-group representative chain (ADR-0021 §3)."""

from __future__ import annotations

import pytest

from domain.sibling_resolution import canonical_group_name, resolve_group_representative


def _m(rom_id, *, fs_name_no_ext="", is_main_sibling=False, regions=(), name=""):
    """A group member dict as build_shortcuts_data shapes it for the resolver."""
    return {
        "rom_id": rom_id,
        "fs_name_no_ext": fs_name_no_ext,
        "is_main_sibling": is_main_sibling,
        "regions": list(regions),
        "name": name,
    }


class TestResolveGroupRepresentative:
    def test_installed_wins_over_binding_default_and_alphabetical(self):
        members = [
            _m(1, fs_name_no_ext="a_first", is_main_sibling=True),  # default + alphabetically first
            _m(2, fs_name_no_ext="z_last"),  # only this one is installed
        ]
        # rom 2 is installed AND bound; rom 1 is the default and alphabetically first.
        assert resolve_group_representative(members, installed_rom_ids={2}, bound_rom_ids={2}) == 2

    def test_multiple_installed_break_by_alphabetical_then_rom_id(self):
        members = [
            _m(5, fs_name_no_ext="beta"),
            _m(3, fs_name_no_ext="alpha"),  # alphabetically first among installed
            _m(9, fs_name_no_ext="alpha"),
        ]
        # roms 3 and 9 tie on fs_name_no_ext="alpha" → lower rom_id (3) wins.
        assert resolve_group_representative(members, installed_rom_ids={3, 5, 9}, bound_rom_ids=set()) == 3

    def test_existing_binding_wins_when_none_installed(self):
        members = [
            _m(1, fs_name_no_ext="a_first", is_main_sibling=True),
            _m(2, fs_name_no_ext="z_last"),
        ]
        # Nothing installed; rom 2 carries the existing binding → it wins over the default.
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids={2}) == 2

    def test_default_wins_when_none_installed_or_bound(self):
        members = [
            _m(1, fs_name_no_ext="z_last", is_main_sibling=True),  # RomM default
            _m(2, fs_name_no_ext="a_first"),  # alphabetically first but not default
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 1

    def test_alphabetical_fallback_when_no_installed_bound_or_default(self):
        members = [
            _m(1, fs_name_no_ext="Zelda"),
            _m(2, fs_name_no_ext="alpha"),  # lower-cased "alpha" < "zelda"
            _m(3, fs_name_no_ext="Mario"),
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_rom_id_is_final_tie_break_on_equal_names(self):
        members = [_m(7, fs_name_no_ext="Game"), _m(3, fs_name_no_ext="Game")]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 3

    def test_multiple_defaults_break_alphabetically(self):
        members = [
            _m(1, fs_name_no_ext="z", is_main_sibling=True),
            _m(2, fs_name_no_ext="a", is_main_sibling=True),
        ]
        # Both are RomM defaults → alphabetical fs_name_no_ext decides.
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_solo_group_returns_its_only_member(self):
        assert resolve_group_representative([_m(42)], installed_rom_ids=set(), bound_rom_ids=set()) == 42

    def test_empty_members_raises_value_error(self):
        with pytest.raises(ValueError, match="empty sibling group"):
            resolve_group_representative([], installed_rom_ids=set(), bound_rom_ids=set())

    def test_result_ignores_member_order(self):
        members = [
            _m(1, fs_name_no_ext="z_last"),
            _m(2, fs_name_no_ext="a_first", is_main_sibling=True),
            _m(3, fs_name_no_ext="m_mid"),
        ]
        forward = resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set())
        reversed_ = resolve_group_representative(list(reversed(members)), installed_rom_ids=set(), bound_rom_ids=set())
        assert forward == reversed_ == 2


class TestRegionPriorityLeg:
    """The region-priority leg (ADR-0021 §3): region rank > alphabetical > rom_id."""

    def test_region_priority_beats_alphabetical_when_no_earlier_leg(self):
        # Japan's fs_name_no_ext sorts first alphabetically, but USA outranks Japan
        # in the build-time order, so USA is the representative — the Pokémon fix.
        members = [
            _m(1, fs_name_no_ext="game_japan", regions=["Japan"]),
            _m(2, fs_name_no_ext="game_usa", regions=["USA"]),
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_full_default_order_world_usa_europe_japan(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["Japan"]),
            _m(2, fs_name_no_ext="b", regions=["World"]),
            _m(3, fs_name_no_ext="c", regions=["USA"]),
            _m(4, fs_name_no_ext="d", regions=["Europe"]),
        ]
        # World tops the build-time order → rom 2.
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_multi_region_member_ranked_by_best_region(self):
        # rom 1 carries [Japan, World]; its BEST region (World) outranks rom 2's USA.
        members = [
            _m(1, fs_name_no_ext="z", regions=["Japan", "World"]),
            _m(2, fs_name_no_ext="a", regions=["USA"]),
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 1

    def test_unknown_regions_rank_after_known_alphabetically(self):
        # Neither region is in the default order → both bucket-2, ranked
        # alphabetically among themselves: "brazil" < "korea" → rom 1.
        members = [
            _m(1, fs_name_no_ext="z", regions=["Brazil"]),
            _m(2, fs_name_no_ext="a", regions=["Korea"]),
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 1

    def test_known_region_outranks_unknown_region(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["Brazil"]),  # unknown → bucket 2
            _m(2, fs_name_no_ext="z", regions=["Japan"]),  # known → bucket 1
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_no_region_member_ranks_last(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=[]),  # no region → ranks last
            _m(2, fs_name_no_ext="z", regions=["Brazil"]),  # any region beats none
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_region_falls_through_to_alphabetical_when_regions_tie(self):
        # Same region on both → region rank ties → alphabetical fs_name_no_ext.
        members = [
            _m(1, fs_name_no_ext="z", regions=["USA"]),
            _m(2, fs_name_no_ext="a", regions=["USA"]),
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_installed_leg_still_wins_over_region(self):
        # The installed filter fires before region priority: even though USA
        # outranks Japan, the installed Japan dump is the representative.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"]),
            _m(2, fs_name_no_ext="z", regions=["Japan"]),
        ]
        assert resolve_group_representative(members, installed_rom_ids={2}, bound_rom_ids=set()) == 2

    def test_override_puts_chosen_region_on_top(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["Europe"]),  # tops the DEFAULT order
            _m(2, fs_name_no_ext="z", regions=["Germany"]),  # user prefers Germany
        ]
        assert resolve_group_representative(members, set(), set(), preferred_region="Germany") == 2
        # Without the override, Europe (default order) wins.
        assert resolve_group_representative(members, set(), set(), preferred_region="auto") == 1

    def test_override_case_insensitive_match(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["Europe"]),
            _m(2, fs_name_no_ext="z", regions=["Germany"]),
        ]
        assert resolve_group_representative(members, set(), set(), preferred_region="germany") == 2


class TestCanonicalGroupName:
    """``canonical_group_name`` — the sticky mint name follows the PURE ranking (ADR-0021 §2/§3)."""

    def test_two_japan_one_usa_yields_usa_name(self):
        # The exact user scenario: majority Japan, one USA → the USA member's
        # name, NOT majority voting.
        members = [
            _m(1, fs_name_no_ext="a", regions=["Japan"], name="ポケットモンスター 1"),
            _m(2, fs_name_no_ext="b", regions=["Japan"], name="ポケットモンスター 2"),
            _m(3, fs_name_no_ext="c", regions=["USA"], name="Pokemon FireRed"),
        ]
        assert canonical_group_name(members) == "Pokemon FireRed"

    def test_only_japan_group_yields_japanese_name(self):
        members = [
            _m(1, fs_name_no_ext="b", regions=["Japan"], name="ファイアレッド B"),
            _m(2, fs_name_no_ext="a", regions=["Japan"], name="ファイアレッド A"),
        ]
        # Regions tie → alphabetical fs_name_no_ext → rom 2 ("a").
        assert canonical_group_name(members) == "ファイアレッド A"

    def test_ignores_installed_binding_default_legs(self):
        # A Japanese RomM default; the canonical NAME still follows region
        # priority (USA), decoupled from the bound/default version.
        members = [
            _m(1, fs_name_no_ext="a", regions=["Japan"], is_main_sibling=True, name="Japan Default"),
            _m(2, fs_name_no_ext="z", regions=["USA"], name="USA Name"),
        ]
        assert canonical_group_name(members) == "USA Name"

    def test_override_selects_preferred_region_name(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["Europe"], name="Euro Name"),
            _m(2, fs_name_no_ext="z", regions=["Germany"], name="German Name"),
        ]
        assert canonical_group_name(members, preferred_region="Germany") == "German Name"

    def test_no_region_falls_back_to_alphabetical_name(self):
        members = [
            _m(1, fs_name_no_ext="zelda", name="Z Name"),
            _m(2, fs_name_no_ext="alpha", name="A Name"),
        ]
        assert canonical_group_name(members) == "A Name"

    def test_partial_view_uses_only_fetched_members(self):
        # Only the Japanese member was fetched (a collection partial view) → its
        # name is canonical among what's present; documented behaviour.
        members = [_m(1, fs_name_no_ext="a", regions=["Japan"], name="Japan Only")]
        assert canonical_group_name(members) == "Japan Only"

    def test_solo_group_returns_its_name(self):
        assert canonical_group_name([_m(9, name="Solo")]) == "Solo"

    def test_empty_members_raises_value_error(self):
        with pytest.raises(ValueError, match="empty sibling group"):
            canonical_group_name([])
