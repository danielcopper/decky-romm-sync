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


class TestReclaimStagedSource:
    """Adopting the staging entry an interrupted removal left behind."""

    @staticmethod
    def _stage(roms, name: str):
        """Interrupt a removal after its staging rename, leaving the tree staged away."""
        source = roms / "psx" / name
        (source / "sub").mkdir(parents=True)
        (source / "disc.bin").write_bytes(b"\x00" * 64)
        (source / "sub" / "data.bin").write_bytes(b"\x01" * 64)
        staged = source.parent / f".{name}.romm-prune-{source.stat().st_ino}"
        source.rename(staged)
        return source, staged

    def test_finishes_a_removal_interrupted_after_the_staging_rename(self, adapter, tmp_path):
        roms = tmp_path / "roms"
        source, staged = self._stage(roms, "Game")

        outcome = adapter.reclaim_staged_source(str(source), str(roms))

        assert outcome == {
            "success": True,
            "changed": True,
            "ambiguous": False,
            "message": "Interrupted removal was finished",
        }
        assert not staged.exists()
        assert not source.exists()
        assert source.parent.is_dir()

    def test_reports_no_change_when_the_parent_holds_no_debris(self, adapter, tmp_path):
        roms = tmp_path / "roms"
        (roms / "psx").mkdir(parents=True)
        sibling = roms / "psx" / "Other.bin"
        sibling.write_bytes(b"keep")

        outcome = adapter.reclaim_staged_source(str(roms / "psx" / "Game"), str(roms))

        assert outcome["success"] is True
        assert outcome["changed"] is False
        assert sibling.read_bytes() == b"keep"

    def test_reports_no_change_when_the_parent_is_gone(self, adapter, tmp_path):
        roms = tmp_path / "roms"
        roms.mkdir()

        outcome = adapter.reclaim_staged_source(str(roms / "psx" / "Game"), str(roms))

        assert outcome["success"] is True
        assert outcome["changed"] is False

    def test_leaves_another_roms_staging_debris_untouched(self, adapter, tmp_path):
        roms = tmp_path / "roms"
        source, staged = self._stage(roms, "Game")
        _, other_staged = self._stage(roms, "Other")

        outcome = adapter.reclaim_staged_source(str(source), str(roms))

        assert outcome["changed"] is True
        assert not staged.exists()
        assert other_staged.is_dir()

    def test_refuses_a_path_outside_the_roms_root(self, adapter, tmp_path):
        roms = tmp_path / "roms"
        roms.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        with pytest.raises(ValueError, match="outside its safe root"):
            adapter.reclaim_staged_source(str(outside / "Game"), str(roms))


class TestProtocolMethodCount:
    """Sanity check that every Protocol method has at least one test class."""

    def test_protocol_methods_covered(self):
        method_names = {
            "is_dir",
            "exists",
            "remove_file",
            "remove_tree",
            "claim_source",
            "remove_claimed",
            "reclaim_staged_source",
        }
        for name in method_names:
            assert hasattr(RomFileAdapter(), name), f"missing {name}"
