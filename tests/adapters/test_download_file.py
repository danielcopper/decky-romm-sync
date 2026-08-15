"""Tests for DownloadFileAdapter — raw filesystem ops for ROM downloads."""

from __future__ import annotations

import contextlib
import os
import socket
import zipfile
from unittest.mock import patch

import pytest

from adapters.download_file import DownloadFileAdapter
from domain.rom_candidates import DIR, FILE, LINK


@pytest.fixture
def adapter() -> DownloadFileAdapter:
    return DownloadFileAdapter()


class TestExists:
    def test_true_for_existing_file(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        assert adapter.exists(str(f)) is True

    def test_true_for_directory(self, adapter, tmp_path):
        assert adapter.exists(str(tmp_path)) is True

    def test_false_for_missing(self, adapter, tmp_path):
        assert adapter.exists(str(tmp_path / "missing.rom")) is False


class TestRemove:
    def test_removes_existing(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        adapter.remove_file(str(f))
        assert not f.exists()

    def test_missing_is_noop(self, adapter, tmp_path):
        # Idempotent — must not raise on a missing file
        adapter.remove_file(str(tmp_path / "missing.rom"))

    def test_propagates_non_filenotfound(self, adapter, tmp_path):
        # Removing a directory with os.remove raises IsADirectoryError /
        # OSError — anything other than FileNotFoundError must surface.
        with pytest.raises(OSError):
            adapter.remove_file(str(tmp_path))


class TestRemoveTree:
    def test_removes_directory(self, adapter, tmp_path):
        d = tmp_path / "rom_dir"
        d.mkdir()
        (d / "a").write_bytes(b"")
        (d / "b").write_bytes(b"")
        adapter.remove_tree(str(d))
        assert not d.exists()

    def test_missing_is_noop(self, adapter, tmp_path):
        # Idempotent on missing directory
        adapter.remove_tree(str(tmp_path / "missing"))

    def test_removes_nested(self, adapter, tmp_path):
        d = tmp_path / "rom"
        nested = d / "sub" / "deeper"
        nested.mkdir(parents=True)
        (nested / "file").write_bytes(b"data")
        adapter.remove_tree(str(d))
        assert not d.exists()


class TestMakeDirs:
    def test_creates_directory(self, adapter, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        adapter.make_dirs(str(target))
        assert target.is_dir()

    def test_idempotent_when_exists(self, adapter, tmp_path):
        adapter.make_dirs(str(tmp_path))  # already exists — must not raise


class TestRename:
    def test_renames(self, adapter, tmp_path):
        src = tmp_path / "src.rom"
        dst = tmp_path / "dst.rom"
        src.write_bytes(b"data")
        adapter.rename(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"data"

    def test_replaces_existing(self, adapter, tmp_path):
        src = tmp_path / "src.rom"
        dst = tmp_path / "dst.rom"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        adapter.rename(str(src), str(dst))
        assert dst.read_bytes() == b"new"

    def test_missing_source_raises(self, adapter, tmp_path):
        with pytest.raises(FileNotFoundError):
            adapter.rename(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestMoveDir:
    def test_moves_whole_subtree(self, adapter, tmp_path):
        src = tmp_path / "Game"
        (src / "sub").mkdir(parents=True)
        (src / "Game.m3u").write_text("disc1.cue\n")
        (src / "sub" / "disc1.cue").write_text("data")
        dst = tmp_path / "Game.m3u"
        adapter.move_dir(str(src), str(dst))
        assert not src.exists()
        assert (dst / "Game.m3u").read_text() == "disc1.cue\n"
        assert (dst / "sub" / "disc1.cue").read_text() == "data"

    def test_missing_source_raises(self, adapter, tmp_path):
        with pytest.raises(FileNotFoundError):
            adapter.move_dir(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestCopyFile:
    def test_copies_and_preserves_source(self, adapter, tmp_path):
        src = tmp_path / "PS3_DISC.SFB.txt"
        src.write_bytes(b"SFB-BYTES")
        dst = tmp_path / "PS3_DISC.SFB"

        adapter.copy_file(str(src), str(dst))

        assert dst.read_bytes() == b"SFB-BYTES"
        # The source is preserved (copy, not move).
        assert src.read_bytes() == b"SFB-BYTES"

    def test_missing_source_raises(self, adapter, tmp_path):
        with pytest.raises(FileNotFoundError):
            adapter.copy_file(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestDiskFree:
    def test_returns_positive_int(self, adapter, tmp_path):
        # Real filesystem returns some non-negative integer.
        free = adapter.disk_free(str(tmp_path))
        assert isinstance(free, int)
        assert free >= 0


class TestWalkFilesMatchingSuffixes:
    def test_lists_matching_suffixes(self, adapter, tmp_path):
        (tmp_path / "a.tmp").write_text("")
        (tmp_path / "b.zip.tmp").write_text("")
        (tmp_path / "real.rom").write_text("keep")
        matches = adapter.walk_files_matching_suffixes(str(tmp_path), (".tmp", ".zip.tmp"))
        assert sorted(matches) == sorted([str(tmp_path / "a.tmp"), str(tmp_path / "b.zip.tmp")])
        # Pure listing — nothing was removed
        assert (tmp_path / "a.tmp").exists()
        assert (tmp_path / "b.zip.tmp").exists()
        assert (tmp_path / "real.rom").exists()

    def test_recursive(self, adapter, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "x.tmp").write_text("")
        (tmp_path / "y.tmp").write_text("")
        matches = adapter.walk_files_matching_suffixes(str(tmp_path), (".tmp",))
        assert sorted(matches) == sorted([str(sub / "x.tmp"), str(tmp_path / "y.tmp")])

    def test_recurses_any_depth(self, adapter, tmp_path):
        # The old clean_tmp_files scan capped at 2 levels — walk_files_matching_suffixes
        # follows os.walk and recurses unbounded so deep mid-extraction crashes are caught.
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "stuck.tmp").write_text("")
        matches = adapter.walk_files_matching_suffixes(str(tmp_path), (".tmp",))
        assert matches == [str(deep / "stuck.tmp")]

    def test_missing_base_dir_returns_empty(self, adapter, tmp_path):
        # Idempotent on missing base_dir
        assert adapter.walk_files_matching_suffixes(str(tmp_path / "missing"), (".tmp",)) == []

    def test_no_matching_suffix(self, adapter, tmp_path):
        (tmp_path / "rom.bin").write_text("")
        assert adapter.walk_files_matching_suffixes(str(tmp_path), (".tmp",)) == []

    def test_empty_directory(self, adapter, tmp_path):
        assert adapter.walk_files_matching_suffixes(str(tmp_path), (".tmp",)) == []


class TestExtractZip:
    def _make_zip(self, path, members: dict[str, bytes]) -> None:
        with zipfile.ZipFile(str(path), "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)

    def test_extracts_members(self, adapter, tmp_path):
        archive = tmp_path / "src.zip"
        self._make_zip(archive, {"a.bin": b"AAA", "b.bin": b"BBB"})
        dest = tmp_path / "out"
        dest.mkdir()
        result = adapter.extract_zip(str(archive), str(dest), str(tmp_path))
        # Adapter returns None — caller asserts on filesystem state.
        assert result is None
        assert (dest / "a.bin").read_bytes() == b"AAA"
        assert (dest / "b.bin").read_bytes() == b"BBB"

    def test_rejects_zip_slip(self, adapter, tmp_path):
        archive = tmp_path / "evil.zip"
        # ZIP member with .. traversal
        self._make_zip(archive, {"../escape.txt": b"bad"})
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ValueError, match="outside"):
            adapter.extract_zip(str(archive), str(dest), str(tmp_path))
        # Nothing should be extracted outside the safe root
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_rejects_extract_dir_outside_safe_root(self, adapter, tmp_path):
        archive = tmp_path / "src.zip"
        self._make_zip(archive, {"a.bin": b"x"})
        # dest is outside safe_root
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="outside safe root"):
            adapter.extract_zip(str(archive), str(outside), str(tmp_path))

    def test_allows_dest_equal_to_safe_root(self, adapter, tmp_path):
        archive = tmp_path / "src.zip"
        self._make_zip(archive, {"a.bin": b"x"})
        # dest == safe_root is allowed
        result = adapter.extract_zip(str(archive), str(tmp_path), str(tmp_path))
        assert result is None
        assert (tmp_path / "a.bin").read_bytes() == b"x"

    def test_progress_callback_reports_monotonic_to_total(self, adapter, tmp_path):
        archive = tmp_path / "src.zip"
        members = {"a.bin": b"A" * 1500, "b.bin": b"B" * 2500}
        self._make_zip(archive, members)
        dest = tmp_path / "out"
        dest.mkdir()
        calls: list[tuple[int, int]] = []
        adapter.extract_zip(str(archive), str(dest), str(tmp_path), progress_callback=lambda e, t: calls.append((e, t)))

        total = sum(len(data) for data in members.values())
        # Every tick carries the same total.
        assert all(t == total for _e, t in calls)
        # extracted is non-decreasing and ends exactly at total.
        extracted_seq = [e for e, _t in calls]
        assert extracted_seq == sorted(extracted_seq)
        assert extracted_seq[-1] == total
        # Files extracted with identical bytes.
        assert (dest / "a.bin").read_bytes() == members["a.bin"]
        assert (dest / "b.bin").read_bytes() == members["b.bin"]

    def test_progress_callback_chunks_large_member(self, adapter, tmp_path):
        # A member larger than _EXTRACT_CHUNK (1 MiB) yields multiple ticks,
        # each a chunk-sized step, climbing to the member's full size.
        archive = tmp_path / "big.zip"
        big = b"\x00" * (1024 * 1024 + 7)  # 1 MiB + a partial chunk
        self._make_zip(archive, {"rom.bin": big})
        dest = tmp_path / "out"
        dest.mkdir()
        calls: list[tuple[int, int]] = []
        adapter.extract_zip(str(archive), str(dest), str(tmp_path), progress_callback=lambda e, t: calls.append((e, t)))

        assert len(calls) >= 2  # at least one full chunk + the remainder
        assert [e for e, _t in calls] == sorted(e for e, _t in calls)
        assert calls[-1] == (len(big), len(big))
        assert (dest / "rom.bin").read_bytes() == big

    def test_progress_callback_nested_dirs(self, adapter, tmp_path):
        archive = tmp_path / "nested.zip"
        members = {
            "base.nsp": b"\x01" * 100,
            "update/upd.nsp": b"\x02" * 200,
            "dlc/extra.nsp": b"\x03" * 300,
        }
        self._make_zip(archive, members)
        dest = tmp_path / "out"
        dest.mkdir()
        calls: list[tuple[int, int]] = []
        adapter.extract_zip(str(archive), str(dest), str(tmp_path), progress_callback=lambda e, t: calls.append((e, t)))

        total = sum(len(d) for d in members.values())
        assert calls[-1] == (total, total)
        assert (dest / "base.nsp").read_bytes() == members["base.nsp"]
        assert (dest / "update" / "upd.nsp").read_bytes() == members["update/upd.nsp"]
        assert (dest / "dlc" / "extra.nsp").read_bytes() == members["dlc/extra.nsp"]

    def test_progress_callback_zip_slip_still_raises(self, adapter, tmp_path):
        archive = tmp_path / "evil.zip"
        self._make_zip(archive, {"../escape.txt": b"bad"})
        dest = tmp_path / "out"
        dest.mkdir()
        calls: list[tuple[int, int]] = []
        with pytest.raises(ValueError, match="outside"):
            adapter.extract_zip(
                str(archive), str(dest), str(tmp_path), progress_callback=lambda e, t: calls.append((e, t))
            )
        # Validation runs BEFORE any write, so the callback never fired.
        assert calls == []
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_none_callback_is_back_compatible(self, adapter, tmp_path):
        # progress_callback=None (the default) extracts byte-identically and
        # invokes no callback — back-compat with the old extractall.
        archive = tmp_path / "src.zip"
        members = {"a.bin": b"AAA", "sub/b.bin": b"BBBB"}
        self._make_zip(archive, members)
        dest = tmp_path / "out"
        dest.mkdir()
        result = adapter.extract_zip(str(archive), str(dest), str(tmp_path))
        assert result is None
        assert (dest / "a.bin").read_bytes() == b"AAA"
        assert (dest / "sub" / "b.bin").read_bytes() == b"BBBB"

    def test_empty_zip_no_callbacks(self, adapter, tmp_path):
        archive = tmp_path / "empty.zip"
        self._make_zip(archive, {})
        dest = tmp_path / "out"
        dest.mkdir()
        calls: list[tuple[int, int]] = []
        adapter.extract_zip(str(archive), str(dest), str(tmp_path), progress_callback=lambda e, t: calls.append((e, t)))
        # No members → no writes → no progress callbacks.
        assert calls == []

    def test_zero_byte_member(self, adapter, tmp_path):
        archive = tmp_path / "mix.zip"
        members = {"empty.bin": b"", "data.bin": b"X" * 50}
        self._make_zip(archive, members)
        dest = tmp_path / "out"
        dest.mkdir()
        calls: list[tuple[int, int]] = []
        adapter.extract_zip(str(archive), str(dest), str(tmp_path), progress_callback=lambda e, t: calls.append((e, t)))
        # The zero-byte member writes no chunk (no tick); the 50-byte member
        # produces the final tick at total.
        assert (dest / "empty.bin").read_bytes() == b""
        assert (dest / "data.bin").read_bytes() == members["data.bin"]
        assert calls[-1] == (50, 50)


class TestDecodeUrlEncodedNames:
    def test_renames_url_encoded_file(self, adapter, tmp_path):
        (tmp_path / "Game%20Title.cue").write_text("")
        adapter.decode_url_encoded_names(str(tmp_path))
        assert (tmp_path / "Game Title.cue").exists()
        assert not (tmp_path / "Game%20Title.cue").exists()

    def test_renames_url_encoded_dir(self, adapter, tmp_path):
        (tmp_path / "Disc%201").mkdir()
        adapter.decode_url_encoded_names(str(tmp_path))
        assert (tmp_path / "Disc 1").exists()

    def test_handles_nested_encoded_dirs(self, adapter, tmp_path):
        outer = tmp_path / "Disc%201"
        outer.mkdir()
        (outer / "track%20one.bin").write_text("")
        adapter.decode_url_encoded_names(str(tmp_path))
        decoded_dir = tmp_path / "Disc 1"
        assert decoded_dir.exists()
        assert (decoded_dir / "track one.bin").exists()

    def test_noop_for_ascii_names(self, adapter, tmp_path):
        (tmp_path / "plain.cue").write_text("")
        (tmp_path / "subdir").mkdir()
        adapter.decode_url_encoded_names(str(tmp_path))
        assert (tmp_path / "plain.cue").exists()
        assert (tmp_path / "subdir").exists()

    def test_empty_directory(self, adapter, tmp_path):
        # Must not raise
        adapter.decode_url_encoded_names(str(tmp_path))

    def test_rejects_decoded_traversal_member(self, adapter, tmp_path):
        """#968: a ``%2e%2e%2f``-encoded member name fails-stop, file not moved out."""
        from lib.path_safety import PathTraversalError

        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()
        # A single literal basename containing no real separator — it passed the
        # pre-decode ZIP-slip check as one safe component. unquote() turns it
        # into "../evil.sh".
        encoded = "%2e%2e%2fevil.sh"
        (extract_dir / encoded).write_text("payload")

        with pytest.raises(PathTraversalError):
            adapter.decode_url_encoded_names(str(extract_dir))

        # The file was NOT moved outside the extraction dir.
        assert not (tmp_path / "evil.sh").exists()
        # The original encoded file is still present (no partial os.replace).
        assert (extract_dir / encoded).exists()

    def test_legit_multi_file_subdir_decodes_correctly(self, adapter, tmp_path):
        """A real multi-file ROM with an encoded name inside a real subdir still extracts.

        ``os.walk`` yields each name as a single basename, so a member inside a
        real ``update/`` subfolder decodes per-basename and stays valid — the
        #968 fix must not break legitimate nested ROM layouts.
        """
        extract_dir = tmp_path / "extract"
        sub = extract_dir / "update"
        sub.mkdir(parents=True)
        (sub / "Zelda%20Update.nsp").write_bytes(b"\x00" * 10)
        (extract_dir / "Zelda%20Base.nsp").write_bytes(b"\x00" * 10)

        adapter.decode_url_encoded_names(str(extract_dir))

        assert (extract_dir / "Zelda Base.nsp").exists()
        assert (extract_dir / "update" / "Zelda Update.nsp").exists()


class TestScanFilesWithSizes:
    def test_returns_paths_and_sizes(self, adapter, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"\x00" * 10)
        (tmp_path / "b.bin").write_bytes(b"\x00" * 20)
        out = adapter.scan_files_with_sizes(str(tmp_path))
        sizes = dict(out)
        assert sizes[str(tmp_path / "a.bin")] == 10
        assert sizes[str(tmp_path / "b.bin")] == 20

    def test_recursive(self, adapter, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.bin").write_bytes(b"\x00" * 5)
        out = adapter.scan_files_with_sizes(str(tmp_path))
        assert (str(sub / "deep.bin"), 5) in out

    def test_empty_directory(self, adapter, tmp_path):
        assert adapter.scan_files_with_sizes(str(tmp_path)) == []

    def test_size_falls_back_to_zero_on_os_error(self, adapter, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"x")
        with patch("adapters.download_file.os.path.getsize", side_effect=OSError):
            out = adapter.scan_files_with_sizes(str(tmp_path))
        assert out == [(str(tmp_path / "a.bin"), 0)]


class TestWriteTextAtomic:
    def test_writes_content(self, adapter, tmp_path):
        dest = tmp_path / "playlist.m3u"
        adapter.write_text_atomic(str(dest), "disc1.cue\ndisc2.cue\n")
        assert dest.read_text() == "disc1.cue\ndisc2.cue\n"

    def test_overwrites_existing(self, adapter, tmp_path):
        dest = tmp_path / "playlist.m3u"
        dest.write_text("old")
        adapter.write_text_atomic(str(dest), "new")
        assert dest.read_text() == "new"

    def test_no_tmp_left_after_success(self, adapter, tmp_path):
        dest = tmp_path / "playlist.m3u"
        adapter.write_text_atomic(str(dest), "x")
        assert not (tmp_path / "playlist.m3u.tmp").exists()

    def test_cleans_tmp_on_failure(self, adapter, tmp_path):
        dest = tmp_path / "playlist.m3u"
        with (
            patch("adapters.download_file.os.replace", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            adapter.write_text_atomic(str(dest), "data")
        assert not (tmp_path / "playlist.m3u.tmp").exists()
        assert not dest.exists()

    def test_encodes_utf8(self, adapter, tmp_path):
        dest = tmp_path / "playlist.m3u"
        adapter.write_text_atomic(str(dest), "Final Fantasy VII — Disc 1.cue\n")
        assert dest.read_text(encoding="utf-8") == "Final Fantasy VII — Disc 1.cue\n"


class TestDescribePath:
    def test_none_for_a_missing_path(self, adapter, tmp_path):
        assert adapter.describe_path(str(tmp_path / "nope")) is None

    def test_describes_a_file(self, adapter, tmp_path):
        f = tmp_path / "Game.sfc"
        f.write_bytes(b"0123456789")
        described = adapter.describe_path(str(f))
        assert described is not None
        assert described["path"] == str(f)
        assert described["kind"] == FILE
        assert described["size_bytes"] == 10
        assert described["modified_at"] == pytest.approx(f.stat().st_mtime)

    def test_a_directory_reports_its_recursive_total(self, adapter, tmp_path):
        # Comparable with the server's fs_size_bytes for a multi-file ROM, which
        # is the sum over every file including the nested ones.
        game = tmp_path / "Game"
        (game / "sub").mkdir(parents=True)
        (game / "disc1.bin").write_bytes(b"a" * 100)
        (game / "sub" / "disc2.bin").write_bytes(b"b" * 55)
        described = adapter.describe_path(str(game))
        assert described is not None
        assert described["kind"] == DIR
        assert described["size_bytes"] == 155

    def test_an_empty_directory_reports_zero(self, adapter, tmp_path):
        empty = tmp_path / "Empty"
        empty.mkdir()
        described = adapter.describe_path(str(empty))
        assert described is not None
        assert described["size_bytes"] == 0

    def test_a_broken_symlink_inside_a_directory_contributes_zero(self, adapter, tmp_path):
        # lstat on a dangling link succeeds and reports the link's own size, so
        # a broken link never aborts the description of the tree around it.
        game = tmp_path / "Game"
        game.mkdir()
        (game / "real.bin").write_bytes(b"a" * 10)
        (game / "dangling").symlink_to(tmp_path / "gone")
        described = adapter.describe_path(str(game))
        assert described is not None
        assert described["size_bytes"] >= 10


class TestListTopLevelEntries:
    def test_a_missing_directory_lists_nothing(self, adapter, tmp_path):
        assert adapter.list_top_level_entries(str(tmp_path / "nope")) == ()

    def test_an_empty_directory_lists_nothing(self, adapter, tmp_path):
        empty = tmp_path / "gba"
        empty.mkdir()
        assert adapter.list_top_level_entries(str(empty)) == ()

    def test_a_file_carries_its_kind_size_and_mtime(self, adapter, tmp_path):
        rom = tmp_path / "Game (U).gba"
        rom.write_bytes(b"0123456789")
        (entry,) = adapter.list_top_level_entries(str(tmp_path))
        assert entry["name"] == "Game (U).gba"
        assert entry["path"] == str(rom)
        assert entry["kind"] == "file"
        assert entry["size_bytes"] == 10
        assert entry["modified_at"] == pytest.approx(rom.stat().st_mtime)

    def test_a_directory_is_reported_without_its_recursive_total(self, adapter, tmp_path):
        # The whole reason the search is affordable: a single multi-file install
        # can hold tens of thousands of files, and this read must never walk one.
        game = tmp_path / "Game (U)"
        (game / "sub").mkdir(parents=True)
        (game / "disc1.bin").write_bytes(b"a" * 100)
        (game / "sub" / "disc2.bin").write_bytes(b"b" * 55)
        (entry,) = adapter.list_top_level_entries(str(tmp_path))
        assert entry["kind"] == "dir"
        assert entry["size_bytes"] == 0

    def test_nested_entries_are_not_listed(self, adapter, tmp_path):
        (tmp_path / "Game (U)").mkdir()
        (tmp_path / "Game (U)" / "disc.bin").write_bytes(b"x")
        (tmp_path / "loose.gba").write_bytes(b"y")
        assert sorted(entry["name"] for entry in adapter.list_top_level_entries(str(tmp_path))) == [
            "Game (U)",
            "loose.gba",
        ]


class TestTheAdmissionRule:
    """What an entry *is*, judged without following it — the same rule for both listings."""

    def _kinds(self, adapter, directory) -> dict[str, str]:
        full = {entry["name"]: entry["kind"] for entry in adapter.list_top_level_entries(str(directory))}
        lean = {entry["name"]: entry["kind"] for entry in adapter.list_top_level_names(str(directory))}
        assert full == lean, "the two listings must admit the same set, with the same kinds"
        return full

    def test_a_file_and_a_directory_are_what_they_are(self, adapter, tmp_path):
        (tmp_path / "rom.gba").write_bytes(b"x")
        (tmp_path / "Game (U)").mkdir()
        assert self._kinds(adapter, tmp_path) == {"rom.gba": "file", "Game (U)": "dir"}

    def test_a_link_is_a_link_however_well_its_target_resolves(self, adapter, tmp_path):
        # Following would report an ordinary file here, and an install row
        # pointing at a link can never be removed — the uninstall path refuses
        # one outright — so the link is judged, never its target.
        (tmp_path / "real.gba").write_bytes(b"x")
        (tmp_path / "link.gba").symlink_to(tmp_path / "real.gba")
        assert self._kinds(adapter, tmp_path)["link.gba"] == "link"

    def test_a_link_to_a_directory_is_a_link_too(self, adapter, tmp_path):
        target = tmp_path / "elsewhere"
        (target / "disc.bin").parent.mkdir(parents=True)
        (target / "disc.bin").write_bytes(b"x")
        platform = tmp_path / "psx"
        platform.mkdir()
        (platform / "Game (U)").symlink_to(target)
        assert self._kinds(adapter, platform) == {"Game (U)": "link"}

    def test_a_link_pointing_nowhere_is_a_link(self, adapter, tmp_path):
        (tmp_path / "dangling.gba").symlink_to(tmp_path / "gone.gba")
        assert self._kinds(adapter, tmp_path) == {"dangling.gba": "link"}

    def test_a_named_pipe_is_not_listed_at_all(self, adapter, tmp_path):
        # It reported as an ordinary zero-byte file and was offered as a game.
        # "File or directory" has no truthful answer for a FIFO, so it has none.
        os.mkfifo(str(tmp_path / "Game (U).sfc"))
        (tmp_path / "real.gba").write_bytes(b"x")
        assert self._kinds(adapter, tmp_path) == {"real.gba": "file"}

    def test_a_unix_socket_is_not_listed_either(self, adapter, tmp_path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(tmp_path / "Game (U).sfc"))
            (tmp_path / "real.gba").write_bytes(b"x")
            assert self._kinds(adapter, tmp_path) == {"real.gba": "file"}
        finally:
            sock.close()


class TestDescribePathDoesNotFollow:
    """What occupies a path, judged without following — item 3's whole subject."""

    def test_a_file_is_described_as_before(self, adapter, tmp_path):
        rom = tmp_path / "Game.gba"
        rom.write_bytes(b"0123456789")
        described = adapter.describe_path(str(rom))
        assert described is not None
        assert described["kind"] == FILE
        assert described["size_bytes"] == 10

    def test_a_link_to_a_real_file_is_reported_as_a_link(self, adapter, tmp_path):
        # Following described it as ordinary content, and the occupied-target
        # dialog then offered to adopt it — an install row the UI can never undo.
        (tmp_path / "real.gba").write_bytes(b"0123456789")
        link = tmp_path / "Game.gba"
        link.symlink_to(tmp_path / "real.gba")
        described = adapter.describe_path(str(link))
        assert described is not None
        assert described["kind"] == LINK

    def test_a_link_pointing_nowhere_still_occupies_its_path(self, adapter, tmp_path):
        # Reported as "nothing here", the finalize ``os.replace`` destroyed it
        # without a word. It is something, and the caller has to be told.
        link = tmp_path / "Game.gba"
        link.symlink_to(tmp_path / "gone.gba")
        described = adapter.describe_path(str(link))
        assert described is not None
        assert described["kind"] == LINK

    def test_a_link_to_a_directory_is_not_described_as_a_directory(self, adapter, tmp_path):
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "disc.bin").write_bytes(b"x" * 32)
        link = tmp_path / "Game"
        link.symlink_to(tmp_path / "elsewhere")
        described = adapter.describe_path(str(link))
        assert described is not None
        assert described["kind"] == LINK
        # And no tree walk was charged for a link's own size.
        assert described["size_bytes"] != 32

    def test_nothing_at_all_is_still_nothing(self, adapter, tmp_path):
        assert adapter.describe_path(str(tmp_path / "gone.gba")) is None

    def test_a_named_pipe_occupies_its_path_with_no_kind_at_all(self, adapter, tmp_path):
        # It came back as an ordinary zero-byte file, so the dialog said "a file
        # is already in place", offered it, and the uninstall path could then
        # never remove the row. The listings leave one out; this door cannot,
        # because something that is there must not be reported as nothing.
        pipe = tmp_path / "Game.gba"
        os.mkfifo(str(pipe))

        described = adapter.describe_path(str(pipe))

        assert described is not None
        assert described["kind"] is None
        assert described["path"] == str(pipe)

    def test_a_unix_socket_answers_the_same_way(self, adapter, tmp_path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(tmp_path / "Game.gba"))

            described = adapter.describe_path(str(tmp_path / "Game.gba"))

            assert described is not None
            assert described["kind"] is None
        finally:
            sock.close()

    def test_the_kinds_it_answers_are_the_kinds_the_listings_admit(self, adapter, tmp_path):
        # The two doors ask one function, so what one calls a link the other
        # cannot call a file. The pipe is the asymmetry, and it is the only one:
        # the listing leaves it out where this reports it kindless.
        (tmp_path / "real.gba").write_bytes(b"x")
        (tmp_path / "Game (U).gba").write_bytes(b"x")
        (tmp_path / "Game (E)").mkdir()
        (tmp_path / "Game (J).gba").symlink_to(tmp_path / "real.gba")
        os.mkfifo(str(tmp_path / "Game (I).gba"))

        listed = {entry["name"]: entry["kind"] for entry in adapter.list_top_level_entries(str(tmp_path))}
        described = {
            name: (adapter.describe_path(str(tmp_path / name)) or {}).get("kind")
            for name in ("Game (U).gba", "Game (E)", "Game (J).gba", "Game (I).gba")
        }

        assert described == {"Game (U).gba": FILE, "Game (E)": DIR, "Game (J).gba": LINK, "Game (I).gba": None}
        assert listed == {"real.gba": FILE, "Game (U).gba": FILE, "Game (E)": DIR, "Game (J).gba": LINK}


class _StatRecordingEntry:
    """A ``scandir`` entry that notes every ``stat()`` the adapter asks it for.

    ``os.DirEntry.stat`` is a C method, so patching ``os.stat`` would not see it —
    a counter built that way reads zero however the adapter is written. This sits
    where the adapter actually touches the entry.
    """

    def __init__(self, entry: os.DirEntry[str], stat_calls: list[str]) -> None:
        self._entry = entry
        self._stat_calls = stat_calls
        self.name = entry.name
        self.path = entry.path

    def is_symlink(self):
        return self._entry.is_symlink()

    def is_dir(self, **kwargs):
        return self._entry.is_dir(**kwargs)

    def is_file(self, **kwargs):
        return self._entry.is_file(**kwargs)

    def stat(self, **kwargs):
        self._stat_calls.append(self._entry.path)
        return self._entry.stat(**kwargs)


def _record_scandir_stats(monkeypatch, stat_calls: list[str]) -> None:
    """Route the adapter's ``os.scandir`` through entries that record their stats."""
    real_scandir = os.scandir

    @contextlib.contextmanager
    def recording_scandir(directory):
        with real_scandir(directory) as entries:
            yield [_StatRecordingEntry(entry, stat_calls) for entry in entries]

    monkeypatch.setattr(os, "scandir", recording_scandir)


class TestListTopLevelNames:
    def test_a_missing_directory_lists_nothing(self, adapter, tmp_path):
        assert adapter.list_top_level_names(str(tmp_path / "nope")) == ()

    def test_it_reports_the_same_names_and_kinds_as_the_full_listing(self, adapter, tmp_path):
        (tmp_path / "Game (U)").mkdir()
        (tmp_path / "Game (U)" / "disc.bin").write_bytes(b"x")
        (tmp_path / "loose.gba").write_bytes(b"y")
        (tmp_path / "link.gba").symlink_to(tmp_path / "loose.gba")

        lean = adapter.list_top_level_names(str(tmp_path))

        assert {entry["name"]: (entry["path"], entry["kind"]) for entry in lean} == {
            entry["name"]: (entry["path"], entry["kind"]) for entry in adapter.list_top_level_entries(str(tmp_path))
        }

    def test_it_carries_no_size_or_mtime(self, adapter, tmp_path):
        # The saving IS the omission: one `stat` per ROM, on every game page.
        (tmp_path / "Game (U).gba").write_bytes(b"0123456789")
        (entry,) = adapter.list_top_level_names(str(tmp_path))
        assert set(entry) == {"name", "path", "kind"}

    def test_it_stats_nothing_per_entry(self, adapter, tmp_path, monkeypatch):
        (tmp_path / "Game (U).gba").write_bytes(b"x")
        (tmp_path / "Other (U).gba").write_bytes(b"y")
        stat_calls: list[str] = []
        _record_scandir_stats(monkeypatch, stat_calls)

        assert len(adapter.list_top_level_names(str(tmp_path))) == 2
        assert stat_calls == []

    def test_the_full_listing_does_stat_each_entry(self, adapter, tmp_path, monkeypatch):
        # The control for the test above: same instrument, same tree, and it does
        # see the calls — so "no stats" is a statement about the leaner read and
        # not about an instrument that cannot detect one.
        (tmp_path / "Game (U).gba").write_bytes(b"x")
        (tmp_path / "Other (U).gba").write_bytes(b"y")
        stat_calls: list[str] = []
        _record_scandir_stats(monkeypatch, stat_calls)

        assert len(adapter.list_top_level_entries(str(tmp_path))) == 2
        assert sorted(os.path.basename(path) for path in stat_calls) == ["Game (U).gba", "Other (U).gba"]


class TestChecksum:
    def test_md5_matches_hashlib(self, adapter, tmp_path):
        import hashlib

        f = tmp_path / "a.bin"
        f.write_bytes(b"the quick brown fox" * 100)
        assert adapter.checksum(str(f), "md5") == hashlib.md5(f.read_bytes()).hexdigest()

    def test_crc32_is_eight_lowercase_hex_digits(self, adapter, tmp_path):
        import zlib

        f = tmp_path / "a.bin"
        f.write_bytes(b"\xff" * 3)
        expected = f"{zlib.crc32(f.read_bytes()) & 0xFFFFFFFF:08x}"
        assert adapter.checksum(str(f), "crc32") == expected
        assert len(expected) == 8

    def test_crc32_of_an_empty_file_is_zero_padded(self, adapter, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert adapter.checksum(str(f), "crc32") == "00000000"

    def test_progress_reports_byte_deltas_summing_to_the_file_size(self, adapter, tmp_path):
        f = tmp_path / "big.bin"
        f.write_bytes(b"x" * (3 * 1024 * 1024 + 7))
        deltas: list[int] = []
        adapter.checksum(str(f), "md5", deltas.append)
        assert sum(deltas) == 3 * 1024 * 1024 + 7
        assert len(deltas) > 1  # chunked, not one gulp

    def test_an_unknown_algorithm_raises(self, adapter, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"x")
        with pytest.raises(ValueError, match="sha512"):
            adapter.checksum(str(f), "sha512")

    def test_a_missing_file_raises(self, adapter, tmp_path):
        with pytest.raises(OSError):
            adapter.checksum(str(tmp_path / "nope.bin"), "md5")


class TestListArchiveMembers:
    """The central directory answers what is inside without decompressing it."""

    def _zip(self, path, members: dict[str, bytes], *, add_dir: str | None = None) -> None:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            if add_dir is not None:
                archive.writestr(zipfile.ZipInfo(add_dir), b"")
            for name, data in members.items():
                archive.writestr(name, data)

    def test_states_each_members_name_uncompressed_size_and_crc32(self, adapter, tmp_path):
        import zlib

        payload = b"rom bytes" * 512
        f = tmp_path / "Game.zip"
        self._zip(f, {"Game.gba": payload})

        assert adapter.list_archive_members(str(f)) == (
            {
                "name": "Game.gba",
                "size_bytes": len(payload),
                "crc32": f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
            },
        )

    def test_reads_the_uncompressed_size_not_the_stored_one(self, adapter, tmp_path):
        # A compressible member is much smaller inside the archive; the size the
        # server states is the one it decompresses to.
        payload = b"a" * 10_000
        f = tmp_path / "Game.zip"
        self._zip(f, {"Game.gba": payload})

        assert adapter.list_archive_members(str(f))[0]["size_bytes"] == 10_000
        assert f.stat().st_size < 10_000

    def test_keeps_each_members_path_inside_the_archive(self, adapter, tmp_path):
        f = tmp_path / "Game.zip"
        self._zip(f, {"disc1/track1.bin": b"one", "disc2/track1.bin": b"two"})

        assert [member["name"] for member in adapter.list_archive_members(str(f))] == [
            "disc1/track1.bin",
            "disc2/track1.bin",
        ]

    def test_directory_entries_are_omitted(self, adapter, tmp_path):
        f = tmp_path / "Game.zip"
        self._zip(f, {"disc1/track1.bin": b"one"}, add_dir="disc1/")

        assert [member["name"] for member in adapter.list_archive_members(str(f))] == ["disc1/track1.bin"]

    def test_an_empty_archive_lists_nothing_rather_than_failing(self, adapter, tmp_path):
        f = tmp_path / "Game.zip"
        self._zip(f, {})

        assert adapter.list_archive_members(str(f)) == ()

    def test_a_file_that_is_not_a_zip_cannot_be_looked_inside(self, adapter, tmp_path):
        f = tmp_path / "Game.7z"
        f.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)

        assert adapter.list_archive_members(str(f)) is None

    def test_a_truncated_archive_cannot_be_looked_inside(self, adapter, tmp_path):
        source = tmp_path / "Game.zip"
        self._zip(source, {"Game.gba": b"rom bytes" * 512})
        truncated = tmp_path / "Truncated.zip"
        truncated.write_bytes(source.read_bytes()[: len(source.read_bytes()) // 2])

        assert adapter.list_archive_members(str(truncated)) is None

    def test_a_missing_path_cannot_be_looked_inside(self, adapter, tmp_path):
        assert adapter.list_archive_members(str(tmp_path / "nope.zip")) is None


class TestChecksumArchiveMember:
    """A member's digest is taken over its decompressed bytes, as RomM's is."""

    def _zip(self, path, members: dict[str, bytes]) -> None:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def test_md5_is_the_members_content_not_the_containers(self, adapter, tmp_path):
        import hashlib

        payload = b"rom bytes" * 512
        f = tmp_path / "Game.zip"
        self._zip(f, {"Game.gba": payload})

        digest = adapter.checksum_archive_member(str(f), "Game.gba", "md5")

        assert digest == hashlib.md5(payload).hexdigest()
        assert digest != hashlib.md5(f.read_bytes()).hexdigest()

    def test_crc32_matches_the_central_directory(self, adapter, tmp_path):
        payload = b"rom bytes" * 512
        f = tmp_path / "Game.zip"
        self._zip(f, {"Game.gba": payload})

        listed = adapter.list_archive_members(str(f))[0]["crc32"]

        assert adapter.checksum_archive_member(str(f), "Game.gba", "crc32") == listed

    def test_progress_reports_deltas_summing_to_the_uncompressed_size(self, adapter, tmp_path):
        payload = b"x" * (3 * 1024 * 1024 + 7)
        f = tmp_path / "Game.zip"
        self._zip(f, {"Game.gba": payload})
        deltas: list[int] = []

        adapter.checksum_archive_member(str(f), "Game.gba", "md5", deltas.append)

        assert sum(deltas) == len(payload)
        assert len(deltas) > 1  # streamed, never held whole in memory

    def test_a_member_the_archive_does_not_hold_raises(self, adapter, tmp_path):
        f = tmp_path / "Game.zip"
        self._zip(f, {"Game.gba": b"rom"})

        with pytest.raises(KeyError):
            adapter.checksum_archive_member(str(f), "Other.gba", "md5")

    def test_an_unknown_algorithm_raises(self, adapter, tmp_path):
        f = tmp_path / "Game.zip"
        self._zip(f, {"Game.gba": b"rom"})

        with pytest.raises(ValueError, match="sha512"):
            adapter.checksum_archive_member(str(f), "Game.gba", "sha512")

    def test_a_container_that_is_not_a_zip_raises(self, adapter, tmp_path):
        f = tmp_path / "Game.zip"
        f.write_bytes(b"not an archive")

        with pytest.raises(zipfile.BadZipFile):
            adapter.checksum_archive_member(str(f), "Game.gba", "md5")


class TestProtocolMethodCount:
    """Sanity check that every Protocol method has at least one test class."""

    def test_protocol_methods_covered(self):
        method_names = {
            "exists",
            "describe_path",
            "list_top_level_entries",
            "list_top_level_names",
            "checksum",
            "list_archive_members",
            "checksum_archive_member",
            "remove_file",
            "remove_tree",
            "make_dirs",
            "move_dir",
            "copy_file",
            "rename",
            "disk_free",
            "walk_files_matching_suffixes",
            "extract_zip",
            "decode_url_encoded_names",
            "scan_files_with_sizes",
            "write_text_atomic",
        }
        # All listed methods are implemented on the concrete adapter.
        for name in method_names:
            assert hasattr(DownloadFileAdapter(), name), f"missing {name}"
