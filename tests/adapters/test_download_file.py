"""Tests for DownloadFileAdapter — raw filesystem ops for ROM downloads."""

from __future__ import annotations

import zipfile
from unittest.mock import patch

import pytest

from adapters.download_file import DownloadFileAdapter


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


class TestScanFilesWithSizes:
    def test_returns_paths_and_sizes(self, adapter, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"\x00" * 10)
        (tmp_path / "b.bin").write_bytes(b"\x00" * 20)
        out = adapter.scan_files_with_sizes(str(tmp_path))
        sizes = {p: s for p, s in out}
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


class TestProtocolMethodCount:
    """Sanity check that every Protocol method has at least one test class."""

    def test_protocol_methods_covered(self):
        method_names = {
            "exists",
            "remove_file",
            "remove_tree",
            "make_dirs",
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
