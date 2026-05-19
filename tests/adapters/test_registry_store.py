"""Tests for ``RegistryStoreAdapter`` — typed-patch writes over the live state dict."""

import logging
from typing import cast

import pytest
from models.registry_patches import (
    RegistryCoverPathPatch,
    RegistryDeletePatch,
    RegistryIdsPatch,
    RegistrySgdbIdPatch,
    RegistrySyncApplyPatch,
)
from models.state import PluginState, ShortcutRegistryEntry, make_default_plugin_state

from adapters.registry_store import RegistryStoreAdapter


@pytest.fixture
def logger():
    return logging.getLogger("test_registry_store")


@pytest.fixture
def state() -> PluginState:
    return make_default_plugin_state()


@pytest.fixture
def store(state: PluginState, logger: logging.Logger) -> RegistryStoreAdapter:
    return RegistryStoreAdapter(state=state, logger=logger)


def _make_existing_entry(**overrides: object) -> ShortcutRegistryEntry:
    entry: ShortcutRegistryEntry = {
        "app_id": 100,
        "name": "Old Name",
        "fs_name": "old",
        "platform_name": "Platform",
        "platform_slug": "plat",
        "cover_path": "/covers/old.png",
    }
    cast("dict", entry).update(overrides)
    return entry


def _base_sync_patch(**overrides: object) -> RegistrySyncApplyPatch:
    params: dict = {
        "rom_id_str": "42",
        "app_id": 123,
        "name": "Game",
        "fs_name": "game",
        "platform_name": "Game Boy",
        "platform_slug": "gb",
        "cover_path": "/covers/42.png",
    }
    params.update(overrides)
    return RegistrySyncApplyPatch(**params)


