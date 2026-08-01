"""Unit tests for ``domain.game_instance`` — matching a live tree to a ROM.

The kernel behind Stop Game's targeting. Its safety property is asymmetric: a
false negative costs a refused stop, a false positive ends another game
mid-save. So the exact-path pass must win over the fallback, and the fallback
must never widen into a substring test.
"""

from __future__ import annotations

from domain.game_instance import (
    BASENAME_DISCRIMINATOR,
    PATH_DISCRIMINATOR,
    GameInstance,
    match_instance_for_launch_path,
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


class TestBasenameFallback:
    def test_a_differently_rooted_path_with_the_same_basename_matches(self) -> None:
        # The sandbox may expose the ROM under a different absolute path than the
        # host one the launch command was baked from.
        sandboxed = _instance(101, "retroarch", "/run/media/mmcblk0p1/roms/psx/ours.chd")

        match = match_instance_for_launch_path([sandboxed], OURS)

        assert match is not None
        assert match.instance is sandboxed
        assert match.discriminator == BASENAME_DISCRIMINATOR

    def test_a_basename_that_is_a_substring_of_another_does_not_match(self) -> None:
        # "a.bin" must not match "aaa.bin" — the fallback compares whole
        # basenames, which is what keeps it from ending the wrong game.
        other = _instance(201, "retroarch", "/roms/nes/aaa.bin")

        assert match_instance_for_launch_path([other], "/roms/nes/a.bin") is None

    def test_a_bare_basename_token_matches(self) -> None:
        # An emulator invoked from inside the ROM's own directory passes the
        # filename alone.
        relative = _instance(101, "retroarch", "ours.chd")

        match = match_instance_for_launch_path([relative], OURS)

        assert match is not None
        assert match.discriminator == BASENAME_DISCRIMINATOR

    def test_a_folder_launch_target_matches_on_its_directory_name(self) -> None:
        # A folder-boot ROM (PS3/RPCS3) bakes a directory, not a file.
        folder = "/home/deck/retrodeck/roms/ps3/MyGame"
        instance = _instance(101, "rpcs3", "--no-gui", "/run/media/roms/ps3/MyGame")

        match = match_instance_for_launch_path([instance], folder)

        assert match is not None
        assert match.discriminator == BASENAME_DISCRIMINATOR


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
