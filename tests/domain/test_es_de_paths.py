"""Tests for py_modules/domain/es_de_paths.py"""

from __future__ import annotations

import os

import pytest

from domain.es_de_paths import gamelist_entry_path, normalize_gamelist_path

# ---------------------------------------------------------------------------
# gamelist_entry_path
# ---------------------------------------------------------------------------


def test_single_file_rom_uses_basename_when_rom_dir_is_none() -> None:
    assert gamelist_entry_path("/roms/snes/Mario.sfc", None) == "Mario.sfc"


def test_single_file_rom_uses_basename_when_rom_dir_is_empty_string() -> None:
    # Empty string is falsy — same single-file branch as None.
    assert gamelist_entry_path("/roms/snes/Mario.sfc", "") == "Mario.sfc"


def test_folder_backed_rom_joins_dir_basename_with_relative_launch_file() -> None:
    assert gamelist_entry_path("/roms/ps1/FF7/FF7.m3u", "/roms/ps1/FF7") == os.path.join("FF7", "FF7.m3u")


def test_folder_backed_rom_with_nested_launch_file_keeps_subdir() -> None:
    # Launch file sits in a subdirectory of the dedicated rom_dir.
    result = gamelist_entry_path("/roms/ps1/FF7/disc/FF7.cue", "/roms/ps1/FF7")
    assert result == os.path.join("FF7", "disc", "FF7.cue")


def test_escape_outside_rom_dir_falls_back_to_basename() -> None:
    # Data inconsistency: file_path is not under rom_dir, so relpath would
    # escape with "..". Defensive fallback returns the bare basename.
    result = gamelist_entry_path("/roms/snes/Mario.sfc", "/roms/ps1/FF7")
    assert result == "Mario.sfc"


def test_escape_one_level_up_falls_back_to_basename() -> None:
    # file_path is the parent of rom_dir → relpath == ".." exactly.
    result = gamelist_entry_path("/roms/ps1/FF7.m3u", "/roms/ps1/FF7")
    assert result == "FF7.m3u"


def test_trailing_slash_rom_dir_still_yields_dedicated_dir_segment() -> None:
    # A trailing separator on rom_dir must not drop the dedicated-dir segment
    # (basename of a trailing-slash path is "" — the #864 regression). The dir
    # is normalized before its basename is taken, so the identity is intact.
    result = gamelist_entry_path("/roms/ps1/FF7/FF7.m3u", "/roms/ps1/FF7/")
    assert result == os.path.join("FF7", "FF7.m3u")


def test_file_path_equal_to_rom_dir_falls_back_to_basename() -> None:
    # Degenerate input: file_path IS the rom_dir → relpath == "." exactly.
    # Falling back to basename avoids emitting a "<dir>/." identity.
    result = gamelist_entry_path("/roms/ps1/FF7", "/roms/ps1/FF7")
    assert result == "FF7"


def test_empty_file_path_with_rom_dir_returns_empty() -> None:
    # os.path.relpath("", rom_dir) raises ValueError — the early guard keeps
    # the function total, returning "" rather than propagating the exception.
    assert gamelist_entry_path("", "/roms/ps1/FF7") == ""


def test_empty_file_path_with_none_rom_dir_returns_empty() -> None:
    # Empty file_path short-circuits before the rom_dir branches.
    assert gamelist_entry_path("", None) == ""


# ---------------------------------------------------------------------------
# normalize_gamelist_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("./Mario.sfc", "Mario.sfc"),
        ("Mario.sfc", "Mario.sfc"),
        ("./FF7/FF7.m3u", "FF7/FF7.m3u"),
        # Prefix strip, NOT a charset strip: the hidden-file dot must survive.
        ("./.hidden", ".hidden"),
        ("", ""),
        ("  Mario.sfc  ", "Mario.sfc"),
        # Leading whitespace means the value does not start with "./", so only
        # the trailing whitespace is stripped — the "./" prefix is left intact
        # (prefix check runs before strip, per the contract).
        ("  ./Mario.sfc  ", "./Mario.sfc"),
    ],
)
def test_normalize_gamelist_path(raw: str, expected: str) -> None:
    assert normalize_gamelist_path(raw) == expected


def test_normalize_strips_only_one_leading_dot_slash() -> None:
    # A second "./" is part of the path, not a repeated prefix marker.
    assert normalize_gamelist_path("././Mario.sfc") == "./Mario.sfc"


def test_two_paths_are_equal_after_normalization() -> None:
    # Identity comparison: "./X" and "X" denote the same game.
    assert normalize_gamelist_path("./FF7/FF7.m3u") == normalize_gamelist_path("FF7/FF7.m3u")
