"""domain.rom_candidates — the name match and the evidence behind each offer.

The normalization is the whole filter: everything downstream trusts that two
names denoting the same game reduce to one string and two names denoting
different games do not. The cases here are the ones a real library produces —
region tags, revision tags, dump-status brackets, unicode titles — plus the
degenerate ones that would silently match everything if they were not refused.
"""

from __future__ import annotations

from domain.rom_candidates import (
    CANDIDATE_LIMIT,
    CRC32_MATCH,
    NAME_MATCH,
    SIZE_MATCH,
    LocalEntry,
    candidates_refusal,
    matching_entries,
    normalize_rom_name,
    rank_candidates,
)

_ACCEPTED = frozenset({".gba", ".zip", ".sfc"})


def _entry(name: str, *, is_dir: bool = False, size: int = 0, directory: str = "/roms/gba") -> LocalEntry:
    return LocalEntry(
        name=name,
        path=f"{directory}/{name}",
        is_dir=is_dir,
        size_bytes=size,
        modified_at=1700000000.0,
    )


class TestNormalizeRomName:
    def test_a_region_tag_is_not_part_of_the_game(self) -> None:
        assert normalize_rom_name("Mario Golf - Advance Tour (USA).zip") == "mario golf advance tour"

    def test_two_names_of_the_same_game_reduce_to_one_string(self) -> None:
        assert normalize_rom_name("Mario Golf - Advance Tour (U).zip") == normalize_rom_name(
            "Mario Golf - Advance Tour (Rev 1) (USA).gba"
        )

    def test_several_tag_groups_all_go(self) -> None:
        assert normalize_rom_name("Zelda (USA) (Rev 2) (Virtual Console).zip") == "zelda"

    def test_square_brackets_are_tags_too(self) -> None:
        assert normalize_rom_name("Zelda [!][b1].sfc") == "zelda"

    def test_parens_and_brackets_mix(self) -> None:
        assert normalize_rom_name("Zelda (USA) [!].sfc") == "zelda"

    def test_a_bracket_nested_in_a_paren_closes_with_it(self) -> None:
        assert normalize_rom_name("Zelda (Rev [1]) Ocarina.sfc") == "zelda ocarina"

    def test_punctuation_collapses_to_single_spaces_and_case_is_dropped(self) -> None:
        assert normalize_rom_name("SUPER___Mario--World!!.sfc") == "super mario world"

    def test_leading_and_trailing_punctuation_is_trimmed(self) -> None:
        assert normalize_rom_name("  ...Metroid... .sfc") == "metroid"

    def test_a_unicode_title_survives_intact(self) -> None:
        assert normalize_rom_name("Pokémon Édition Rouge (France).gba") == "pokémon édition rouge"

    def test_a_non_latin_title_survives_intact(self) -> None:
        assert normalize_rom_name("ポケモン (Japan).gba") == "ポケモン"

    def test_a_name_that_is_only_tags_normalizes_to_nothing(self) -> None:
        assert normalize_rom_name("(USA).zip") == ""

    def test_a_name_that_is_only_punctuation_normalizes_to_nothing(self) -> None:
        assert normalize_rom_name("---.zip") == ""

    def test_an_unmatched_opener_swallows_the_rest_rather_than_keeping_the_tag(self) -> None:
        assert normalize_rom_name("Zelda (USA.sfc") == "zelda"

    def test_an_unmatched_closer_is_dropped(self) -> None:
        assert normalize_rom_name("Zelda) Ocarina.sfc") == "zelda ocarina"

    def test_only_the_last_extension_comes_off(self) -> None:
        # ``splitext`` is deliberately blunt. Symmetric on both sides of the
        # comparison, which is what makes it safe; the cost is pinned here.
        assert normalize_rom_name("Game.tar.gz") == "game tar"

    def test_a_dotted_version_loses_its_last_component_on_both_sides(self) -> None:
        assert normalize_rom_name("Sonic 3.0") == "sonic 3"

    def test_an_es_de_collapsed_directory_name_loses_its_extension(self) -> None:
        # The plugin's own multi-file installs are named ``<game>.m3u/`` so ES-DE
        # collapses them; a hand-made directory carries no extension. Both have
        # to reduce to the same game.
        assert normalize_rom_name("Final Fantasy VII (USA).m3u") == normalize_rom_name("Final Fantasy VII (U)")


