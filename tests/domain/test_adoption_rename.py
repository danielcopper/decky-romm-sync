"""domain.adoption_rename — the plan, computed in full before anything moves.

The load-bearing property here is completeness: every pair the adoption consists
of is known, and every collision among them is known, before the first file is
touched. A plan that missed a save would orphan it silently; a plan that claimed
a file belonging to another game would move someone else's save.
"""

from __future__ import annotations

from domain.adoption_rename import (
    KEEP,
    OVERWRITE,
    ROM,
    SAVE,
    SAVESTATE,
    CompanionDir,
    RenamePair,
    collision_refusal,
    pairs_for_choice,
    rename_pairs,
    split_collisions,
)

_OLD = "/roms/gba/Example Quest - Second Journey (U).zip"
_NEW = "/roms/gba/Example Quest - Second Journey (USA).zip"
_OLD_STEM = "Example Quest - Second Journey (U)"
_NEW_STEM = "Example Quest - Second Journey (USA)"


def _saves(*names: str, source: str = "/saves/gba", target: str = "/saves/gba") -> CompanionDir:
    return CompanionDir(kind=SAVE, source_dir=source, target_dir=target, names=names)


def _states(*names: str, source: str = "/states", target: str = "/states") -> CompanionDir:
    return CompanionDir(kind=SAVESTATE, source_dir=source, target_dir=target, names=names)


def _plan(*companions: CompanionDir) -> tuple[RenamePair, ...]:
    return rename_pairs(
        rom_source=_OLD,
        rom_target=_NEW,
        stem_source=_OLD_STEM,
        stem_target=_NEW_STEM,
        companions=companions,
    )


class TestRenamePairs:
    def test_the_rom_moves_even_with_no_saves_at_all(self) -> None:
        assert _plan() == (RenamePair(source=_OLD, target=_NEW, kind=ROM),)

    def test_the_rom_comes_first(self) -> None:
        assert _plan(_saves(f"{_OLD_STEM}.srm"))[0].kind == ROM

    def test_a_save_named_after_the_rom_travels_with_it(self) -> None:
        pairs = _plan(_saves(f"{_OLD_STEM}.srm"))
        assert pairs[1] == RenamePair(
            source=f"/saves/gba/{_OLD_STEM}.srm",
            target=f"/saves/gba/{_NEW_STEM}.srm",
            kind=SAVE,
        )

    def test_a_savestate_travels_out_of_its_own_directory(self) -> None:
        # The stock RetroDECK shape: savefiles content-sorted under ``saves/gba``,
        # savestates not sorted at all and sitting directly in ``states``.
        pairs = _plan(_saves(f"{_OLD_STEM}.srm"), _states(f"{_OLD_STEM}.state"))
        assert [(pair.kind, pair.source) for pair in pairs] == [
            (ROM, _OLD),
            (SAVE, f"/saves/gba/{_OLD_STEM}.srm"),
            (SAVESTATE, f"/states/{_OLD_STEM}.state"),
        ]

    def test_a_multi_suffix_savestate_keeps_its_whole_tail(self) -> None:
        pairs = _plan(_states(f"{_OLD_STEM}.state.auto", f"{_OLD_STEM}.state1"))
        assert [pair.target for pair in pairs[1:]] == [
            f"/states/{_NEW_STEM}.state.auto",
            f"/states/{_NEW_STEM}.state1",
        ]

    def test_another_game_s_save_is_never_claimed(self) -> None:
        assert _plan(_saves("Example Quest (U).srm", "Other Game (U).srm")) == (
            RenamePair(source=_OLD, target=_NEW, kind=ROM),
        )

    def test_a_stem_that_is_a_prefix_of_another_game_does_not_claim_it(self) -> None:
        # ``Example Quest`` must not take ``Example Quest - Second Journey``'s
        # saves; the separating dot is what makes the prefix test safe.
        pairs = rename_pairs(
            rom_source="/roms/gba/Example Quest (U).gba",
            rom_target="/roms/gba/Example Quest (USA).gba",
            stem_source="Example Quest (U)",
            stem_target="Example Quest (USA)",
            companions=(_saves(f"{_OLD_STEM}.srm", "Example Quest (U).srm"),),
        )
        assert [pair.source for pair in pairs[1:]] == ["/saves/gba/Example Quest (U).srm"]

    def test_a_file_named_exactly_the_stem_travels_too(self) -> None:
        pairs = _plan(_saves(_OLD_STEM))
        assert pairs[1].target == f"/saves/gba/{_NEW_STEM}"

    def test_an_empty_stem_claims_nothing(self) -> None:
        # A directory ROM with no launch file inside has no stem. As a prefix an
        # empty one would match every file in the save directory.
        pairs = rename_pairs(
            rom_source="/roms/psx/Game (U)",
            rom_target="/roms/psx/Game (USA)",
            stem_source="",
            stem_target="",
            companions=(_saves("anything.srm", "someone-elses.state"),),
        )
        assert len(pairs) == 1
        assert pairs[0].kind == ROM

    def test_a_content_sorted_directory_rom_moves_its_save_folder_not_its_filenames(self) -> None:
        # A multi-file ROM's launch file keeps its name while the directory that
        # names the content-sorted save folder changes — the mirror image of the
        # single-file case, produced by the same rule.
        pairs = rename_pairs(
            rom_source="/roms/psx/Game (U)",
            rom_target="/roms/psx/Game (USA)",
            stem_source="disc",
            stem_target="disc",
            companions=(_saves("disc.srm", source="/saves/Game (U)", target="/saves/Game (USA)"),),
        )
        assert pairs[1] == RenamePair(
            source="/saves/Game (U)/disc.srm",
            target="/saves/Game (USA)/disc.srm",
            kind=SAVE,
        )

    def test_a_pair_that_would_not_move_is_dropped(self) -> None:
        # Both directories and both stems equal: nothing to do, and staging a
        # self-rename as a hardlink would fail on its own target.
        pairs = rename_pairs(
            rom_source="/roms/psx/Game",
            rom_target="/roms/psx/Game (USA)",
            stem_source="disc",
            stem_target="disc",
            companions=(_saves("disc.srm"),),
        )
        assert [pair.kind for pair in pairs] == [ROM]

    def test_one_directory_listed_twice_contributes_each_file_once(self) -> None:
        both = "/saves/gba"
        pairs = _plan(
            _saves(f"{_OLD_STEM}.srm", source=both, target=both),
            _states(f"{_OLD_STEM}.srm", source=both, target=both),
        )
        assert [pair.source for pair in pairs[1:]] == [f"{both}/{_OLD_STEM}.srm"]

    def test_a_save_differing_only_in_extension_is_a_separate_pair(self) -> None:
        pairs = _plan(_saves(f"{_OLD_STEM}.srm", f"{_OLD_STEM}.rtc", f"{_OLD_STEM}.sav"))
        assert sorted(pair.target for pair in pairs[1:]) == [
            f"/saves/gba/{_NEW_STEM}.rtc",
            f"/saves/gba/{_NEW_STEM}.sav",
            f"/saves/gba/{_NEW_STEM}.srm",
        ]


