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
    """The same folder, staged in the fake — link targets and all."""
    store = FakeDownloadFileStore()
    store.files[f"{directory}/Game (U).sfc"] = b"cartridge"
    store.dirs.add(f"{directory}/Game (E)")
    store.files[f"{directory}/Game (E)/rom.sfc"] = b"x"
    store.links[f"{directory}/Game (J).sfc"] = f"{directory}/Game (U).sfc"
    store.links[f"{directory}/Game (F).sfc"] = f"{directory}/gone.sfc"
    store.other_kinds.add(f"{directory}/Game (I).sfc")
    return store


def _kinds(entries) -> dict[str, str]:
    return {entry["name"]: entry["kind"] for entry in entries}


def _sizes(entries) -> dict[str, int]:
    return {entry["name"]: entry["size_bytes"] for entry in entries}


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

    def test_the_full_listing_agrees_on_the_numbers_too(self, real, tmp_path):
        # Kinds agreeing is not enough: the size is what the occupied-target
        # dialog puts on screen, and the fake used to answer 0 for a link where
        # ``lstat`` reports the length of the path it stores.
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        assert _sizes(fake.list_top_level_entries(directory)) == _sizes(real.list_top_level_entries(directory))

    def test_describe_path_agrees_entry_for_entry_on_kind_and_size(self, real, tmp_path):
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)
        names = ("Game (U).sfc", "Game (E)", "Game (J).sfc", "Game (F).sfc", "Game (I).sfc", "nothing.sfc")

        def described(store) -> dict[str, tuple[str | None, int] | None]:
            out = {}
            for name in names:
                answer = store.describe_path(f"{directory}/{name}")
                out[name] = None if answer is None else (answer["kind"], answer["size_bytes"])
            return out

        assert described(fake) == described(real)

    def test_the_kindless_entry_is_described_by_either_rather_than_reported_absent(self, real, tmp_path):
        # The one place the two doors differ on purpose: a listing leaves it out,
        # this reports it with no kind. Both implementations have to do that, or
        # a service test would prove a download safe that destroys one.
        directory = _stage_real(tmp_path)
        fake = _stage_fake(directory)

        for store in (real, fake):
            answer = store.describe_path(f"{directory}/Game (I).sfc")
            assert answer is not None
            assert answer["kind"] is None
