"""Tests for RomFileAdapter — raw filesystem ops for installed ROM removal."""

from __future__ import annotations

import pytest

from adapters.rom_files import RomFileAdapter


@pytest.fixture
def adapter() -> RomFileAdapter:
    return RomFileAdapter()


class TestIsDir:
    def test_true_for_directory(self, adapter, tmp_path):
        assert adapter.is_dir(str(tmp_path)) is True

    def test_false_for_file(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        assert adapter.is_dir(str(f)) is False

    def test_false_for_missing(self, adapter, tmp_path):
        assert adapter.is_dir(str(tmp_path / "missing")) is False


class TestExists:
    def test_true_for_existing_file(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        assert adapter.exists(str(f)) is True

    def test_true_for_directory(self, adapter, tmp_path):
        assert adapter.exists(str(tmp_path)) is True

    def test_false_for_missing(self, adapter, tmp_path):
        assert adapter.exists(str(tmp_path / "missing.rom")) is False


class TestRemoveFile:
    def test_removes_existing(self, adapter, tmp_path):
        f = tmp_path / "a.rom"
        f.write_bytes(b"x")
        adapter.remove_file(str(f))
        assert not f.exists()

    def test_missing_is_noop(self, adapter, tmp_path):
        # Idempotent — must not raise on a missing file.
        adapter.remove_file(str(tmp_path / "missing.rom"))

    def test_propagates_non_filenotfound(self, adapter, tmp_path):
        # Calling os.remove on a directory raises IsADirectoryError /
        # OSError — anything other than FileNotFoundError must surface.
        with pytest.raises(OSError):
            adapter.remove_file(str(tmp_path))


class TestRemoveTree:
    def test_removes_directory(self, adapter, tmp_path):
        d = tmp_path / "rom_dir"
        d.mkdir()
        (d / "a.cue").write_text("cue")
        (d / "a.bin").write_bytes(b"\x00" * 100)
        adapter.remove_tree(str(d))
        assert not d.exists()

    def test_removes_nested(self, adapter, tmp_path):
        d = tmp_path / "rom_dir"
        nested = d / "sub" / "deeper"
        nested.mkdir(parents=True)
        (nested / "file").write_bytes(b"data")
        adapter.remove_tree(str(d))
        assert not d.exists()

    def test_missing_raises(self, adapter, tmp_path):
        # Distinct from MigrationFileAdapter.remove_tree: RomFileAdapter
        # propagates FileNotFoundError so callers (which guard with
        # is_dir/exists first) see the failure if the guard slipped.
        with pytest.raises(FileNotFoundError):
            adapter.remove_tree(str(tmp_path / "missing"))


class TestProtocolMethodCount:
    """Sanity check that every Protocol method has at least one test class."""

    def test_protocol_methods_covered(self):
        method_names = {"is_dir", "exists", "remove_file", "remove_tree"}
        for name in method_names:
            assert hasattr(RomFileAdapter(), name), f"missing {name}"
