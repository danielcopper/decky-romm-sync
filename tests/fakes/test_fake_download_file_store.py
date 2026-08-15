"""The fake's listings held to the real adapter's, on the tree that tells them apart.

A fake that projects one listing from the other cannot exhibit a disagreement
between them, which is exactly how a real one survived. These tests run the same
folder through both implementations and compare — including the kinds, which are
the whole admission rule: a file, a directory or a link, judged without
following, and nothing else listed at all.
"""

from __future__ import annotations

import os
import socket

import pytest

from adapters.download_file import DownloadFileAdapter
from fakes.fake_download_file_store import FakeDownloadFileStore


@pytest.fixture
def real() -> DownloadFileAdapter:
    return DownloadFileAdapter()


def _stage_real(tmp_path) -> str:
    """A folder with one of every kind, plus one thing that is no kind at all."""
    (tmp_path / "Game (U).sfc").write_bytes(b"cartridge")
    (tmp_path / "Game (E)").mkdir()
    (tmp_path / "Game (E)" / "rom.sfc").write_bytes(b"x")
    (tmp_path / "Game (J).sfc").symlink_to(tmp_path / "Game (U).sfc")
    (tmp_path / "Game (F).sfc").symlink_to(tmp_path / "gone.sfc")
    os.mkfifo(str(tmp_path / "Game (I).sfc"))
    return str(tmp_path)


def _stage_fake(directory: str) -> FakeDownloadFileStore:
    """The same folder, staged in the fake."""
    store = FakeDownloadFileStore()
    store.files[f"{directory}/Game (U).sfc"] = b"cartridge"
    store.dirs.add(f"{directory}/Game (E)")
    store.files[f"{directory}/Game (E)/rom.sfc"] = b"x"
    store.links.add(f"{directory}/Game (J).sfc")
    store.links.add(f"{directory}/Game (F).sfc")
    store.other_kinds.add(f"{directory}/Game (I).sfc")
    return store


def _kinds(entries) -> dict[str, str]:
    return {entry["name"]: entry["kind"] for entry in entries}


class TestFakeMatchesTheAdapter:
    def test_the_full_listing_agrees_name_for_name_and_kind_for_kind(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        assert _kinds(fake.list_top_level_entries(directory)) == _kinds(real.list_top_level_entries(directory))

    def test_the_lean_listing_agrees_too(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        assert _kinds(fake.list_top_level_names(directory)) == _kinds(real.list_top_level_names(directory))

    def test_both_listings_admit_the_same_set_in_either_implementation(self, real, tmp_path):
        # The property a projecting fake used to guarantee by construction and
        # now has to hold on its own merits — in both implementations.
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        for store in (real, fake):
            full = {entry["path"] for entry in store.list_top_level_entries(directory)}
            lean = {entry["path"] for entry in store.list_top_level_names(directory)}
            assert full == lean

    def test_the_kinds_are_what_the_rule_says_in_either(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        for store in (real, fake):
            assert _kinds(store.list_top_level_entries(directory)) == {
                "Game (U).sfc": "file",
                "Game (E)": "dir",
                "Game (J).sfc": "link",
                "Game (F).sfc": "link",
            }

    def test_a_socket_is_left_out_by_either(self, real, tmp_path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(tmp_path / "Game (S).sfc"))
            (tmp_path / "real.gba").write_bytes(b"x")
            directory = str(tmp_path)
            fake = FakeDownloadFileStore()
            fake.files[f"{directory}/real.gba"] = b"x"
            fake.other_kinds.add(f"{directory}/Game (S).sfc")

            for store in (real, fake):
                assert _kinds(store.list_top_level_entries(directory)) == {"real.gba": "file"}
        finally:
            sock.close()

    def test_describe_path_reports_a_link_as_a_link_in_either(self, real, tmp_path):
        (tmp_path / "real.gba").write_bytes(b"x")
        link = tmp_path / "Game.gba"
        link.symlink_to(tmp_path / "real.gba")
        fake = FakeDownloadFileStore()
        fake.files[str(tmp_path / "real.gba")] = b"x"
        fake.links.add(str(link))

        for store in (real, fake):
            described = store.describe_path(str(link))
            assert described is not None
            assert described["is_symlink"] is True
            assert described["is_dir"] is False
