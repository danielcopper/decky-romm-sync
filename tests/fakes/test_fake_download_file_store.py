"""The fake's listings held to the real adapter's, on the tree that told them apart.

A fake that projects one listing from the other cannot exhibit a disagreement
between them, which is exactly how a real one survived: the full listing dropped
what it could not ``stat`` while the lean one kept it, so the game page saw an
entry the Download click did not. These tests run the same tree through both
implementations and compare.
"""

from __future__ import annotations

import pytest

from adapters.download_file import DownloadFileAdapter
from fakes.fake_download_file_store import FakeDownloadFileStore


@pytest.fixture
def real() -> DownloadFileAdapter:
    return DownloadFileAdapter()


def _stage_real(tmp_path) -> str:
    """A folder with a file, a directory, and a link pointing nowhere."""
    (tmp_path / "Game (U).sfc").write_bytes(b"cartridge")
    (tmp_path / "Game (E)").mkdir()
    (tmp_path / "Game (E)" / "rom.sfc").write_bytes(b"x")
    (tmp_path / "Game (J).sfc").symlink_to(tmp_path / "gone.sfc")
    return str(tmp_path)


def _stage_fake(directory: str) -> FakeDownloadFileStore:
    """The same folder, staged in the fake."""
    store = FakeDownloadFileStore()
    store.files[f"{directory}/Game (U).sfc"] = b"cartridge"
    store.dirs.add(f"{directory}/Game (E)")
    store.files[f"{directory}/Game (E)/rom.sfc"] = b"x"
    store.unreadable.add(f"{directory}/Game (J).sfc")
    store.broken_symlinks.add(f"{directory}/Game (J).sfc")
    return store


def _shape(entries) -> set[tuple[str, bool, bool]]:
    """What both listings must agree on, ignoring numbers only one of them has."""
    return {(entry["name"], entry["is_dir"], entry.get("readable", True)) for entry in entries}


class TestFakeMatchesTheAdapter:
    def test_the_full_listing_agrees_entry_for_entry(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        assert _shape(fake.list_top_level_entries(directory)) == _shape(real.list_top_level_entries(directory))

    def test_the_lean_listing_agrees_entry_for_entry(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        assert _shape(fake.list_top_level_names(directory)) == _shape(real.list_top_level_names(directory))

    def test_both_admit_the_same_set_in_the_fake_as_in_the_adapter(self, real, tmp_path):
        # The property the projection used to guarantee by construction and now
        # has to hold on its own merits — in both implementations.
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        for store in (real, fake):
            full = {entry["path"] for entry in store.list_top_level_entries(directory)}
            lean = {entry["path"] for entry in store.list_top_level_names(directory)}
            assert full == lean

    def test_the_unreadable_entry_carries_no_measurements_in_either(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        for store in (real, fake):
            (entry,) = [e for e in store.list_top_level_entries(directory) if e["name"] == "Game (J).sfc"]
            assert entry["readable"] is False
            assert entry["size_bytes"] == 0
            assert entry["modified_at"] == 0.0

    def test_the_broken_link_is_removable_in_either(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        for store in (real, fake):
            assert store.is_broken_symlink(f"{directory}/Game (J).sfc") is True
            assert store.is_broken_symlink(f"{directory}/Game (U).sfc") is False
