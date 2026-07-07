"""Tests for domain.sibling_resolution — the sibling-group representative chain (ADR-0021 §3)."""

from __future__ import annotations

import pytest

from domain.sibling_resolution import canonical_group_name, resolve_group_representative


def _m(rom_id, *, fs_name_no_ext="", is_main_sibling=False, regions=(), name="", revision="", tags=()):
    """A group member dict as build_shortcuts_data shapes it for the resolver."""
    return {
        "rom_id": rom_id,
        "fs_name_no_ext": fs_name_no_ext,
        "is_main_sibling": is_main_sibling,
        "regions": list(regions),
        "name": name,
        "revision": revision,
        "tags": list(tags),
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


class TestRevisionLeg:
    """The revision leg (ADR-0021 §3): newest revision wins, but only within a region."""

    def test_rev1_beats_base_same_region(self):
        # (USA) (Rev 1) beats (USA) base — a real revision outranks the empty one.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], revision=""),
            _m(2, fs_name_no_ext="z", regions=["USA"], revision="1"),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_rev3_beats_rev1_same_region(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], revision="1"),
            _m(2, fs_name_no_ext="z", regions=["USA"], revision="3"),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_rev10_beats_rev2_natural_numeric(self):
        # Natural numeric compare, not lexical: "10" > "2" (lexically "10" < "2").
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], revision="2"),
            _m(2, fs_name_no_ext="z", regions=["USA"], revision="10"),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_alphanumeric_revision_b_beats_a(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], revision="A"),
            _m(2, fs_name_no_ext="z", regions=["USA"], revision="B"),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_alphanumeric_revision_case_insensitive_tie(self):
        # "B" and "b" are the same revision → the leg ties, alphabetical fs decides.
        members = [
            _m(1, fs_name_no_ext="z", regions=["USA"], revision="B"),
            _m(2, fs_name_no_ext="a", regions=["USA"], revision="b"),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_empty_revision_is_lowest(self):
        # Base (empty revision) loses to any real revision, even a low one.
        members = [
            _m(1, fs_name_no_ext="z", regions=["USA"], revision="1"),
            _m(2, fs_name_no_ext="a", regions=["USA"], revision=""),
        ]
        assert resolve_group_representative(members, set(), set()) == 1

    def test_non_decimal_digit_revision_does_not_crash(self):
        # "²" is isdigit()-True but not int()-parseable — it must rank as text,
        # not raise ValueError and abort the resolution (LOW review finding).
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], revision="²"),
            _m(2, fs_name_no_ext="z", regions=["USA"], revision="1"),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_revision_only_breaks_ties_within_a_region(self):
        # (USA) base beats (Europe) (Rev 9): region ranks BEFORE revision, so a
        # higher revision never lifts a lower-ranked region.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], revision=""),
            _m(2, fs_name_no_ext="z", regions=["Europe"], revision="9"),
        ]
        assert resolve_group_representative(members, set(), set()) == 1


class TestPrereleaseDemotion:
    """The prerelease leg (ADR-0021 §3): a retail dump beats every prerelease, across regions."""

    def test_beta_loses_to_base_same_region(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=["Beta"]),
            _m(2, fs_name_no_ext="z", regions=["USA"], tags=[]),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_prerelease_demotion_crosses_regions(self):
        # THE cross-region case: a finished (Japan) release beats a (USA) (Beta),
        # even though USA outranks Japan — prerelease demotion ranks before region.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=["Beta"]),
            _m(2, fs_name_no_ext="z", regions=["Japan"], tags=[]),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    @pytest.mark.parametrize("tag", ["Alpha", "Beta", "Beta 1", "Beta 2", "Proto", "Sample", "Demo"])
    def test_all_prerelease_markers_recognized(self, tag):
        # The retail Japan dump wins over the USA prerelease regardless of marker.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=[tag]),
            _m(2, fs_name_no_ext="z", regions=["Japan"], tags=[]),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    @pytest.mark.parametrize("tag", ["ALPHA", "beta", "PrOtO", "demo 3", "Beta1"])
    def test_marker_match_is_case_insensitive(self, tag):
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=[tag]),
            _m(2, fs_name_no_ext="z", regions=["Japan"], tags=[]),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    @pytest.mark.parametrize("tag", ["Unl", "Aftermarket", "Castlevania Advance Collection", "Rumble Version"])
    def test_neutral_tags_not_demoted(self, tag):
        # "Unl" / "Aftermarket" / a collection-name / an unknown tag carry no draft
        # signal → the USA member stays retail and its region wins over Japan.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=[tag]),
            _m(2, fs_name_no_ext="z", regions=["Japan"], tags=[]),
        ]
        assert resolve_group_representative(members, set(), set()) == 1

    @pytest.mark.parametrize("tag", ["Betamax", "Prototype", "Sampler"])
    def test_word_starting_with_marker_not_demoted(self, tag):
        # A longer word that merely starts with a marker (not a numbered variant)
        # is neutral → the USA member keeps its region win over Japan.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=[tag]),
            _m(2, fs_name_no_ext="z", regions=["Japan"], tags=[]),
        ]
        assert resolve_group_representative(members, set(), set()) == 1

    def test_prerelease_demotion_beats_higher_revision(self):
        # A (USA) retail base beats a (USA) (Beta) (Rev 2): prerelease ranks before
        # revision, so a higher revision cannot rescue a prerelease.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=["Beta"], revision="2"),
            _m(2, fs_name_no_ext="z", regions=["USA"], tags=[], revision=""),
        ]
        assert resolve_group_representative(members, set(), set()) == 2

    def test_two_prereleases_fall_through_to_region(self):
        # Both prerelease → the demotion ties, so region priority decides among them.
        members = [
            _m(1, fs_name_no_ext="a", regions=["Japan"], tags=["Beta"]),
            _m(2, fs_name_no_ext="z", regions=["USA"], tags=["Proto"]),
        ]
        assert resolve_group_representative(members, set(), set()) == 2


