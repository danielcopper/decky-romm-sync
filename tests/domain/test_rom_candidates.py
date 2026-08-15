"""domain.rom_candidates — the name match and the evidence behind each offer.

The normalization is the whole filter: everything downstream trusts that two
names denoting the same game reduce to one string and two names denoting
different games do not. The cases here are the ones ROM filenames produce —
region tags, revision tags, dump-status brackets, unicode titles — plus the
degenerate ones that would silently match everything if they were not refused.
"""

from __future__ import annotations

import pytest

from domain.rom_candidates import (
    CANDIDATE_LIMIT,
    CRC32_MATCH,
    DIR,
    FILE,
    LINK,
    NAME_MATCH,
    SIZE_MATCH,
    Kind,
    LocalEntry,
    LocalName,
    candidates_refusal,
    matching_entries,
    normalize_rom_name,
    rank_candidates,
    unusable_namesake_refusal,
    vanished_candidate_refusal,
)

_ACCEPTED = frozenset({".gba", ".zip", ".sfc"})


def _entry(name: str, *, kind: Kind = FILE, size: int = 0, directory: str = "/roms/gba") -> LocalEntry:
    return LocalEntry(
        name=name,
        path=f"{directory}/{name}",
        kind=kind,
        size_bytes=size,
        modified_at=1700000000.0,
    )


def _name(name: str, *, kind: Kind = FILE, directory: str = "/roms/gba") -> LocalName:
    """What a bare directory read knows — the page's half of the search works on this."""
    return LocalName(name=name, path=f"{directory}/{name}", kind=kind)


class TestNormalizeRomName:
    def test_a_region_tag_is_not_part_of_the_game(self) -> None:
        assert normalize_rom_name("Example Quest - Second Journey (USA).zip") == "example quest second journey"

    def test_two_names_of_the_same_game_reduce_to_one_string(self) -> None:
        assert normalize_rom_name("Example Quest - Second Journey (U).zip") == normalize_rom_name(
            "Example Quest - Second Journey (Rev 1) (USA).gba"
        )

    def test_several_tag_groups_all_go(self) -> None:
        assert normalize_rom_name("Example Quest (USA) (Rev 2) (Virtual Console).zip") == "example quest"

    def test_square_brackets_are_tags_too(self) -> None:
        assert normalize_rom_name("Example Quest [!][b1].sfc") == "example quest"

    def test_parens_and_brackets_mix(self) -> None:
        assert normalize_rom_name("Example Quest (USA) [!].sfc") == "example quest"

    def test_a_bracket_nested_in_a_paren_closes_with_it(self) -> None:
        assert normalize_rom_name("Example Quest (Rev [1]) Second Journey.sfc") == "example quest second journey"

    def test_punctuation_collapses_to_single_spaces_and_case_is_dropped(self) -> None:
        assert normalize_rom_name("EXAMPLE___Quest--Second!!.sfc") == "example quest second"

    def test_leading_and_trailing_punctuation_is_trimmed(self) -> None:
        assert normalize_rom_name("  ...Example Quest... .sfc") == "example quest"

    def test_a_unicode_title_survives_intact(self) -> None:
        assert normalize_rom_name("Exemple Quête Édition Rouge (France).gba") == "exemple quête édition rouge"

    def test_a_non_latin_title_survives_intact(self) -> None:
        assert normalize_rom_name("テストゲーム (Japan).gba") == "テストゲーム"

    def test_a_name_that_is_only_tags_normalizes_to_nothing(self) -> None:
        assert normalize_rom_name("(USA).zip") == ""

    def test_a_name_that_is_only_punctuation_normalizes_to_nothing(self) -> None:
        assert normalize_rom_name("---.zip") == ""

    def test_an_unmatched_opener_swallows_the_rest_rather_than_keeping_the_tag(self) -> None:
        assert normalize_rom_name("Example Quest (USA.sfc") == "example quest"

    def test_an_unmatched_closer_is_dropped(self) -> None:
        assert normalize_rom_name("Example Quest) Second Journey.sfc") == "example quest second journey"

    def test_only_the_last_extension_comes_off(self) -> None:
        # ``splitext`` is deliberately blunt. Symmetric on both sides of the
        # comparison, which is what makes it safe; the cost is pinned here.
        assert normalize_rom_name("Game.tar.gz") == "game tar"

    def test_a_dotted_version_loses_its_last_component_on_both_sides(self) -> None:
        assert normalize_rom_name("Example Quest 3.0") == "example quest 3"

    def test_an_es_de_collapsed_directory_name_loses_its_extension(self) -> None:
        # The plugin's own multi-file installs are named ``<game>.m3u/`` so ES-DE
        # collapses them; a hand-made directory carries no extension. Both have
        # to reduce to the same game.
        assert normalize_rom_name("Example Quest - Second Journey (USA).m3u") == normalize_rom_name(
            "Example Quest - Second Journey (U)"
        )


