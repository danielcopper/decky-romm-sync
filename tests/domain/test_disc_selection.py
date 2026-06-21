"""Unit tests for ``domain/disc_selection`` — enumeration + launch-path resolution."""

from __future__ import annotations

from domain.disc_formats import DISC_IMAGE_EXTENSIONS
from domain.disc_selection import Disc, default_descriptor, enumerate_discs, resolve_launch_path

_PSX_DIR = "/roms/psx/Final Fantasy VII"


def _p(name: str) -> str:
    return f"{_PSX_DIR}/{name}"


class TestEnumerateLabelParsing:
    def test_paren_disc_label(self):
        discs = enumerate_discs([_p("FF7 (Disc 1).cue"), _p("FF7 (Disc 2).cue")], None)
        assert [d.label for d in discs] == ["Disc 1", "Disc 2"]
        assert [d.filename for d in discs] == ["FF7 (Disc 1).cue", "FF7 (Disc 2).cue"]
        assert [d.index for d in discs] == [1, 2]

    def test_disk_of_n_form(self):
        discs = enumerate_discs([_p("Game (Disk 1 of 2).chd"), _p("Game (Disk 2 of 2).chd")], None)
        assert [d.label for d in discs] == ["Disc 1", "Disc 2"]

    def test_bracket_disc_label(self):
        discs = enumerate_discs([_p("Game [Disc 2].iso"), _p("Game [Disc 1].iso")], None)
        # Bracket form parses and sorts numerically regardless of input order.
        assert [d.label for d in discs] == ["Disc 1", "Disc 2"]
        assert [d.filename for d in discs] == ["Game [Disc 1].iso", "Game [Disc 2].iso"]

    def test_zero_padded_numbers_compare_numerically(self):
        discs = enumerate_discs([_p("G (Disc 02).cue"), _p("G (Disc 01).cue")], None)
        assert [d.label for d in discs] == ["Disc 1", "Disc 2"]

    def test_ten_or_more_discs_order_numerically_not_lexically(self):
        files = [_p(f"G (Disc {n}).cue") for n in (1, 2, 10, 11, 3)]
        discs = enumerate_discs(files, None)
        assert [d.label for d in discs] == ["Disc 1", "Disc 2", "Disc 3", "Disc 10", "Disc 11"]
        assert [d.index for d in discs] == [1, 2, 3, 4, 5]

    def test_case_insensitive_disc_keyword(self):
        discs = enumerate_discs([_p("Game (DISC 1).cue"), _p("Game (disc 2).cue")], None)
        assert [d.label for d in discs] == ["Disc 1", "Disc 2"]

    def test_unparseable_falls_to_end_lexicographically_using_stem_label(self):
        files = [_p("zeta.cue"), _p("alpha.cue"), _p("Game (Disc 1).cue")]
        discs = enumerate_discs(files, None)
        # Numbered disc first, then the two unparseable ones alphabetically.
        assert [d.label for d in discs] == ["Disc 1", "alpha", "zeta"]
        # Unparseable label is the basename stem (no extension).
        assert discs[1].filename == "alpha.cue"


class TestEnumerateExtensionIntersection:
    def test_none_supported_keeps_full_disc_set(self):
        files = [_p("a.cue"), _p("b.chd"), _p("c.iso")]
        discs = enumerate_discs(files, None)
        assert {d.filename for d in discs} == {"a.cue", "b.chd", "c.iso"}

    def test_psx_supported_keeps_all_three_disc_formats(self):
        supported = frozenset({".cue", ".chd", ".iso", ".m3u"})
        files = [_p("a.cue"), _p("b.chd"), _p("c.iso")]
        discs = enumerate_discs(files, supported)
        assert {d.filename for d in discs} == {"a.cue", "b.chd", "c.iso"}

    def test_xbox360_like_narrows_to_iso_only(self):
        # A system that accepts only .iso (e.g. an Xbox 360 image) drops cue/chd.
        supported = frozenset({".iso", ".xex"})
        files = [_p("game.iso"), _p("game.cue"), _p("game.chd")]
        discs = enumerate_discs(files, supported)
        assert {d.filename for d in discs} == {"game.iso"}

    def test_intersection_is_with_disc_set_not_arbitrary_extensions(self):
        # Supported list carries non-disc extensions; only disc-image ones survive.
        supported = frozenset({".cue", ".bin", ".m3u", ".txt"})
        files = [_p("a (Disc 1).cue"), _p("a (Disc 2).cue")]
        discs = enumerate_discs(files, supported)
        assert {d.filename for d in discs} == {"a (Disc 1).cue", "a (Disc 2).cue"}


