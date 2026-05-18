"""Tests for MigrationFileAdapter — raw filesystem ops for RetroDECK migration."""

from __future__ import annotations

import os

import pytest

from adapters.migration_file import MigrationFileAdapter


@pytest.fixture
def adapter() -> MigrationFileAdapter:
    return MigrationFileAdapter()


class TestExists:
    def test_true_for_existing_file(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        assert adapter.exists(str(f)) is True

    def test_true_for_directory(self, adapter, tmp_path):
        assert adapter.exists(str(tmp_path)) is True

    def test_false_for_missing(self, adapter, tmp_path):
        assert adapter.exists(str(tmp_path / "missing.rom")) is False


class TestIsDir:
    def test_true_for_directory(self, adapter, tmp_path):
        assert adapter.is_dir(str(tmp_path)) is True

    def test_false_for_file(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        assert adapter.is_dir(str(f)) is False

    def test_false_for_missing(self, adapter, tmp_path):
        assert adapter.is_dir(str(tmp_path / "missing")) is False


class TestMakeDirs:
    def test_creates_directory(self, adapter, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        adapter.make_dirs(str(target))
        assert target.is_dir()

    def test_idempotent_when_exists(self, adapter, tmp_path):
        adapter.make_dirs(str(tmp_path))  # already exists — must not raise


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
        d = tmp_path / "tree"
        d.mkdir()
        (d / "a").write_bytes(b"")
        (d / "b").write_bytes(b"")
        adapter.remove_tree(str(d))
        assert not d.exists()

    def test_missing_is_noop(self, adapter, tmp_path):
        # Idempotent on missing directory
        adapter.remove_tree(str(tmp_path / "missing"))

    def test_removes_nested(self, adapter, tmp_path):
        d = tmp_path / "tree"
        nested = d / "sub" / "deeper"
        nested.mkdir(parents=True)
        (nested / "file").write_bytes(b"data")
        adapter.remove_tree(str(d))
        assert not d.exists()


class TestMove:
    def test_moves_file_same_dir(self, adapter, tmp_path):
        src = tmp_path / "src.rom"
        dst = tmp_path / "dst.rom"
        src.write_bytes(b"data")
        adapter.move(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"data"

    def test_moves_file_across_dirs(self, adapter, tmp_path):
        src_dir = tmp_path / "old"
        dst_dir = tmp_path / "new"
        src_dir.mkdir()
        dst_dir.mkdir()
        src = src_dir / "a.rom"
        dst = dst_dir / "a.rom"
        src.write_bytes(b"payload")
        adapter.move(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"payload"

    def test_missing_source_raises(self, adapter, tmp_path):
        with pytest.raises((FileNotFoundError, OSError)):
            adapter.move(str(tmp_path / "missing"), str(tmp_path / "dst"))


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
        with pytest.raises(OSError):
            adapter.rename(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestGetmtime:
    def test_returns_mtime_float(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        os.utime(str(f), (1_700_000_000.0, 1_700_000_000.0))
        assert adapter.get_mtime(str(f)) == pytest.approx(1_700_000_000.0)

    def test_missing_raises_oserror(self, adapter, tmp_path):
        with pytest.raises(OSError):
            adapter.get_mtime(str(tmp_path / "missing"))


class TestWalkFiles:
    def test_returns_os_walk_triples(self, adapter, tmp_path):
        # Build a small tree:
        # tmp_path/
        #   a.txt
        #   sub/
        #     b.txt
        (tmp_path / "a.txt").write_text("")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("")

        triples = adapter.walk_files(str(tmp_path))
        # Snapshot: expect 2 directories (root + sub).
        dirs_visited = {dp for dp, _dn, _fn in triples}
        assert str(tmp_path) in dirs_visited
        assert str(tmp_path / "sub") in dirs_visited
        # Locate the root triple by dirpath.
        root_triple = next(t for t in triples if t[0] == str(tmp_path))
        assert "sub" in root_triple[1]
        assert "a.txt" in root_triple[2]

    def test_returns_empty_for_empty_dir(self, adapter, tmp_path):
        triples = adapter.walk_files(str(tmp_path))
        # os.walk yields one entry for the root even when empty.
        assert len(triples) == 1
        assert triples[0][0] == str(tmp_path)
        assert triples[0][1] == []
        assert triples[0][2] == []

    def test_returns_empty_for_missing_dir(self, adapter, tmp_path):
        # os.walk silently returns nothing for a missing directory.
        triples = adapter.walk_files(str(tmp_path / "missing"))
        assert triples == []

    def test_triples_are_mutable_snapshots(self, adapter, tmp_path):
        """Caller can prune dirnames in place; we materialise to lists, not iterator state."""
        (tmp_path / "keep.txt").write_text("")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "x").write_text("")
        triples = adapter.walk_files(str(tmp_path))
        # Caller pruning the dirnames list is a no-op for our snapshot
        # (we don't drive os.walk lazily) — but the lists must be
        # mutable to mirror os.walk's API expectations.
        root_triple = next(t for t in triples if t[0] == str(tmp_path))
        root_triple[1][:] = [d for d in root_triple[1] if not d.startswith(".")]
        # The mutation should stick on the returned list.
        assert ".hidden" not in root_triple[1]


class TestProtocolMethodCount:
    """Sanity check that every Protocol method has at least one test class."""

    def test_protocol_methods_covered(self):
        method_names = {
            "exists",
            "is_dir",
            "make_dirs",
            "remove_file",
            "remove_tree",
            "move",
            "rename",
            "get_mtime",
            "walk_files",
        }
        for name in method_names:
            assert hasattr(MigrationFileAdapter(), name), f"missing {name}"
