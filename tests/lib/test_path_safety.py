"""Tests for lib.path_safety — path containment predicates with realpath resolution."""

import os

from lib.path_safety import is_safe_rom_path


class TestIsSafeRomPath:
    def test_path_inside_roms_dir_is_safe(self, tmp_path):
        roms_base = str(tmp_path / "retrodeck" / "roms")
        safe = str(tmp_path / "retrodeck" / "roms" / "n64" / "game.z64")
        assert is_safe_rom_path(safe, roms_base) is True

    def test_path_outside_roms_dir_is_not_safe(self, tmp_path):
        roms_base = str(tmp_path / "retrodeck" / "roms")
        outside = str(tmp_path / "evil" / "game.z64")
        assert is_safe_rom_path(outside, roms_base) is False

    def test_roms_base_itself_is_not_safe(self, tmp_path):
        roms_base = str(tmp_path / "retrodeck" / "roms")
        # Only 1 level deep — must be at least 2
        base = str(tmp_path / "retrodeck" / "roms" / "n64")
        assert is_safe_rom_path(base, roms_base) is False

    def test_etc_passwd_is_not_safe(self, tmp_path):
        roms_base = str(tmp_path / "retrodeck" / "roms")
        assert is_safe_rom_path("/etc/passwd", roms_base) is False

    def test_deeper_than_two_levels_is_safe(self, tmp_path):
        roms_base = str(tmp_path / "retrodeck" / "roms")
        deep = str(tmp_path / "retrodeck" / "roms" / "gb" / "sub" / "file.zip")
        assert is_safe_rom_path(deep, roms_base) is True

    def test_exactly_two_levels_is_safe(self, tmp_path):
        roms_base = str(tmp_path / "retrodeck" / "roms")
        two_levels = str(tmp_path / "retrodeck" / "roms" / "gb" / "file.zip")
        assert is_safe_rom_path(two_levels, roms_base) is True

    def test_one_level_deep_is_not_safe(self, tmp_path):
        roms_base = str(tmp_path / "retrodeck" / "roms")
        one_level = str(tmp_path / "retrodeck" / "roms" / "file.zip")
        assert is_safe_rom_path(one_level, roms_base) is False

    def test_empty_roms_base_returns_quirky_cwd_match(self):
        # Preserved quirk: empty roms_base resolves to cwd via os.path.realpath("").
        cwd = os.path.realpath(os.getcwd())
        path_two_levels_in_cwd = os.path.join(cwd, "a", "b")
        assert is_safe_rom_path(path_two_levels_in_cwd, "") is True