class TestSplitCollisions:
    def test_no_collisions_leaves_every_pair_clear(self) -> None:
        pairs = _plan(_saves(f"{_OLD_STEM}.srm"))
        clear, colliding = split_collisions(pairs, frozenset())
        assert clear == pairs
        assert colliding == ()

    def test_one_taken_name_is_separated_from_the_rest(self) -> None:
        pairs = _plan(_saves(f"{_OLD_STEM}.srm"), _states(f"{_OLD_STEM}.state"))
        clear, colliding = split_collisions(pairs, frozenset({f"/saves/gba/{_NEW_STEM}.srm"}))
        assert [pair.kind for pair in clear] == [ROM, SAVESTATE]
        assert [pair.kind for pair in colliding] == [SAVE]

    def test_a_save_and_a_savestate_colliding_in_different_directories_are_both_reported(self) -> None:
        pairs = _plan(_saves(f"{_OLD_STEM}.srm"), _states(f"{_OLD_STEM}.state"))
        _clear, colliding = split_collisions(
            pairs,
            frozenset({f"/saves/gba/{_NEW_STEM}.srm", f"/states/{_NEW_STEM}.state"}),
        )
        assert [pair.target for pair in colliding] == [
            f"/saves/gba/{_NEW_STEM}.srm",
            f"/states/{_NEW_STEM}.state",
        ]

    def test_every_target_colliding_leaves_nothing_clear(self) -> None:
        pairs = _plan(_saves(f"{_OLD_STEM}.srm"))
        clear, colliding = split_collisions(pairs, frozenset(pair.target for pair in pairs))
        assert clear == ()
        assert colliding == pairs


class TestPairsForChoice:
    def test_overwrite_moves_everything(self) -> None:
        clear = (RenamePair("/a", "/b", ROM),)
        colliding = (RenamePair("/c", "/d", SAVE),)
        assert pairs_for_choice(clear, colliding, OVERWRITE) == clear + colliding

    def test_keep_moves_only_what_is_clear(self) -> None:
        clear = (RenamePair("/a", "/b", ROM),)
        colliding = (RenamePair("/c", "/d", SAVE),)
        assert pairs_for_choice(clear, colliding, KEEP) == clear

    def test_an_unanswered_collision_is_not_a_choice(self) -> None:
        assert pairs_for_choice((), (RenamePair("/c", "/d", SAVE),), "") is None

    def test_an_unrecognised_answer_is_refused_rather_than_guessed(self) -> None:
        assert pairs_for_choice((), (RenamePair("/c", "/d", SAVE),), "cancel") is None


class TestCollisionRefusal:
    def test_the_refusal_carries_the_canonical_failure_shape(self) -> None:
        refusal = collision_refusal((RenamePair("/saves/old.srm", "/saves/new.srm", SAVE),))
        assert refusal["success"] is False
        assert refusal["reason"] == "rename_collisions"
        assert isinstance(refusal["message"], str)
        assert refusal["message"]
        assert "error" not in refusal
        assert "error_code" not in refusal

    def test_every_collision_is_listed_not_just_the_first(self) -> None:
        refusal = collision_refusal(
            (
                RenamePair("/saves/a.srm", "/saves/new.srm", SAVE),
                RenamePair("/states/a.state", "/states/new.state", SAVESTATE),
            )
        )
        assert refusal["collisions"] == [
            {"name": "new.srm", "path": "/saves/new.srm", "kind": SAVE},
            {"name": "new.state", "path": "/states/new.state", "kind": SAVESTATE},
        ]

    def test_a_single_collision_is_named_in_the_message(self) -> None:
        refusal = collision_refusal((RenamePair("/saves/a.srm", "/saves/new.srm", SAVE),))
        assert "new.srm" in str(refusal["message"])