class TestEnumerateExclusions:
    def test_bin_sidecar_excluded(self):
        files = [_p("FF7 (Disc 1).cue"), _p("FF7 (Disc 1).bin"), _p("FF7 (Disc 2).cue"), _p("FF7 (Disc 2).bin")]
        discs = enumerate_discs(files, None)
        assert [d.filename for d in discs] == ["FF7 (Disc 1).cue", "FF7 (Disc 2).cue"]

    def test_m3u_playlist_excluded(self):
        files = [_p("FF7.m3u"), _p("FF7 (Disc 1).cue"), _p("FF7 (Disc 2).cue")]
        discs = enumerate_discs(files, None)
        assert all(not d.filename.endswith(".m3u") for d in discs)
        assert [d.filename for d in discs] == ["FF7 (Disc 1).cue", "FF7 (Disc 2).cue"]

    def test_extension_match_is_case_insensitive(self):
        discs = enumerate_discs([_p("Game (Disc 1).CUE"), _p("Game (Disc 2).Iso")], None)
        assert {d.filename for d in discs} == {"Game (Disc 1).CUE", "Game (Disc 2).Iso"}


class TestEnumerateDiscCount:
    def test_single_disc_returns_one(self):
        discs = enumerate_discs([_p("solo.iso")], None)
        assert len(discs) == 1

    def test_empty_input_returns_empty(self):
        assert enumerate_discs([], None) == []

    def test_no_disc_files_returns_empty(self):
        assert enumerate_discs([_p("readme.txt"), _p("cover.png")], None) == []

    def test_mixed_cue_and_chd_both_enumerated(self):
        discs = enumerate_discs([_p("Game (Disc 1).cue"), _p("Game (Disc 2).chd")], None)
        assert [d.filename for d in discs] == ["Game (Disc 1).cue", "Game (Disc 2).chd"]


def _discs() -> list[Disc]:
    return [
        Disc(filename="FF7 (Disc 1).cue", path=_p("FF7 (Disc 1).cue"), label="Disc 1", index=1),
        Disc(filename="FF7 (Disc 2).cue", path=_p("FF7 (Disc 2).cue"), label="Disc 2", index=2),
        Disc(filename="FF7 (Disc 3).cue", path=_p("FF7 (Disc 3).cue"), label="Disc 3", index=3),
    ]


class TestResolveLaunchPath:
    def test_non_multi_disc_returns_file_path_unchanged(self):
        single = [_discs()[0]]
        path, stale = resolve_launch_path(_p("solo.iso"), single, None)
        assert path == _p("solo.iso")
        assert stale is False

    def test_empty_discs_returns_file_path(self):
        path, stale = resolve_launch_path(_p("solo.iso"), [], None)
        assert path == _p("solo.iso")
        assert stale is False

    def test_pinned_disc_returns_its_path(self):
        path, stale = resolve_launch_path(_p("FF7.m3u"), _discs(), "FF7 (Disc 2).cue")
        assert path == _p("FF7 (Disc 2).cue")
        assert stale is False

    def test_stale_pin_falls_back_to_m3u_default_when_file_path_is_m3u(self):
        path, stale = resolve_launch_path(_p("FF7.m3u"), _discs(), "FF7 (Disc 9).cue")
        assert path == _p("FF7.m3u")
        assert stale is True

    def test_stale_pin_falls_back_to_disc_1_when_no_m3u(self):
        path, stale = resolve_launch_path(_p("FF7 (Disc 1).cue"), _discs(), "FF7 (Disc 9).cue")
        assert path == _p("FF7 (Disc 1).cue")
        assert stale is True

    def test_m3u_default_when_no_selection(self):
        path, stale = resolve_launch_path(_p("FF7.m3u"), _discs(), None)
        assert path == _p("FF7.m3u")
        assert stale is False

    def test_disc_1_default_when_no_selection_and_no_m3u(self):
        path, stale = resolve_launch_path(_p("FF7 (Disc 1).cue"), _discs(), None)
        assert path == _p("FF7 (Disc 1).cue")
        assert stale is False

    def test_m3u_match_is_case_insensitive(self):
        path, stale = resolve_launch_path(_p("FF7.M3U"), _discs(), None)
        assert path == _p("FF7.M3U")
        assert stale is False


class TestDefaultDescriptor:
    def test_m3u_file_path_describes_all_discs(self):
        desc = default_descriptor(_p("FF7.m3u"), _discs())
        assert desc == {"kind": "m3u", "label": "All discs (m3u)", "filename": "FF7.m3u"}

    def test_m3u_match_is_case_insensitive(self):
        desc = default_descriptor(_p("FF7.M3U"), _discs())
        assert desc["kind"] == "m3u"
        assert desc["filename"] == "FF7.M3U"

    def test_non_m3u_describes_first_disc(self):
        desc = default_descriptor(_p("FF7 (Disc 1).cue"), _discs())
        assert desc == {"kind": "disc", "label": "Disc 1", "filename": "FF7 (Disc 1).cue"}


class TestDiscImageExtensionsConstant:
    def test_enumerate_default_set_is_the_constant(self):
        # No supported list -> exactly the disc-image set is the accept set.
        files = [_p(f"x{ext}") for ext in DISC_IMAGE_EXTENSIONS]
        discs = enumerate_discs(files, None)
        assert {d.filename for d in discs} == {f"x{ext}" for ext in DISC_IMAGE_EXTENSIONS}
