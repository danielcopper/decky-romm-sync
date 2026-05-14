"""Tests for SaveFileAdapter — raw filesystem ops for local save files."""

from __future__ import annotations

import hashlib
import os
import re

import pytest

from adapters.save_file import SaveFileAdapter


@pytest.fixture
def save_files() -> SaveFileAdapter:
    return SaveFileAdapter()


class TestExists:
    def test_true_for_existing_file(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        assert save_files.exists(str(f)) is True

    def test_true_for_directory(self, save_files, tmp_path):
        assert save_files.exists(str(tmp_path)) is True

    def test_false_for_missing(self, save_files, tmp_path):
        assert save_files.exists(str(tmp_path / "missing.srm")) is False


class TestIsFile:
    def test_true_for_existing_file(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        assert save_files.is_file(str(f)) is True

    def test_false_for_directory(self, save_files, tmp_path):
        assert save_files.is_file(str(tmp_path)) is False

    def test_false_for_missing(self, save_files, tmp_path):
        assert save_files.is_file(str(tmp_path / "missing.srm")) is False


class TestIsDir:
    def test_true_for_directory(self, save_files, tmp_path):
        assert save_files.is_dir(str(tmp_path)) is True

    def test_false_for_file(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        assert save_files.is_dir(str(f)) is False

    def test_false_for_missing(self, save_files, tmp_path):
        assert save_files.is_dir(str(tmp_path / "missing")) is False


class TestMakeDirs:
    def test_creates_dir(self, save_files, tmp_path):
        target = tmp_path / "saves"
        save_files.make_dirs(str(target))
        assert target.is_dir()

    def test_creates_parents(self, save_files, tmp_path):
        target = tmp_path / "retrodeck" / "saves" / "gba"
        save_files.make_dirs(str(target))
        assert target.is_dir()

    def test_idempotent_when_dir_exists(self, save_files, tmp_path):
        target = tmp_path / "saves"
        target.mkdir()
        save_files.make_dirs(str(target))
        assert target.is_dir()


class TestRemove:
    def test_removes_existing(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        save_files.remove(str(f))
        assert not f.exists()

    def test_missing_is_noop(self, save_files, tmp_path):
        # Idempotent: must not raise.
        save_files.remove(str(tmp_path / "missing.srm"))

    def test_propagates_non_filenotfound_errors(self, save_files, tmp_path):
        # Removing a non-empty directory raises IsADirectoryError or OSError —
        # anything other than FileNotFoundError must surface.
        with pytest.raises(OSError):
            save_files.remove(str(tmp_path))


class TestRename:
    def test_renames_file(self, save_files, tmp_path):
        src = tmp_path / "a.tmp"
        dst = tmp_path / "a.srm"
        src.write_bytes(b"payload")
        save_files.rename(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"payload"

    def test_overwrites_existing_destination(self, save_files, tmp_path):
        src = tmp_path / "new.tmp"
        dst = tmp_path / "old.srm"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        save_files.rename(str(src), str(dst))
        assert dst.read_bytes() == b"new"
        assert not src.exists()

    def test_missing_source_raises(self, save_files, tmp_path):
        with pytest.raises(FileNotFoundError):
            save_files.rename(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestGetMtime:
    def test_returns_unix_timestamp(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        mtime = save_files.get_mtime(str(f))
        assert mtime == pytest.approx(f.stat().st_mtime)

    def test_missing_raises(self, save_files, tmp_path):
        with pytest.raises(OSError):
            save_files.get_mtime(str(tmp_path / "missing.srm"))


class TestGetSize:
    def test_returns_byte_count(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"abcdef")
        assert save_files.get_size(str(f)) == 6

    def test_zero_for_empty_file(self, save_files, tmp_path):
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")
        assert save_files.get_size(str(f)) == 0

    def test_missing_raises(self, save_files, tmp_path):
        with pytest.raises(OSError):
            save_files.get_size(str(tmp_path / "missing.srm"))


class TestChecksumMd5:
    def test_matches_hashlib(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        payload = b"save payload bytes"
        f.write_bytes(payload)
        expected = hashlib.md5(payload).hexdigest()
        assert save_files.checksum_md5(str(f)) == expected

    def test_returns_hex_digest_format(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        digest = save_files.checksum_md5(str(f))
        assert re.fullmatch(r"[0-9a-f]{32}", digest)

    def test_empty_file(self, save_files, tmp_path):
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")
        assert save_files.checksum_md5(str(f)) == hashlib.md5(b"").hexdigest()

    def test_streams_large_file(self, save_files, tmp_path):
        """Files larger than one chunk are hashed correctly."""
        f = tmp_path / "large.srm"
        # 25 KiB — well above the 8 KiB chunk size.
        payload = b"A" * (25 * 1024)
        f.write_bytes(payload)
        assert save_files.checksum_md5(str(f)) == hashlib.md5(payload).hexdigest()

    def test_missing_file_raises(self, save_files, tmp_path):
        with pytest.raises(FileNotFoundError):
            save_files.checksum_md5(str(tmp_path / "missing.srm"))


class TestMakeTempPath:
    def test_returns_existing_empty_file(self, save_files):
        path = save_files.make_temp_path()
        try:
            assert os.path.isfile(path)
            assert os.path.getsize(path) == 0
        finally:
            os.remove(path)

    def test_unique_paths_each_call(self, save_files):
        paths = [save_files.make_temp_path() for _ in range(3)]
        try:
            assert len(set(paths)) == 3
        finally:
            for p in paths:
                os.remove(p)

    def test_respects_suffix(self, save_files):
        path = save_files.make_temp_path(suffix=".srm.tmp")
        try:
            assert path.endswith(".srm.tmp")
        finally:
            os.remove(path)

    def test_no_suffix_default(self, save_files):
        path = save_files.make_temp_path()
        try:
            assert os.path.isfile(path)
        finally:
            os.remove(path)