class TestMatchingEntries:
    def test_an_empty_platform_directory_yields_nothing(self) -> None:
        assert (
            matching_entries(
                (),
                wanted_name="mario golf advance tour",
                want_dir=False,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_nothing_matches_a_different_game(self) -> None:
        entries = (_entry("Zelda (USA).gba"), _entry("Metroid (USA).gba"))
        assert (
            matching_entries(
                entries,
                wanted_name="mario golf advance tour",
                want_dir=False,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_the_same_game_under_another_name_matches(self) -> None:
        wanted = _entry("Mario Golf - Advance Tour (U).zip")
        found = matching_entries(
            (wanted, _entry("Zelda (USA).gba")),
            wanted_name="mario golf advance tour",
            want_dir=False,
            accepted_extensions=_ACCEPTED,
            covered_paths=frozenset(),
        )
        assert found == (wanted,)

    def test_an_entry_an_install_row_accounts_for_is_another_game_s_content(self) -> None:
        covered = _entry("Mario Golf - Advance Tour (U).zip")
        assert (
            matching_entries(
                (covered,),
                wanted_name="mario golf advance tour",
                want_dir=False,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset({covered.path}),
            )
            == ()
        )

    def test_an_extension_the_system_does_not_accept_is_not_a_rom(self) -> None:
        assert (
            matching_entries(
                (_entry("Mario Golf - Advance Tour (U).txt"),),
                wanted_name="mario golf advance tour",
                want_dir=False,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_frontend_bookkeeping_falls_out_of_the_positive_test(self) -> None:
        # No blacklist names these: they are excluded because their extension is
        # absent from the system's accept list, which is a list nobody maintains.
        entries = (_entry("systeminfo.txt"), _entry(".directory"), _entry("gamelist.xml"))
        for name in ("systeminfo", "directory", "gamelist"):
            assert (
                matching_entries(
                    entries,
                    wanted_name=name,
                    want_dir=False,
                    accepted_extensions=_ACCEPTED,
                    covered_paths=frozenset(),
                )
                == ()
            )

    def test_an_accept_list_that_could_not_answer_does_not_turn_the_search_off(self) -> None:
        # An empty set means ES-DE could not be read. Every other consumer of that
        # list treats it as "cannot tell" and takes its permissive branch; the
        # name match plus the user's confirmation is what the offer rests on.
        wanted = _entry("Mario Golf - Advance Tour (U).xyz")
        assert matching_entries(
            (wanted,),
            wanted_name="mario golf advance tour",
            want_dir=False,
            accepted_extensions=frozenset(),
            covered_paths=frozenset(),
        ) == (wanted,)

    def test_a_directory_is_never_extension_tested(self) -> None:
        wanted = _entry("Final Fantasy VII (U)", is_dir=True)
        assert matching_entries(
            (wanted,),
            wanted_name="final fantasy vii",
            want_dir=True,
            accepted_extensions=_ACCEPTED,
            covered_paths=frozenset(),
        ) == (wanted,)

    def test_a_file_is_not_offered_for_a_rom_the_server_serves_as_a_folder(self) -> None:
        assert (
            matching_entries(
                (_entry("Final Fantasy VII (U).zip"),),
                wanted_name="final fantasy vii",
                want_dir=True,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_a_folder_is_not_offered_for_a_rom_the_server_serves_as_one_file(self) -> None:
        assert (
            matching_entries(
                (_entry("Zelda (U)", is_dir=True),),
                wanted_name="zelda",
                want_dir=False,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_a_wanted_name_that_normalizes_to_nothing_matches_nothing(self) -> None:
        # The failure this refuses is the loud one: an empty normalization read as
        # a value would equal every other empty normalization in the folder.
        assert (
            matching_entries(
                (_entry("(USA).gba"), _entry("[!].gba")),
                wanted_name="",
                want_dir=False,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_an_entry_that_normalizes_to_nothing_is_not_a_candidate_either(self) -> None:
        assert (
            matching_entries(
                (_entry("(USA).gba"),),
                wanted_name="zelda",
                want_dir=False,
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )


class TestRankCandidates:
    def test_no_matches_yields_no_candidates(self) -> None:
        assert rank_candidates((), server_size=100, server_crc32="deadbeef", member_crc32s={}) == ((), False)

    def test_a_single_member_archive_whose_crc_agrees_ranks_on_the_checksum(self) -> None:
        entry = _entry("Mario (U).zip", size=999)
        candidates, truncated = rank_candidates(
            (entry,), server_size=100, server_crc32="deadbeef", member_crc32s={entry.path: ("deadbeef",)}
        )
        assert truncated is False
        assert candidates[0].evidence == CRC32_MATCH
        assert candidates[0].detail

    def test_an_exact_size_ranks_below_a_checksum_and_above_a_name(self) -> None:
        crc = _entry("A (U).zip", size=1)
        size = _entry("B (U).gba", size=100)
        name = _entry("C (U).gba", size=7)
        candidates, _truncated = rank_candidates(
            (name, size, crc), server_size=100, server_crc32="deadbeef", member_crc32s={crc.path: ("deadbeef",)}
        )
        assert [(c.name, c.evidence) for c in candidates] == [
            ("A (U).zip", CRC32_MATCH),
            ("B (U).gba", SIZE_MATCH),
            ("C (U).gba", NAME_MATCH),
        ]

    def test_rows_resting_on_the_same_evidence_keep_a_stable_order(self) -> None:
        entries = (_entry("Beta (U).gba"), _entry("Alpha (U).gba"))
        candidates, _truncated = rank_candidates(entries, server_size=0, server_crc32="", member_crc32s={})
        assert [c.name for c in candidates] == ["Alpha (U).gba", "Beta (U).gba"]

    def test_a_crc_that_disagrees_is_no_evidence_at_all(self) -> None:
        entry = _entry("Mario (U).zip", size=7)
        candidates, _truncated = rank_candidates(
            (entry,), server_size=100, server_crc32="deadbeef", member_crc32s={entry.path: ("0badf00d",)}
        )
        assert candidates[0].evidence == NAME_MATCH

    def test_a_multi_member_archive_cannot_claim_the_server_s_one_number(self) -> None:
        # RomM's file-level digest for such an archive is a composite this plugin
        # cannot attribute to any single member (ADR-0028), so agreement with one
        # of them proves nothing and must not be printed as if it did.
        entry = _entry("Set (U).zip", size=7)
        candidates, _truncated = rank_candidates(
            (entry,),
            server_size=100,
            server_crc32="deadbeef",
            member_crc32s={entry.path: ("deadbeef", "0badf00d")},
        )
        assert candidates[0].evidence == NAME_MATCH

    def test_a_server_without_a_checksum_falls_through_to_size(self) -> None:
        entry = _entry("Mario (U).zip", size=100)
        candidates, _truncated = rank_candidates(
            (entry,), server_size=100, server_crc32="", member_crc32s={entry.path: ("deadbeef",)}
        )
        assert candidates[0].evidence == SIZE_MATCH

    def test_a_server_without_a_size_falls_through_to_the_name(self) -> None:
        candidates, _truncated = rank_candidates(
            (_entry("Mario (U).zip", size=0),), server_size=0, server_crc32="", member_crc32s={}
        )
        assert candidates[0].evidence == NAME_MATCH

    def test_a_directory_never_claims_a_size_match(self) -> None:
        # The search does not descend, so a directory's ``size_bytes`` is 0 and an
        # ``fs_size_bytes`` of 0 must not read as two zeroes agreeing.
        candidates, _truncated = rank_candidates(
            (_entry("Final Fantasy VII (U)", is_dir=True),), server_size=0, server_crc32="", member_crc32s={}
        )
        assert candidates[0].evidence == NAME_MATCH

    def test_a_capped_list_says_so(self) -> None:
        entries = tuple(_entry(f"Game ({index:03d}).gba") for index in range(CANDIDATE_LIMIT + 3))
        candidates, truncated = rank_candidates(entries, server_size=0, server_crc32="", member_crc32s={})
        assert len(candidates) == CANDIDATE_LIMIT
        assert truncated is True

    def test_a_list_that_fits_is_not_reported_as_cut(self) -> None:
        entries = tuple(_entry(f"Game ({index:03d}).gba") for index in range(CANDIDATE_LIMIT))
        candidates, truncated = rank_candidates(entries, server_size=0, server_crc32="", member_crc32s={})
        assert len(candidates) == CANDIDATE_LIMIT
        assert truncated is False


class TestCandidatesRefusal:
    def test_the_refusal_carries_the_canonical_failure_shape(self) -> None:
        candidates, truncated = rank_candidates(
            (_entry("Mario (U).gba", size=100),), server_size=100, server_crc32="", member_crc32s={}
        )
        refusal = candidates_refusal(
            candidates, truncated=truncated, incoming_name="Mario (USA).gba", incoming_size=100
        )
        assert refusal["success"] is False
        assert refusal["reason"] == "adoption_candidates"
        assert isinstance(refusal["message"], str)
        assert refusal["message"]
        assert "error" not in refusal
        assert "error_code" not in refusal

    def test_every_field_the_dialog_renders_crosses_the_wire(self) -> None:
        candidates, truncated = rank_candidates(
            (_entry("Mario (U).gba", size=100),), server_size=100, server_crc32="", member_crc32s={}
        )
        refusal = candidates_refusal(
            candidates, truncated=truncated, incoming_name="Mario (USA).gba", incoming_size=100
        )
        assert refusal["candidates"] == [
            {
                "name": "Mario (U).gba",
                "path": "/roms/gba/Mario (U).gba",
                "is_dir": False,
                "size_bytes": 100,
                "modified_at": 1700000000.0,
                "evidence": SIZE_MATCH,
                "detail": candidates[0].detail,
            }
        ]
        assert refusal["incoming"] == {"name": "Mario (USA).gba", "size_bytes": 100}
        assert refusal["truncated"] is False

    def test_a_truncated_list_is_stated_rather_than_implied(self) -> None:
        entries = tuple(_entry(f"Game ({index:03d}).gba") for index in range(CANDIDATE_LIMIT + 1))
        candidates, truncated = rank_candidates(entries, server_size=0, server_crc32="", member_crc32s={})
        refusal = candidates_refusal(candidates, truncated=truncated, incoming_name="Game.gba", incoming_size=0)
        assert refusal["truncated"] is True
