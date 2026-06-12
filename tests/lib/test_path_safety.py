"""Tests for lib.path_safety — path containment predicates with realpath resolution."""

import os

import pytest

from lib.path_safety import (
    PathTraversalError,
    is_safe_rom_path,
    safe_join,
    safe_path_component,
)


class TestSafePathComponent:
    def test_accepts_clean_name(self):
        assert safe_path_component("game.z64") == "game.z64"

    def test_accepts_name_with_spaces_and_parens(self):
        # Decoded ZIP basenames commonly carry these — must stay valid.
        assert safe_path_component("Final Fantasy VII (USA).cue") == "Final Fantasy VII (USA).cue"

    def test_rejects_parent_dir(self):
        with pytest.raises(PathTraversalError):
            safe_path_component("..")

    def test_rejects_current_dir(self):
        with pytest.raises(PathTraversalError):
            safe_path_component(".")

    def test_rejects_empty(self):
        with pytest.raises(PathTraversalError):
            safe_path_component("")

    def test_rejects_nul_byte(self):
        with pytest.raises(PathTraversalError):
            safe_path_component("evil\x00.sh")

    def test_rejects_absolute_path(self):
        with pytest.raises(PathTraversalError):
            safe_path_component("/etc/passwd")

    def test_rejects_separator(self):
        # A single component must stay a single component.
        with pytest.raises(PathTraversalError):
            safe_path_component("a/b")

    def test_rejects_decoded_traversal(self):
        # ``%2e%2e%2fevil.sh`` decodes to this — the #968 attack vector.
        with pytest.raises(PathTraversalError):
            safe_path_component("../evil.sh")

    def test_rejects_embedded_traversal_segment(self):
        with pytest.raises(PathTraversalError):
            safe_path_component("sub/../../evil")


class TestSafeJoin:
    def test_accepts_legit_single_component(self, tmp_path):
        base = str(tmp_path / "bios")
        os.makedirs(base)
        result = safe_join(base, "scph5501.bin")
        assert result == os.path.realpath(os.path.join(base, "scph5501.bin"))

    def test_accepts_legit_multi_component(self, tmp_path):
        # The registry ``dc/dc_boot.bin`` shape must pass.
        base = str(tmp_path / "bios")
        os.makedirs(base)
        result = safe_join(base, "dc/dc_boot.bin")
        assert result == os.path.realpath(os.path.join(base, "dc", "dc_boot.bin"))

    def test_rejects_parent_traversal(self, tmp_path):
        base = str(tmp_path / "bios")
        os.makedirs(base)
        with pytest.raises(PathTraversalError):
            safe_join(base, "../evil.desktop")

    def test_rejects_deep_traversal(self, tmp_path):
        base = str(tmp_path / "retrodeck" / "roms")
        os.makedirs(base)
        with pytest.raises(PathTraversalError):
            safe_join(base, "../../etc/passwd")

    def test_rejects_absolute_second_arg(self, tmp_path):
        # os.path.join resets to an absolute part — must be caught.
        base = str(tmp_path / "bios")
        os.makedirs(base)
        with pytest.raises(PathTraversalError):
            safe_join(base, "/home/deck/.config/autostart/evil.desktop")

    def test_rejects_equality_with_base(self, tmp_path):
        # Strictly-below semantics: resolving exactly to base is rejected.
        base = str(tmp_path / "bios")
        os.makedirs(base)
        with pytest.raises(PathTraversalError):
            safe_join(base, ".")

    def test_rejects_symlink_escape(self, tmp_path):
        # A symlink planted UNDER base pointing OUTSIDE must not be a usable
        # escape — realpath resolves it, lexical normalization would not.
        base = tmp_path / "bios"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.bin").write_bytes(b"x")
        # base/link -> ../outside
        (base / "link").symlink_to(outside)
        with pytest.raises(PathTraversalError):
            safe_join(str(base), "link/secret.bin")


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