class TestMatchingEntries:
    def test_an_empty_platform_directory_yields_nothing(self) -> None:
        assert (
            matching_entries(
                (),
                wanted_names=frozenset({"example quest second journey"}),
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_nothing_matches_a_different_game(self) -> None:
        entries = (_entry("Other Game (USA).gba"), _entry("Third Game (USA).gba"))
        assert (
            matching_entries(
                entries,
                wanted_names=frozenset({"example quest second journey"}),
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_the_same_game_under_another_name_matches(self) -> None:
        wanted = _entry("Example Quest - Second Journey (U).zip")
        found = matching_entries(
            (wanted, _entry("Other Game (USA).gba")),
            wanted_names=frozenset({"example quest second journey"}),
            accepted_extensions=_ACCEPTED,
            covered_paths=frozenset(),
        )
        assert found == (wanted,)

    def test_any_of_several_wanted_names_matches(self) -> None:
        # The caller derives more than one name for one ROM — the path the
        # download built, the inner file's name, ``fs_name`` — and a copy on disk
        # may be under any of them.
        by_derived = _entry("Inner Disc (U).zip")
        by_fs_name = _entry("Example Quest - Second Journey (U).zip")
        found = matching_entries(
            (by_derived, by_fs_name, _entry("Other Game (USA).gba")),
            wanted_names=frozenset({"inner disc", "example quest second journey"}),
            accepted_extensions=_ACCEPTED,
            covered_paths=frozenset(),
        )
        assert set(found) == {by_derived, by_fs_name}

    def test_the_page_s_leaner_entries_go_through_the_very_same_filter(self) -> None:
        # The two halves of the search agree because one filter answers for both
        # — the page just hands it entries it paid less to read.
        wanted = _name("Example Quest - Second Journey (U).zip")
        found = matching_entries(
            (wanted, _name("Other Game (USA).gba")),
            wanted_names=frozenset({"example quest second journey"}),
            accepted_extensions=_ACCEPTED,
            covered_paths=frozenset(),
        )
        assert found == (wanted,)

    def test_an_entry_an_install_row_accounts_for_is_another_game_s_content(self) -> None:
        covered = _entry("Example Quest - Second Journey (U).zip")
        assert (
            matching_entries(
                (covered,),
                wanted_names=frozenset({"example quest second journey"}),
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset({covered.path}),
            )
            == ()
        )

    def test_an_extension_the_system_does_not_accept_is_not_a_rom(self) -> None:
        assert (
            matching_entries(
                (_entry("Example Quest - Second Journey (U).txt"),),
                wanted_names=frozenset({"example quest second journey"}),
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
                    wanted_names=frozenset({name}),
                    accepted_extensions=_ACCEPTED,
                    covered_paths=frozenset(),
                )
                == ()
            )

    def test_an_accept_list_that_could_not_answer_does_not_turn_the_search_off(self) -> None:
        # An empty set means ES-DE could not be read. Every other consumer of that
        # list treats it as "cannot tell" and takes its permissive branch; the
        # name match plus the user's confirmation is what the offer rests on.
        wanted = _entry("Example Quest - Second Journey (U).xyz")
        assert matching_entries(
            (wanted,),
            wanted_names=frozenset({"example quest second journey"}),
            accepted_extensions=frozenset(),
            covered_paths=frozenset(),
        ) == (wanted,)

    def test_a_directory_is_never_extension_tested(self) -> None:
        wanted = _entry("Example Quest - Second Journey (U)", kind=DIR)
        assert matching_entries(
            (wanted,),
            wanted_names=frozenset({"example quest second journey"}),
            accepted_extensions=_ACCEPTED,
            covered_paths=frozenset(),
        ) == (wanted,)

    def test_every_kind_comes_back_and_the_caller_decides(self) -> None:
        # Shape is not filtered here. "It is the wrong shape" and "it is a link"
        # are things to tell the user about, so the filter's job ends at the
        # name and the caller sorts out what each one means.
        entries = (
            _entry("Example Quest (U).gba"),
            _entry("Example Quest (E)", kind=DIR),
            _entry("Example Quest (J).gba", kind=LINK),
        )
        assert (
            matching_entries(
                entries,
                wanted_names=frozenset({"example quest"}),
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == entries
        )

    def test_a_link_is_still_extension_tested(self) -> None:
        # It is judged as what it is — not a directory — so the accept-list
        # applies exactly as it does to a file, and a link named like notes is
        # not a namesake worth mentioning.
        assert (
            matching_entries(
                (_entry("Example Quest (U).txt", kind=LINK),),
                wanted_names=frozenset({"example quest"}),
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
                wanted_names=frozenset(),
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_an_empty_string_among_the_wanted_names_is_dropped_not_matched(self) -> None:
        # The reachable shape of the case above: the caller derives several names
        # for one ROM and one of them normalizes away, so the set is non-empty but
        # holds "". Read as a value it equals every entry whose own name is only
        # tags, and the folder's tag-only files all become candidates for this
        # game. Passing an empty *set* does not reach this — that is refused one
        # line earlier — so this is the only witness the filter has.
        assert (
            matching_entries(
                (_entry("(USA).gba"), _entry("[!].gba")),
                wanted_names=frozenset({"", "example quest"}),
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )

    def test_dropping_the_empty_name_leaves_the_real_ones_matching(self) -> None:
        # The other half: the filter drops "" rather than giving up on the set.
        wanted = _entry("Example Quest (USA).gba")
        assert matching_entries(
            (wanted, _entry("(USA).gba")),
            wanted_names=frozenset({"", "example quest"}),
            accepted_extensions=_ACCEPTED,
            covered_paths=frozenset(),
        ) == (wanted,)

    def test_an_entry_that_normalizes_to_nothing_is_not_a_candidate_either(self) -> None:
        assert (
            matching_entries(
                (_entry("(USA).gba"),),
                wanted_names=frozenset({"other game"}),
                accepted_extensions=_ACCEPTED,
                covered_paths=frozenset(),
            )
            == ()
        )


class TestRankCandidates:
    def test_no_matches_yields_no_candidates(self) -> None:
        assert rank_candidates((), server_size=100, server_crc32="deadbeef", member_crc32s={}) == ((), False)

    def test_a_single_member_archive_whose_crc_agrees_ranks_on_the_checksum(self) -> None:
        entry = _entry("Example Quest (U).zip", size=999)
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
        entry = _entry("Example Quest (U).zip", size=7)
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
        entry = _entry("Example Quest (U).zip", size=100)
        candidates, _truncated = rank_candidates(
            (entry,), server_size=100, server_crc32="", member_crc32s={entry.path: ("deadbeef",)}
        )
        assert candidates[0].evidence == SIZE_MATCH

    def test_a_server_without_a_size_falls_through_to_the_name(self) -> None:
        candidates, _truncated = rank_candidates(
            (_entry("Example Quest (U).zip", size=0),), server_size=0, server_crc32="", member_crc32s={}
        )
        assert candidates[0].evidence == NAME_MATCH

    def test_a_directory_never_claims_a_size_match(self) -> None:
        # The search does not descend, so a directory's ``size_bytes`` is 0 and an
        # ``fs_size_bytes`` of 0 must not read as two zeroes agreeing.
        candidates, _truncated = rank_candidates(
            (_entry("Example Quest - Second Journey (U)", kind=DIR),),
            server_size=0,
            server_crc32="",
            member_crc32s={},
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
            (_entry("Example Quest (U).gba", size=100),), server_size=100, server_crc32="", member_crc32s={}
        )
        refusal = candidates_refusal(
            candidates, truncated=truncated, incoming_name="Example Quest (USA).gba", incoming_size=100
        )
        assert refusal["success"] is False
        assert refusal["reason"] == "adoption_candidates"
        assert isinstance(refusal["message"], str)
        assert refusal["message"]
        assert "error" not in refusal
        assert "error_code" not in refusal

    def test_every_field_the_dialog_renders_crosses_the_wire(self) -> None:
        candidates, truncated = rank_candidates(
            (_entry("Example Quest (U).gba", size=100),), server_size=100, server_crc32="", member_crc32s={}
        )
        refusal = candidates_refusal(
            candidates, truncated=truncated, incoming_name="Example Quest (USA).gba", incoming_size=100
        )
        assert refusal["candidates"] == [
            {
                "name": "Example Quest (U).gba",
                "path": "/roms/gba/Example Quest (U).gba",
                "is_dir": False,
                "size_bytes": 100,
                "modified_at": 1700000000.0,
                "evidence": SIZE_MATCH,
                "detail": candidates[0].detail,
            }
        ]
        assert refusal["incoming"] == {"name": "Example Quest (USA).gba", "size_bytes": 100}
        assert refusal["truncated"] is False

    def test_a_truncated_list_is_stated_rather_than_implied(self) -> None:
        entries = tuple(_entry(f"Game ({index:03d}).gba") for index in range(CANDIDATE_LIMIT + 1))
        candidates, truncated = rank_candidates(entries, server_size=0, server_crc32="", member_crc32s={})
        refusal = candidates_refusal(candidates, truncated=truncated, incoming_name="Game.gba", incoming_size=0)
        assert refusal["truncated"] is True


class TestUnusableNamesakeRefusal:
    """The namesake that cannot become this install: the other shape, or a link."""

    def test_the_refusal_carries_the_canonical_failure_shape(self) -> None:
        refusal = unusable_namesake_refusal(
            (_name("Example Quest (U)", kind=DIR),),
            served_dir=False,
            incoming_name="Example Quest (USA).gba",
            incoming_size=100,
        )
        assert refusal["success"] is False
        assert refusal["reason"] == "unusable_namesake"
        assert isinstance(refusal["message"], str)
        assert refusal["message"]
        assert "error" not in refusal
        assert "error_code" not in refusal

    def test_it_names_the_entry_and_both_shapes(self) -> None:
        refusal = unusable_namesake_refusal(
            (_name("Example Quest (U)", kind=DIR),),
            served_dir=False,
            incoming_name="Example Quest (USA).gba",
            incoming_size=100,
        )
        assert refusal["message"] == (
            "'Example Quest (U)' has this game's name but is a folder, and the server sends this game as a single file"
        )
        assert refusal["existing"] == [
            {"name": "Example Quest (U)", "path": "/roms/gba/Example Quest (U)", "kind": DIR}
        ]
        assert refusal["served_is_dir"] is False
        assert refusal["incoming"] == {"name": "Example Quest (USA).gba", "size_bytes": 100}
        assert refusal["truncated"] is False

    def test_the_other_direction_reads_the_other_way_round(self) -> None:
        refusal = unusable_namesake_refusal(
            (_name("Example Quest (U).cue"),),
            served_dir=True,
            incoming_name="Example Quest (USA)",
            incoming_size=0,
        )
        assert refusal["message"] == (
            "'Example Quest (U).cue' has this game's name but is a single file, "
            "and the server sends this game as a folder"
        )
        assert refusal["served_is_dir"] is True

    def test_a_link_is_named_for_what_it_is_rather_than_a_shape(self) -> None:
        # A symlink is not the wrong shape — it is the wrong *kind*, and would be
        # refused even where it resolves to exactly the right thing. So the
        # sentence must NOT end in the served shape: "…is a shortcut, and the
        # server sends this game as a single file" reads as though a folder-served
        # game would have taken the shortcut happily.
        refusal = unusable_namesake_refusal(
            (_name("Example Quest (U).gba", kind=LINK),),
            served_dir=False,
            incoming_name="Example Quest (USA).gba",
            incoming_size=0,
        )
        assert refusal["message"] == (
            "'Example Quest (U).gba' has this game's name but is a shortcut to somewhere else, "
            "which cannot be used as this game whatever it points at"
        )
        assert refusal["existing"] == [
            {"name": "Example Quest (U).gba", "path": "/roms/gba/Example Quest (U).gba", "kind": LINK}
        ]

    def test_several_of_one_kind_are_counted_and_still_named(self) -> None:
        refusal = unusable_namesake_refusal(
            (_name("Example Quest (U)", kind=DIR), _name("Example Quest (E)", kind=DIR)),
            served_dir=False,
            incoming_name="Example Quest (USA).gba",
            incoming_size=0,
        )
        assert refusal["message"] == (
            "2 folders here have this game's name, and the server sends this game as a single file"
        )

    def test_several_links_keep_the_reason_that_belongs_to_a_link(self) -> None:
        refusal = unusable_namesake_refusal(
            (_name("Example Quest (U).gba", kind=LINK), _name("Example Quest (E).gba", kind=LINK)),
            served_dir=False,
            incoming_name="Example Quest (USA).gba",
            incoming_size=0,
        )
        assert refusal["message"] == (
            "2 shortcuts to somewhere else here have this game's name, "
            "which cannot be used as this game whatever it points at"
        )

    def test_a_mixed_list_names_no_kind_and_no_shape(self) -> None:
        # Two kinds with two different reasons, so the sentence claims neither —
        # the dialog labels each row and that is where the detail belongs.
        refusal = unusable_namesake_refusal(
            (_name("Example Quest (U)", kind=DIR), _name("Example Quest (E).gba", kind=LINK)),
            served_dir=False,
            incoming_name="Example Quest (USA).gba",
            incoming_size=0,
        )
        assert refusal["message"] == "2 entries here have this game's name, and none of them can be used as this game"
        assert refusal["existing"] == [
            {"name": "Example Quest (U)", "path": "/roms/gba/Example Quest (U)", "kind": DIR},
            {"name": "Example Quest (E).gba", "path": "/roms/gba/Example Quest (E).gba", "kind": LINK},
        ]

    def test_a_capped_list_counts_what_was_found_and_claims_no_kind_for_it(self) -> None:
        # The count is what was found; the kind is only what was looked at. Naming
        # the shown kind here would claim the entries beyond the cap were folders
        # too, which nothing read.
        entries = tuple(_name(f"Example Quest ({index:03d})", kind=DIR) for index in range(CANDIDATE_LIMIT + 3))
        refusal = unusable_namesake_refusal(
            entries, served_dir=False, incoming_name="Example Quest.gba", incoming_size=0
        )
        assert refusal["message"] == (
            f"{CANDIDATE_LIMIT + 3} entries here have this game's name, and none of them can be used as this game"
        )
        assert refusal["existing"] == [
            {"name": entry.name, "path": entry.path, "kind": DIR} for entry in entries[:CANDIDATE_LIMIT]
        ]
        assert refusal["truncated"] is True

    def test_it_refuses_to_describe_an_empty_set(self) -> None:
        with pytest.raises(ValueError):
            unusable_namesake_refusal((), served_dir=False, incoming_name="Example Quest.gba", incoming_size=0)


class TestVanishedCandidateRefusal:
    """The backstop's own sentence."""

    def test_it_carries_the_canonical_failure_shape_and_claims_no_cause(self) -> None:
        refusal = vanished_candidate_refusal(incoming_name="Example Quest (USA).gba", incoming_size=100)
        assert refusal["success"] is False
        assert refusal["reason"] == "candidate_vanished"
        assert refusal["incoming"] == {"name": "Example Quest (USA).gba", "size_bytes": 100}
        # No list: nothing was found, and naming a cause would be a guess.
        assert "existing" not in refusal