class TestFilenameOnlyVariants:
    """Filename-only re-dumps RomM does NOT parse into tags fall to the alphabetical leg.

    "(Virtual Console)" and "(Extended Edition)" appear only in ``fs_name_no_ext``,
    not as structured ``tags`` / ``regions`` / ``revision``. With every ranked
    dimension equal, the alphabetical prefix rule keeps the base dump (the shorter
    stem) ahead of the re-dump.
    """

    def test_base_beats_virtual_console_redump(self):
        members = [
            _m(1, fs_name_no_ext="game", regions=["USA"], name="Game"),
            _m(2, fs_name_no_ext="game (virtual console)", regions=["USA"], name="Game (Virtual Console)"),
        ]
        assert resolve_group_representative(members, set(), set()) == 1

    def test_base_beats_extended_edition_redump(self):
        members = [
            _m(1, fs_name_no_ext="game", regions=["USA"], name="Game"),
            _m(2, fs_name_no_ext="game (extended edition)", regions=["USA"], name="Game (Extended Edition)"),
        ]
        assert resolve_group_representative(members, set(), set()) == 1

    def test_discussed_example_world_usa_extended_edition(self):
        # The discussed group: (World), (USA), (USA) (Extended Edition) → World wins
        # on region priority, before the filename leg is even consulted.
        members = [
            _m(1, fs_name_no_ext="game (usa)", regions=["USA"], name="Game (USA)"),
            _m(
                2, fs_name_no_ext="game (usa) (extended edition)", regions=["USA"], name="Game (USA) (Extended Edition)"
            ),
            _m(3, fs_name_no_ext="game (world)", regions=["World"], name="Game (World)"),
        ]
        assert resolve_group_representative(members, set(), set()) == 3


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

    def test_name_follows_prerelease_demotion_cross_region(self):
        # (Japan) retail + (USA) (Beta): the canonical NAME comes from the Japan
        # retail member — prerelease demotion outranks region for naming too.
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], tags=["Beta"], name="USA Beta"),
            _m(2, fs_name_no_ext="z", regions=["Japan"], tags=[], name="Japan Final"),
        ]
        assert canonical_group_name(members) == "Japan Final"

    def test_name_follows_newest_revision_within_region(self):
        members = [
            _m(1, fs_name_no_ext="a", regions=["USA"], revision="1", name="USA Rev1"),
            _m(2, fs_name_no_ext="z", regions=["USA"], revision="3", name="USA Rev3"),
        ]
        assert canonical_group_name(members) == "USA Rev3"

    def test_name_base_beats_filename_only_variant(self):
        # RomM does not parse "(Virtual Console)" into tags → the alphabetical leg
        # keeps the base dump's name as canonical.
        members = [
            _m(1, fs_name_no_ext="game", regions=["USA"], name="Game"),
            _m(2, fs_name_no_ext="game (virtual console)", regions=["USA"], name="Game (Virtual Console)"),
        ]
        assert canonical_group_name(members) == "Game"

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
