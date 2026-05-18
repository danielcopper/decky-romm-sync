"""Tests for CoverArtFileStoreAdapter — raw filesystem ops for cover art."""

from __future__ import annotations

import pytest

from adapters.cover_art_file_store import CoverArtFileStoreAdapter


@pytest.fixture
def store() -> CoverArtFileStoreAdapter:
    return CoverArtFileStoreAdapter()


class TestExists:
    def test_true_for_existing_file(self, store, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        assert store.exists(str(f)) is True

    def test_true_for_directory(self, store, tmp_path):
        assert store.exists(str(tmp_path)) is True

    def test_false_for_missing(self, store, tmp_path):
        assert store.exists(str(tmp_path / "missing.png")) is False


class TestRemove:
    def test_removes_existing(self, store, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        store.remove_file(str(f))
        assert not f.exists()

    def test_missing_is_noop(self, store, tmp_path):
        # idempotent: must not raise
        store.remove_file(str(tmp_path / "missing.png"))

    def test_propagates_non_filenotfound_errors(self, store, tmp_path):
        # Removing a non-empty directory raises IsADirectoryError or
        # OSError — anything other than FileNotFoundError must surface.
        with pytest.raises(OSError):
            store.remove_file(str(tmp_path))


class TestRename:
    def test_renames_to_new_path(self, store, tmp_path):
        src = tmp_path / "src.png"
        src.write_bytes(b"data")
        dst = tmp_path / "dst.png"

        store.rename(str(src), str(dst))

        assert not src.exists()
        assert dst.exists()
        assert dst.read_bytes() == b"data"

    def test_replaces_existing_dst(self, store, tmp_path):
        src = tmp_path / "src.png"
        src.write_bytes(b"new")
        dst = tmp_path / "dst.png"
        dst.write_bytes(b"old")

        store.rename(str(src), str(dst))

        assert not src.exists()
        assert dst.read_bytes() == b"new"

    def test_missing_src_raises(self, store, tmp_path):
        with pytest.raises(FileNotFoundError):
            store.rename(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestListdir:
    def test_returns_entries(self, store, tmp_path):
        (tmp_path / "a.png").write_bytes(b"")
        (tmp_path / "b.png").write_bytes(b"")
        entries = store.listdir(str(tmp_path))
        assert sorted(entries) == ["a.png", "b.png"]

    def test_empty_dir(self, store, tmp_path):
        assert store.listdir(str(tmp_path)) == []

    def test_missing_dir_raises(self, store, tmp_path):
        with pytest.raises(FileNotFoundError):
            store.listdir(str(tmp_path / "missing"))


class TestIsdir:
    def test_true_for_directory(self, store, tmp_path):
        assert store.is_dir(str(tmp_path)) is True

    def test_false_for_file(self, store, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"")
        assert store.is_dir(str(f)) is False

    def test_false_for_missing(self, store, tmp_path):
        assert store.is_dir(str(tmp_path / "missing")) is False


class TestReadBytes:
    def test_roundtrip(self, store, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert store.read_bytes(str(f)) == b"\x89PNG\r\n\x1a\n"

    def test_empty_file(self, store, tmp_path):
        f = tmp_path / "empty.png"
        f.write_bytes(b"")
        assert store.read_bytes(str(f)) == b""

    def test_missing_raises(self, store, tmp_path):
        with pytest.raises(FileNotFoundError):
            store.read_bytes(str(tmp_path / "missing.png"))
