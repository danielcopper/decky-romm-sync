"""Tests for the frozen registry-patch dataclasses."""

import dataclasses

import pytest
from models.registry_patches import (
    RegistryCoverPathPatch,
    RegistryDeletePatch,
    RegistryIdsPatch,
    RegistrySgdbIdPatch,
    RegistrySyncApplyPatch,
)


class TestRegistrySyncApplyPatch:
    def test_required_fields_assigned(self):
        patch = RegistrySyncApplyPatch(
            rom_id_str="42",
            app_id=123,
            name="Game",
            fs_name="game",
            platform_name="Game Boy",
            platform_slug="gb",
            cover_path="/covers/42.png",
        )
        assert patch.rom_id_str == "42"
        assert patch.app_id == 123
        assert patch.name == "Game"
        assert patch.fs_name == "game"
        assert patch.platform_name == "Game Boy"
        assert patch.platform_slug == "gb"
        assert patch.cover_path == "/covers/42.png"

    def test_optional_ids_default_to_none(self):
        patch = RegistrySyncApplyPatch(
            rom_id_str="42",
            app_id=123,
            name="Game",
            fs_name="game",
            platform_name="Game Boy",
            platform_slug="gb",
            cover_path="/covers/42.png",
        )
        assert patch.igdb_id is None
        assert patch.sgdb_id is None
        assert patch.ra_id is None

    def test_optional_ids_can_be_set(self):
        patch = RegistrySyncApplyPatch(
            rom_id_str="42",
            app_id=123,
            name="Game",
            fs_name="game",
            platform_name="Game Boy",
            platform_slug="gb",
            cover_path="/covers/42.png",
            igdb_id=999,
            sgdb_id=888,
            ra_id=777,
        )
        assert patch.igdb_id == 999
        assert patch.sgdb_id == 888
        assert patch.ra_id == 777

    def test_frozen(self):
        patch = RegistrySyncApplyPatch(
            rom_id_str="42",
            app_id=123,
            name="Game",
            fs_name="game",
            platform_name="Game Boy",
            platform_slug="gb",
            cover_path="/covers/42.png",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            patch.app_id = 456  # type: ignore[misc]


class TestRegistryCoverPathPatch:
    def test_fields(self):
        patch = RegistryCoverPathPatch(rom_id_str="42", cover_path="/covers/42.png")
        assert patch.rom_id_str == "42"
        assert patch.cover_path == "/covers/42.png"

    def test_frozen(self):
        patch = RegistryCoverPathPatch(rom_id_str="42", cover_path="/covers/42.png")
        with pytest.raises(dataclasses.FrozenInstanceError):
            patch.cover_path = "/other.png"  # type: ignore[misc]


class TestRegistrySgdbIdPatch:
    def test_fields(self):
        patch = RegistrySgdbIdPatch(rom_id_str="42", sgdb_id=888)
        assert patch.rom_id_str == "42"
        assert patch.sgdb_id == 888

    def test_frozen(self):
        patch = RegistrySgdbIdPatch(rom_id_str="42", sgdb_id=888)
        with pytest.raises(dataclasses.FrozenInstanceError):
            patch.sgdb_id = 0  # type: ignore[misc]


class TestRegistryIdsPatch:
    def test_both_set(self):
        patch = RegistryIdsPatch(rom_id_str="42", sgdb_id=888, igdb_id=999)
        assert patch.sgdb_id == 888
        assert patch.igdb_id == 999

    def test_both_none(self):
        patch = RegistryIdsPatch(rom_id_str="42", sgdb_id=None, igdb_id=None)
        assert patch.sgdb_id is None
        assert patch.igdb_id is None

    def test_frozen(self):
        patch = RegistryIdsPatch(rom_id_str="42", sgdb_id=888, igdb_id=999)
        with pytest.raises(dataclasses.FrozenInstanceError):
            patch.sgdb_id = 0  # type: ignore[misc]


class TestRegistryDeletePatch:
    def test_fields(self):
        patch = RegistryDeletePatch(rom_id_str="42")
        assert patch.rom_id_str == "42"

    def test_frozen(self):
        patch = RegistryDeletePatch(rom_id_str="42")
        with pytest.raises(dataclasses.FrozenInstanceError):
            patch.rom_id_str = "0"  # type: ignore[misc]
