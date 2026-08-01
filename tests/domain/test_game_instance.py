"""Unit tests for ``domain.game_instance`` — matching a live tree to a ROM.

The kernel behind Stop Game's targeting. Its safety property is asymmetric: a
false negative costs a refused stop, a false positive ends another game
mid-save. So the exact-path pass must win over the fallback, and the fallback
must never widen into a substring test.
"""

from __future__ import annotations

from domain.game_instance import (
    PATH_DISCRIMINATOR,
    PATH_TAIL_DISCRIMINATOR,
    GameInstance,
    match_instance_for_launch_path,
    path_tail,
)

OURS = "/home/deck/retrodeck/roms/psx/ours.chd"
THEIRS = "/home/deck/retrodeck/roms/snes/theirs.sfc"


def _instance(pid: int, *argv: str) -> GameInstance:
    return GameInstance(pids=(pid,), argv=argv)


class TestExactPathMatch:
    def test_the_instance_whose_argv_holds_the_path_is_returned(self) -> None:
        theirs = _instance(201, "retroarch", THEIRS)
        ours = _instance(101, "duckstation-qt", OURS)

        match = match_instance_for_launch_path([theirs, ours], OURS)

        assert match is not None
        assert match.instance is ours
        assert match.discriminator == PATH_DISCRIMINATOR

    def test_a_path_that_is_only_part_of_a_token_does_not_match(self) -> None:
        # A token that merely CONTAINS the path (a longer path below it) is a
        # different file — token equality, never a substring test.
        other = _instance(201, "retroarch", f"{OURS}.bak")

        assert match_instance_for_launch_path([other], OURS) is None

    def test_the_first_exact_match_wins_over_a_later_basename_match(self) -> None:
        exact = _instance(101, OURS)
        same_name = _instance(201, "/run/media/mmcblk0p1/roms/psx/ours.chd")

        # Both orders resolve to the exact one: the whole first pass runs before
        # the fallback pass starts.
        assert match_instance_for_launch_path([same_name, exact], OURS).instance is exact  # type: ignore[union-attr]
        assert match_instance_for_launch_path([exact, same_name], OURS).instance is exact  # type: ignore[union-attr]


class TestPathTail:
    def test_the_tail_is_the_parent_directory_and_the_file(self) -> None:
        assert path_tail("/home/deck/retrodeck/roms/snes/Aladdin.zip") == "snes/Aladdin.zip"

    def test_a_path_without_a_parent_yields_the_file_alone(self) -> None:
        assert path_tail("Aladdin.zip") == "Aladdin.zip"
        assert path_tail("/Aladdin.zip") == "Aladdin.zip"


class TestPathTailFallback:
    def test_a_differently_rooted_path_with_the_same_tail_matches(self) -> None:
        # The sandbox may expose the ROM under a different absolute path than the
        # host one the launch command was baked from. Only the ROOT differs, so
        # the platform directory + filename still line up.
        sandboxed = _instance(101, "retroarch", "/run/media/mmcblk0p1/roms/psx/ours.chd")

        match = match_instance_for_launch_path([sandboxed], OURS)

        assert match is not None
        assert match.instance is sandboxed
        assert match.discriminator == PATH_TAIL_DISCRIMINATOR

    def test_a_filename_that_is_a_substring_of_another_does_not_match(self) -> None:
        # "a.bin" must not match "aaa.bin" — the fallback compares whole path
        # components, which is what keeps it from ending the wrong game.
        other = _instance(201, "retroarch", "/roms/nes/aaa.bin")

        assert match_instance_for_launch_path([other], "/roms/nes/a.bin") is None

    def test_the_same_filename_under_another_platform_does_not_match(self) -> None:
        # THE cross-platform collision: one game, two platform directories, one
        # filename. A bare-filename fallback would call this a hit and SIGTERM
        # the Genesis session while the SNES one was meant.
        genesis = _instance(201, "retroarch", "/run/media/roms/genesis/Aladdin.zip")

        assert match_instance_for_launch_path([genesis], "/home/deck/roms/snes/Aladdin.zip") is None

    def test_a_bare_filename_token_does_not_match(self) -> None:
        # A token with no parent component cannot be attributed to a platform
        # directory, so it is refused rather than matched on the filename alone.
        relative = _instance(101, "retroarch", "ours.chd")

        assert match_instance_for_launch_path([relative], OURS) is None

    def test_a_folder_launch_target_matches_on_its_own_tail(self) -> None:
        # A folder-boot ROM (PS3/RPCS3) bakes a directory, not a file.
        folder = "/home/deck/retrodeck/roms/ps3/MyGame"
        instance = _instance(101, "rpcs3", "--no-gui", "/run/media/roms/ps3/MyGame")

        match = match_instance_for_launch_path([instance], folder)

        assert match is not None
        assert match.discriminator == PATH_TAIL_DISCRIMINATOR


