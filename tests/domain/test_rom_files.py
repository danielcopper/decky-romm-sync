"""Tests for domain.rom_files — pure M3U and launch file detection functions."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))

from domain.rom_files import (
    build_m3u_content,
    detect_launch_file,
    es_de_collapse_rename,
    is_multi_file_download,
    needs_m3u,
    resolve_local_file_name,
)


def _with_sizes(paths: list[str]) -> list[tuple[str, int]]:
    """Helper: convert a list of file paths to (path, size) tuples for detect_launch_file."""
    result = []
    for p in paths:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        result.append((p, size))
    return result


class TestIsMultiFileDownload:
    def test_zero_files_and_no_flag_is_single(self):
        assert is_multi_file_download({"files": []}) is False

    def test_one_file_and_no_flag_is_single(self):
        assert is_multi_file_download({"files": [{"file_name": "game.nsp"}]}) is False

    def test_two_files_is_multi(self):
        rom_detail = {
            "files": [{"file_name": "base.nsp"}, {"file_name": "update/patch.nsp"}],
        }
        assert is_multi_file_download(rom_detail) is True

    def test_three_files_is_multi(self):
        rom_detail = {
            "files": [
                {"file_name": "base.nsp"},
                {"file_name": "update/patch.nsp"},
                {"file_name": "dlc/extra.nsp"},
            ],
        }
        assert is_multi_file_download(rom_detail) is True

    def test_flag_true_with_one_file_falls_back_to_multi(self):
        # Defensive fallback: trust the boolean even when files implies single.
        rom_detail = {"has_multiple_files": True, "files": [{"file_name": "game.zip"}]}
        assert is_multi_file_download(rom_detail) is True

    def test_nested_switch_single_top_level_but_many_files_is_multi(self):
        # #855: Switch base/update/DLC folder — exactly one top-level file so
        # has_multiple_files is False, but total file count > 1 → RomM zips it.
        rom_detail = {
            "has_multiple_files": False,
            "has_nested_single_file": True,
            "files": [
                {"file_name": "base.nsp"},
                {"file_name": "update/patch.nsp"},
                {"file_name": "dlc/extra.nsp"},
            ],
        }
        assert is_multi_file_download(rom_detail) is True

    def test_genuine_nested_single_stays_single(self):
        # has_nested_single_file with exactly one file must NOT be treated as multi.
        rom_detail = {
            "has_multiple_files": False,
            "has_nested_single_file": True,
            "files": [{"file_name": "game.chd"}],
        }
        assert is_multi_file_download(rom_detail) is False

    def test_missing_files_key_uses_flag_only(self):
        assert is_multi_file_download({"has_multiple_files": True}) is True
        assert is_multi_file_download({"has_multiple_files": False}) is False

    def test_missing_both_keys_is_single(self):
        assert is_multi_file_download({}) is False

    def test_files_explicitly_none_uses_flag_only(self):
        assert is_multi_file_download({"files": None, "has_multiple_files": True}) is True
        assert is_multi_file_download({"files": None}) is False


class TestNeedsM3u:
    def test_two_disc_files_returns_true(self):
        assert needs_m3u(["disc1.cue", "disc2.cue"], m3u_supported=True) is True

    def test_three_disc_files_returns_true(self):
        assert needs_m3u(["disc1.chd", "disc2.chd", "disc3.chd"], m3u_supported=True) is True

    def test_empty_list_returns_false(self):
        assert needs_m3u([], m3u_supported=True) is False

    def test_single_cue_returns_true(self):
        # Single-disc bin/cue: M3U so the extract dir gets a game-named playlist.
        assert needs_m3u(["disc1.cue"], m3u_supported=True) is True

    def test_single_cue_case_insensitive_returns_true(self):
        assert needs_m3u(["Game.CUE"], m3u_supported=True) is True

    def test_single_chd_returns_false(self):
        # Single-disc chd is a single-file download; iso/chd are out of scope.
        assert needs_m3u(["game.chd"], m3u_supported=True) is False

    def test_single_iso_returns_false(self):
        assert needs_m3u(["game.iso"], m3u_supported=True) is False

    def test_boundary_exactly_two(self):
        assert needs_m3u(["a.iso", "b.iso"], m3u_supported=True) is True

    def test_two_chd_returns_true(self):
        assert needs_m3u(["disc1.chd", "disc2.chd"], m3u_supported=True) is True

    def test_mixed_two_or_more_returns_true(self):
        assert needs_m3u(["disc1.cue", "disc2.chd"], m3u_supported=True) is True

    def test_unsupported_platform_always_false(self):
        # #1111: when the platform does not support .m3u, nothing warrants one —
        # not multi-cue, not multi-iso, not single-cue.
        assert needs_m3u(["disc1.cue", "disc2.cue"], m3u_supported=False) is False
        assert needs_m3u(["a.iso", "b.iso"], m3u_supported=False) is False
        assert needs_m3u(["disc1.cue"], m3u_supported=False) is False
        assert needs_m3u([], m3u_supported=False) is False


class TestBuildM3uContent:
    def test_two_files_sorted(self):
        content = build_m3u_content(["disc2.cue", "disc1.cue"])
        lines = content.strip().split("\n")
        assert lines[0] == "disc1.cue"
        assert lines[1] == "disc2.cue"

    def test_trailing_newline(self):
        content = build_m3u_content(["disc1.cue", "disc2.cue"])
        assert content.endswith("\n")

    def test_single_file(self):
        content = build_m3u_content(["game.cue"])
        assert content.strip() == "game.cue"
        assert content.endswith("\n")

    def test_already_sorted_list_unchanged(self):
        files = ["disc1.cue", "disc2.cue", "disc3.cue"]
        content = build_m3u_content(files)
        lines = content.strip().split("\n")
        assert lines == ["disc1.cue", "disc2.cue", "disc3.cue"]

    def test_special_characters_preserved(self):
        files = ["Game (Disc 1) [Japan].cue", "Game (Disc 2) [Japan].cue"]
        content = build_m3u_content(files)
        lines = content.strip().split("\n")
        assert "Game (Disc 1) [Japan].cue" in lines
        assert "Game (Disc 2) [Japan].cue" in lines

    def test_sorting_is_applied(self):
        files = ["b.cue", "c.cue", "a.cue"]
        content = build_m3u_content(files)
        lines = content.strip().split("\n")
        assert lines == ["a.cue", "b.cue", "c.cue"]

    def test_mixed_formats_sorted_together(self):
        files = ["disc2.chd", "disc1.cue"]
        content = build_m3u_content(files)
        lines = content.strip().split("\n")
        assert len(lines) == 2
        # Both present, sorted alphabetically
        assert "disc1.cue" in lines
        assert "disc2.chd" in lines


class TestDetectLaunchFile:
    def test_empty_list_returns_none(self):
        assert detect_launch_file([], m3u_supported=True) is None

    def test_prefers_m3u_over_cue(self, tmp_path):
        m3u = str(tmp_path / "game.m3u")
        cue = str(tmp_path / "disc1.cue")
        open(m3u, "w").close()
        open(cue, "w").close()
        result = detect_launch_file(_with_sizes([m3u, cue]), m3u_supported=True)
        assert result == m3u

    def test_prefers_cue_over_bin(self, tmp_path):
        cue = str(tmp_path / "disc1.cue")
        binf = str(tmp_path / "disc1.bin")
        open(cue, "w").close()
        with open(binf, "wb") as f:
            f.write(b"\x00" * 1000)
        result = detect_launch_file(_with_sizes([cue, binf]), m3u_supported=True)
        assert result == cue

    def test_rpx_returned_when_no_m3u_or_cue(self, tmp_path):
        rpx = str(tmp_path / "code" / "game.rpx")
        os.makedirs(os.path.dirname(rpx))
        open(rpx, "w").close()
        result = detect_launch_file(_with_sizes([rpx]), m3u_supported=True)
        assert result == rpx

    def test_m3u_beats_rpx(self, tmp_path):
        m3u = str(tmp_path / "game.m3u")
        rpx = str(tmp_path / "code" / "game.rpx")
        os.makedirs(os.path.dirname(rpx))
        open(m3u, "w").close()
        open(rpx, "w").close()
        result = detect_launch_file(_with_sizes([m3u, rpx]), m3u_supported=True)
        assert result == m3u

    def test_wux_disc_image(self, tmp_path):
        wux = str(tmp_path / "game.wux")
        txt = str(tmp_path / "readme.txt")
        with open(wux, "wb") as f:
            f.write(b"\x00" * 1000)
        open(txt, "w").close()
        result = detect_launch_file(_with_sizes([wux, txt]), m3u_supported=True)
        assert result == wux

    def test_wud_disc_image(self, tmp_path):
        wud = str(tmp_path / "game.wud")
        with open(wud, "wb") as f:
            f.write(b"\x00" * 1000)
        result = detect_launch_file(_with_sizes([wud]), m3u_supported=True)
        assert result == wud

    def test_wua_disc_image(self, tmp_path):
        wua = str(tmp_path / "game.wua")
        with open(wua, "wb") as f:
            f.write(b"\x00" * 1000)
        result = detect_launch_file(_with_sizes([wua]), m3u_supported=True)
        assert result == wua

    def test_eboot_bin_ps3(self, tmp_path):
        eboot = str(tmp_path / "PS3_GAME" / "USRDIR" / "EBOOT.BIN")
        os.makedirs(os.path.dirname(eboot))
        with open(eboot, "wb") as f:
            f.write(b"\x00" * 500)
        result = detect_launch_file(_with_sizes([eboot]), m3u_supported=True)
        assert result == eboot

    def test_3ds_preferred_over_cia(self, tmp_path):
        rom_3ds = str(tmp_path / "game.3ds")
        cia = str(tmp_path / "game.cia")
        with open(rom_3ds, "wb") as f:
            f.write(b"\x00" * 100)
        with open(cia, "wb") as f:
            f.write(b"\x00" * 100)
        result = detect_launch_file(_with_sizes([rom_3ds, cia]), m3u_supported=True)
        assert result == rom_3ds

    def test_cia_preferred_over_cxi(self, tmp_path):
        cia = str(tmp_path / "game.cia")
        cxi = str(tmp_path / "game.cxi")
        with open(cia, "wb") as f:
            f.write(b"\x00" * 100)
        with open(cxi, "wb") as f:
            f.write(b"\x00" * 100)
        result = detect_launch_file(_with_sizes([cia, cxi]), m3u_supported=True)
        assert result == cia

    def test_falls_back_to_largest_file(self, tmp_path):
        small = str(tmp_path / "small.bin")
        large = str(tmp_path / "large.bin")
        with open(small, "wb") as f:
            f.write(b"\x00" * 100)
        with open(large, "wb") as f:
            f.write(b"\x00" * 10000)
        result = detect_launch_file(_with_sizes([small, large]), m3u_supported=True)
        assert result == large

    def test_single_file_returned_directly(self, tmp_path):
        f = str(tmp_path / "game.z64")
        with open(f, "wb") as fh:
            fh.write(b"\x00" * 100)
        assert detect_launch_file(_with_sizes([f]), m3u_supported=True) == f

    def test_case_insensitive_extension_matching(self, tmp_path):
        m3u = str(tmp_path / "GAME.M3U")
        open(m3u, "w").close()
        result = detect_launch_file(_with_sizes([m3u]), m3u_supported=True)
        assert result == m3u

    def test_bundled_m3u_skipped_when_unsupported_picks_cue(self, tmp_path):
        # #1111: with m3u unsupported a bundled .m3u is ignored; the .cue wins.
        m3u = str(tmp_path / "game.m3u")
        cue = str(tmp_path / "disc1.cue")
        open(m3u, "w").close()
        open(cue, "w").close()
        result = detect_launch_file(_with_sizes([m3u, cue]), m3u_supported=False)
        assert result == cue

    def test_bundled_m3u_skipped_when_unsupported_falls_to_largest(self, tmp_path):
        # No cue/platform-specific file: the .m3u is skipped and the real game
        # file (largest) is chosen instead of the playlist.
        m3u = str(tmp_path / "game.m3u")
        nsp = str(tmp_path / "game.nsp")
        open(m3u, "w").close()
        with open(nsp, "wb") as f:
            f.write(b"\x00" * 5000)
        result = detect_launch_file(_with_sizes([m3u, nsp]), m3u_supported=False)
        assert result == nsp


class TestEsDeCollapseRename:
    """Tests for es_de_collapse_rename — pure path algebra for the ES-DE dir collapse."""

    def test_happy_m3u_renames_dir_to_launch_file_basename(self):
        rom_dir = "/roms/psx/Game"
        launch_file = "/roms/psx/Game/Game.m3u"
        assert es_de_collapse_rename(rom_dir, launch_file) == (
            "/roms/psx/Game.m3u",
            "/roms/psx/Game.m3u/Game.m3u",
        )

    def test_cue_variant_renames_dir(self):
        rom_dir = "/roms/psx/Final Fantasy VII (USA)"
        launch_file = "/roms/psx/Final Fantasy VII (USA)/Final Fantasy VII (USA).cue"
        assert es_de_collapse_rename(rom_dir, launch_file) == (
            "/roms/psx/Final Fantasy VII (USA).cue",
            "/roms/psx/Final Fantasy VII (USA).cue/Final Fantasy VII (USA).cue",
        )

    def test_idempotent_already_named_after_launch_file(self):
        rom_dir = "/roms/psx/Game.m3u"
        launch_file = "/roms/psx/Game.m3u/Game.m3u"
        assert es_de_collapse_rename(rom_dir, launch_file) is None

    def test_fallback_launch_file_equals_rom_dir(self):
        rom_dir = "/roms/psx/Game"
        assert es_de_collapse_rename(rom_dir, rom_dir) is None

    def test_empty_launch_file_returns_none(self):
        assert es_de_collapse_rename("/roms/psx/Game", "") is None

    def test_nested_launch_file_in_subdir_returns_none(self):
        rom_dir = "/roms/wiiu/Game"
        launch_file = "/roms/wiiu/Game/code/Game.rpx"
        assert es_de_collapse_rename(rom_dir, launch_file) is None


class TestResolveLocalFileName:
    def test_non_nested_returns_fs_name(self):
        assert resolve_local_file_name({"fs_name": "game.zip"}) == ("game.zip", False)

    def test_nested_returns_file_name_from_files_list(self):
        rom_detail = {
            "fs_name": "parent_folder",
            "has_nested_single_file": True,
            "files": [{"file_name": "actual.rom"}],
        }
        assert resolve_local_file_name(rom_detail) == ("actual.rom", False)

    def test_nested_but_empty_files_returns_fs_name_with_inconsistent_flag(self):
        rom_detail = {
            "fs_name": "parent",
            "has_nested_single_file": True,
            "files": [],
        }
        name, inconsistent = resolve_local_file_name(rom_detail)
        assert name == "parent"
        assert inconsistent is True

    def test_nested_with_missing_file_name_falls_back_to_fs_name(self):
        rom_detail = {
            "fs_name": "parent",
            "has_nested_single_file": True,
            "files": [{}],
        }
        assert resolve_local_file_name(rom_detail) == ("parent", False)

    def test_missing_fs_name_uses_id_fallback(self):
        assert resolve_local_file_name({"id": 42}) == ("rom_42", False)

    def test_missing_fs_name_and_id_uses_unknown_fallback(self):
        assert resolve_local_file_name({}) == ("rom_unknown", False)

    def test_files_explicitly_none(self):
        rom_detail = {
            "fs_name": "parent",
            "has_nested_single_file": True,
            "files": None,
        }
        name, inconsistent = resolve_local_file_name(rom_detail)
        assert name == "parent"
        assert inconsistent is True
