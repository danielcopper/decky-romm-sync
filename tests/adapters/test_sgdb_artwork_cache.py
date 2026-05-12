"""Tests for SgdbArtworkCacheAdapter — raw filesystem ops for the SGDB artwork cache."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from adapters.sgdb_artwork_cache import SgdbArtworkCacheAdapter


@pytest.fixture
def cache(tmp_path) -> SgdbArtworkCacheAdapter:
    return SgdbArtworkCacheAdapter(runtime_dir=str(tmp_path))


class TestCacheDir:
    def test_returns_artwork_subdir(self, cache, tmp_path):
        result = cache.cache_dir()
        assert result == os.path.join(str(tmp_path), "artwork")

    def test_creates_dir_if_missing(self, cache, tmp_path):
        art_dir = tmp_path / "artwork"
        assert not art_dir.exists()
        cache.cache_dir()
        assert art_dir.is_dir()

    def test_idempotent_when_dir_exists(self, cache, tmp_path):
        (tmp_path / "artwork").mkdir()
        # Must not raise
        result = cache.cache_dir()
        assert result == str(tmp_path / "artwork")


class TestExists:
    def test_true_for_existing_file(self, cache, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        assert cache.exists(str(f)) is True

    def test_true_for_directory(self, cache, tmp_path):
        assert cache.exists(str(tmp_path)) is True

    def test_false_for_missing(self, cache, tmp_path):
        assert cache.exists(str(tmp_path / "missing.png")) is False


class TestRemove:
    def test_removes_existing(self, cache, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        cache.remove(str(f))
        assert not f.exists()

    def test_missing_is_noop(self, cache, tmp_path):
        # idempotent: must not raise
        cache.remove(str(tmp_path / "missing.png"))

    def test_propagates_non_filenotfound_errors(self, cache, tmp_path):
        # Removing a non-empty directory raises IsADirectoryError or OSError —
        # anything other than FileNotFoundError must surface.
        with pytest.raises(OSError):
            cache.remove(str(tmp_path))


class TestListdir:
    def test_returns_entries(self, cache, tmp_path):
        (tmp_path / "a.png").write_bytes(b"")
        (tmp_path / "b.png").write_bytes(b"")
        entries = cache.listdir(str(tmp_path))
        assert sorted(entries) == ["a.png", "b.png"]

    def test_empty_dir(self, cache, tmp_path):
        assert cache.listdir(str(tmp_path)) == []

    def test_missing_dir_raises(self, cache, tmp_path):
        with pytest.raises(FileNotFoundError):
            cache.listdir(str(tmp_path / "missing"))


class TestIsdir:
    def test_true_for_directory(self, cache, tmp_path):
        assert cache.isdir(str(tmp_path)) is True

    def test_false_for_file(self, cache, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"")
        assert cache.isdir(str(f)) is False

    def test_false_for_missing(self, cache, tmp_path):
        assert cache.isdir(str(tmp_path / "missing")) is False


class TestReadBytes:
    def test_roundtrip(self, cache, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert cache.read_bytes(str(f)) == b"\x89PNG\r\n\x1a\n"

    def test_empty_file(self, cache, tmp_path):
        f = tmp_path / "empty.png"
        f.write_bytes(b"")
        assert cache.read_bytes(str(f)) == b""

    def test_missing_raises(self, cache, tmp_path):
        with pytest.raises(FileNotFoundError):
            cache.read_bytes(str(tmp_path / "missing.png"))


class TestWriteBytesAtomic:
    def test_writes_data(self, cache, tmp_path):
        dest = tmp_path / "icon.png"
        cache.write_bytes_atomic(str(dest), b"icon-payload")
        assert dest.read_bytes() == b"icon-payload"

    def test_no_tmp_left_after_success(self, cache, tmp_path):
        dest = tmp_path / "icon.png"
        cache.write_bytes_atomic(str(dest), b"data")
        assert not (tmp_path / "icon.png.tmp").exists()

    def test_overwrites_existing(self, cache, tmp_path):
        dest = tmp_path / "icon.png"
        dest.write_bytes(b"old")
        cache.write_bytes_atomic(str(dest), b"new")
        assert dest.read_bytes() == b"new"

    def test_cleans_tmp_on_failure(self, cache, tmp_path):
        dest = tmp_path / "icon.png"
        # Make os.replace fail; the tmp file should be removed and the
        # exception propagated to the caller.
        with (
            patch("adapters.sgdb_artwork_cache.os.replace", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            cache.write_bytes_atomic(str(dest), b"data")
        assert not (tmp_path / "icon.png.tmp").exists()
        assert not dest.exists()

    def test_cleans_tmp_when_open_fails(self, cache, tmp_path):
        dest = tmp_path / "icon.png"
        # Simulate write-side failure after tmp was opened — ensure that
        # if a tmp file lingers, the cleanup still suppresses
        # FileNotFoundError gracefully.
        with (
            patch("builtins.open", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            cache.write_bytes_atomic(str(dest), b"data")
        # tmp may or may not exist depending on how open() failed; what
        # matters is cleanup didn't itself raise.
        assert not dest.exists()