class TestApplySync:
    def test_creates_entry_with_required_fields_only(self, store, state):
        store.apply_sync(_base_sync_patch())

        entry = state["shortcut_registry"]["42"]
        assert entry == {
            "app_id": 123,
            "name": "Game",
            "fs_name": "game",
            "platform_name": "Game Boy",
            "platform_slug": "gb",
            "cover_path": "/covers/42.png",
        }
        assert "igdb_id" not in entry
        assert "sgdb_id" not in entry
        assert "ra_id" not in entry

    def test_writes_optional_ids_from_patch(self, store, state):
        store.apply_sync(_base_sync_patch(igdb_id=999, sgdb_id=888, ra_id=777))

        entry = state["shortcut_registry"]["42"]
        assert entry["igdb_id"] == 999
        assert entry["sgdb_id"] == 888
        assert entry["ra_id"] == 777

    def test_preserves_existing_sgdb_id_when_patch_none(self, store, state):
        """Field-clobber regression: a patch with sgdb_id=None must not wipe
        a previously-set sgdb_id on the existing row."""
        state["shortcut_registry"]["42"] = _make_existing_entry(sgdb_id=555, igdb_id=666, ra_id=777)

        store.apply_sync(_base_sync_patch())  # all optional IDs default to None

        entry = state["shortcut_registry"]["42"]
        assert entry["sgdb_id"] == 555
        assert entry["igdb_id"] == 666
        assert entry["ra_id"] == 777

    def test_patch_value_replaces_existing_when_provided(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry(sgdb_id=555)

        store.apply_sync(_base_sync_patch(sgdb_id=999))

        assert state["shortcut_registry"]["42"]["sgdb_id"] == 999

    def test_mixed_provided_and_preserved(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry(sgdb_id=555, igdb_id=666)

        store.apply_sync(_base_sync_patch(sgdb_id=999))

        entry = state["shortcut_registry"]["42"]
        assert entry["sgdb_id"] == 999  # patch value wins
        assert entry["igdb_id"] == 666  # existing preserved
        assert "ra_id" not in entry  # neither side carried it

    def test_replaces_required_fields_on_existing_entry(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry()

        store.apply_sync(_base_sync_patch())

        entry = state["shortcut_registry"]["42"]
        assert entry["app_id"] == 123
        assert entry["name"] == "Game"
        assert entry["cover_path"] == "/covers/42.png"


class TestApplyCoverPath:
    def test_no_op_on_missing_entry(self, store, state, caplog):
        with caplog.at_level(logging.WARNING):
            store.apply_cover_path(RegistryCoverPathPatch(rom_id_str="42", cover_path="/new.png"))

        assert "42" not in state["shortcut_registry"]
        assert any("apply_cover_path" in rec.message for rec in caplog.records)

    def test_mutates_only_cover_path(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry(sgdb_id=555)

        store.apply_cover_path(RegistryCoverPathPatch(rom_id_str="42", cover_path="/new.png"))

        entry = state["shortcut_registry"]["42"]
        assert entry["cover_path"] == "/new.png"
        # Other fields untouched
        assert entry["app_id"] == 100
        assert entry["name"] == "Old Name"
        assert entry["fs_name"] == "old"
        assert entry["platform_name"] == "Platform"
        assert entry["platform_slug"] == "plat"
        assert entry["sgdb_id"] == 555


class TestApplySgdbId:
    def test_no_op_on_missing_entry(self, store, state, caplog):
        with caplog.at_level(logging.WARNING):
            store.apply_sgdb_id(RegistrySgdbIdPatch(rom_id_str="42", sgdb_id=888))

        assert "42" not in state["shortcut_registry"]
        assert any("apply_sgdb_id" in rec.message for rec in caplog.records)

    def test_mutates_only_sgdb_id(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry(igdb_id=666)

        store.apply_sgdb_id(RegistrySgdbIdPatch(rom_id_str="42", sgdb_id=888))

        entry = state["shortcut_registry"]["42"]
        assert entry["sgdb_id"] == 888
        assert entry["igdb_id"] == 666
        assert entry["cover_path"] == "/covers/old.png"


class TestApplyIds:
    def test_both_none_is_noop_existing_entry_unchanged(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry(sgdb_id=555, igdb_id=666)
        before = dict(state["shortcut_registry"]["42"])

        store.apply_ids(RegistryIdsPatch(rom_id_str="42", sgdb_id=None, igdb_id=None))

        assert state["shortcut_registry"]["42"] == before

    def test_both_none_on_missing_entry_is_noop(self, store, state, caplog):
        with caplog.at_level(logging.WARNING):
            store.apply_ids(RegistryIdsPatch(rom_id_str="42", sgdb_id=None, igdb_id=None))

        assert "42" not in state["shortcut_registry"]
        # No warning either — there's nothing to apply, so the missing-row
        # branch isn't reached.
        assert not any("apply_ids" in rec.message for rec in caplog.records)

    def test_only_sgdb_id_leaves_igdb_id_untouched(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry(igdb_id=666)

        store.apply_ids(RegistryIdsPatch(rom_id_str="42", sgdb_id=888, igdb_id=None))

        entry = state["shortcut_registry"]["42"]
        assert entry["sgdb_id"] == 888
        assert entry["igdb_id"] == 666

    def test_only_igdb_id_leaves_sgdb_id_untouched(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry(sgdb_id=555)

        store.apply_ids(RegistryIdsPatch(rom_id_str="42", sgdb_id=None, igdb_id=999))

        entry = state["shortcut_registry"]["42"]
        assert entry["igdb_id"] == 999
        assert entry["sgdb_id"] == 555

    def test_both_set_writes_both(self, store, state):
        state["shortcut_registry"]["42"] = _make_existing_entry()

        store.apply_ids(RegistryIdsPatch(rom_id_str="42", sgdb_id=888, igdb_id=999))

        entry = state["shortcut_registry"]["42"]
        assert entry["sgdb_id"] == 888
        assert entry["igdb_id"] == 999

    def test_missing_entry_with_value_warns(self, store, state, caplog):
        with caplog.at_level(logging.WARNING):
            store.apply_ids(RegistryIdsPatch(rom_id_str="42", sgdb_id=888, igdb_id=None))

        assert "42" not in state["shortcut_registry"]
        assert any("apply_ids" in rec.message for rec in caplog.records)


class TestDelete:
    def test_returns_evicted_entry_and_removes_key(self, store, state):
        entry = _make_existing_entry(sgdb_id=555)
        state["shortcut_registry"]["42"] = entry

        result = store.delete(RegistryDeletePatch(rom_id_str="42"))

        assert result == entry
        assert "42" not in state["shortcut_registry"]

    def test_returns_none_on_missing_entry(self, store, state):
        result = store.delete(RegistryDeletePatch(rom_id_str="42"))
        assert result is None
        assert "42" not in state["shortcut_registry"]