class TestAmbiguityIsRefused:
    """The fallback refuses rather than resolving a tie by scan order.

    A tail collision is not supposed to be reachable — that is what the parent
    component buys over a bare filename — but "not supposed to be" is not a
    guarantee, and the cost of being wrong is another game killed mid-save. The
    fallback is a weak signal, so it only ever speaks when it speaks alone.
    """

    def test_two_instances_matching_the_same_tail_signal_nothing(self) -> None:
        # Same platform directory name under two different roots (an SD card and
        # internal storage, say), each running its own copy.
        first = _instance(101, "retroarch", "/run/media/sd/roms/psx/ours.chd")
        second = _instance(201, "retroarch", "/home/deck/other/roms/psx/ours.chd")

        assert match_instance_for_launch_path([first, second], OURS) is None
        # And the order of the candidates cannot change that answer.
        assert match_instance_for_launch_path([second, first], OURS) is None

    def test_an_exact_hit_still_wins_over_an_ambiguous_tail(self) -> None:
        # The ambiguity refusal must not swallow a match the exact pass already
        # made: the first pass completes before the fallback is consulted.
        exact = _instance(101, OURS)
        tail_a = _instance(201, "/run/media/sd/roms/psx/ours.chd")
        tail_b = _instance(301, "/home/deck/other/roms/psx/ours.chd")

        match = match_instance_for_launch_path([tail_a, tail_b, exact], OURS)

        assert match is not None
        assert match.instance is exact
        assert match.discriminator == PATH_DISCRIMINATOR

    def test_one_instance_matching_twice_over_is_not_ambiguous(self) -> None:
        # Ambiguity is between INSTANCES, not between tokens: the launcher shell
        # and the emulator below it both carry the path, which is one candidate.
        instance = _instance(101, "run_game.sh", "/run/media/roms/psx/ours.chd")
        both_tokens = GameInstance(pids=(102, 101), argv=(*instance.argv, "/run/media/roms/psx/ours.chd"))

        match = match_instance_for_launch_path([both_tokens], OURS)

        assert match is not None
        assert match.instance is both_tokens


class TestNoMatch:
    def test_an_unrelated_instance_is_never_returned(self) -> None:
        assert match_instance_for_launch_path([_instance(201, "retroarch", THEIRS)], OURS) is None

    def test_no_instances_at_all_is_no_match(self) -> None:
        assert match_instance_for_launch_path([], OURS) is None

    def test_an_empty_launch_path_matches_nothing(self) -> None:
        # The unresolvable-ROM case. "Nothing to compare" must never degrade to
        # "match whatever is running".
        assert match_instance_for_launch_path([_instance(101, "retroarch", OURS)], "") is None

    def test_a_path_that_is_all_separators_matches_nothing(self) -> None:
        # basename("/") is "" — an empty basename must not match every token
        # whose own basename happens to be empty.
        assert match_instance_for_launch_path([_instance(101, "retroarch", "/roms/")], "/") is None

    def test_an_instance_with_no_argv_at_all_matches_nothing(self) -> None:
        # Every process's command line was unreadable — unidentifiable, so it is
        # never claimed as this ROM's.
        assert match_instance_for_launch_path([GameInstance(pids=(101,), argv=())], OURS) is None
