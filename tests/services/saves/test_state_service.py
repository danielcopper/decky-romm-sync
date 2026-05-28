"""Tests for StateService (state.py) — save_sync_state.json persistence,
migrations, and the settings.json-backed save-sync feature toggles + device
label (#822)."""

import json
import os
from typing import cast

from fakes.fake_settings_persister import FakeSettingsPersister
from models.state import ShortcutRegistryEntry

from domain.save_state import (
    PlaytimeEntry,
    RomSaveState,
    SaveSyncState,
)
from services.saves import SaveService
from tests.services.saves._helpers import (
    _create_save,
    _install_rom,
    _server_save,
    make_service,
)


class TestStateManagement:
    def test_make_default_state(self):
        state = SaveService.make_default_state()
        assert state.device_id is None
        assert state.saves == {}

    def test_init_state_populates_defaults(self, tmp_path):
        svc, _ = make_service(tmp_path, save_sync_state=SaveSyncState())
        assert svc.get_save_sync_settings()["save_sync_enabled"] is False
        assert svc._save_sync_state.saves == {}

    def test_init_state_preserves_existing(self, tmp_path):
        state = SaveService.make_default_state()
        state.device_id = "existing-id"
        svc, _ = make_service(tmp_path, save_sync_state=state)
        assert svc._save_sync_state.device_id == "existing-id"

    def test_load_state_drops_legacy_dismissed_newer_save_id(self, tmp_path):
        """v0.15.0 user state with the obsolete dismissed_newer_save_id field
        gets the field stripped after load_state runs migrations on the
        loaded data. Mirrors the production order init_state → load_state."""
        legacy = {
            "version": 1,
            "device_id": None,
            "saves": {
                "42": {
                    "files": {
                        "game.srm": {
                            "tracked_save_id": 100,
                            "last_sync_hash": "abc",
                            "dismissed_newer_save_id": 200,  # legacy
                        },
                        "game.rtc": {
                            "tracked_save_id": 101,
                            "dismissed_newer_save_id": 201,  # legacy
                        },
                    }
                }
            },
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)  # calls init_state internally
        svc.load_state()

        files = svc._save_sync_state.saves["42"].files
        # The legacy field is dropped on load (verified at the domain level
        # via FileSyncState.from_dict — see tests/domain/test_save_state.py).
        assert files["game.srm"].tracked_save_id == 100
        assert files["game.srm"].last_sync_hash == "abc"
        assert files["game.rtc"].tracked_save_id == 101

    def test_load_state_drops_legacy_dismissed_newer_save_id_persists_to_disk(self, tmp_path):
        """End-to-end: legacy field on disk → init_state → load_state →
        save_state → reread → field is gone from the file. This is the
        invariant the smoke test (T16) verifies on hardware."""
        legacy = {
            "saves": {
                "42": {
                    "files": {
                        "game.srm": {
                            "tracked_save_id": 100,
                            "dismissed_newer_save_id": 999,
                        }
                    }
                }
            }
        }
        path = tmp_path / "save_sync_state.json"
        path.write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)
        svc.load_state()
        svc.save_state()

        on_disk = json.loads(path.read_text())
        assert "dismissed_newer_save_id" not in on_disk["saves"]["42"]["files"]["game.srm"]

    def test_load_state_renames_active_core_to_last_synced_core(self, tmp_path):
        """Legacy ``active_core`` is migrated to ``last_synced_core`` on load."""
        legacy = {
            "saves": {
                "42": {
                    "active_core": "mgba_libretro",
                    "files": {},
                }
            }
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)
        svc.load_state()

        entry = svc._save_sync_state.saves["42"]
        # Legacy ``active_core`` migration is verified at the domain level
        # (tests/domain/test_save_state.py). Confirm the service-level
        # round-trip wires it up to ``last_synced_core``.
        assert entry.last_synced_core == "mgba_libretro"
        assert "active_core" not in entry.to_dict()

    def test_load_state_skips_migration_for_malformed_entries(self, tmp_path):
        """Migration is defensive: non-dict values don't crash."""
        legacy = {
            "saves": {
                "42": {
                    "files": {
                        "good.srm": {"tracked_save_id": 100, "dismissed_newer_save_id": 5},
                        "weird.srm": "not-a-dict",
                    }
                }
            }
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)
        svc.load_state()  # should not raise

        files = svc._save_sync_state.saves["42"].files
        # Malformed sub-entries default to empty FileSyncState — see the
        # domain-level coverage in tests/domain/test_save_state.py.
        assert files["good.srm"].tracked_save_id == 100

    def test_settings_and_device_name_not_persisted_to_state_file(self, tmp_path):
        """The feature toggles + device label live in settings.json (#822),
        so the save_sync_state.json file no longer carries a ``settings``
        block or a ``device_name`` key — even a legacy file's settings are
        not round-tripped back to disk."""
        legacy = {
            "settings": {"save_sync_enabled": True},
            "device_name": "old-deck",
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)
        svc.load_state()
        svc.save_state()

        on_disk = json.loads((tmp_path / "save_sync_state.json").read_text())
        assert "settings" not in on_disk
        assert "device_name" not in on_disk

    def test_save_and_load_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.device_id = "test-device"
        svc._save_sync_state.saves["42"] = RomSaveState()
        svc.save_state()

        # Load into a fresh service
        svc2, _ = make_service(tmp_path)
        svc2.load_state()
        assert svc2._save_sync_state.device_id == "test-device"
        assert "42" in svc2._save_sync_state.saves

    def test_load_state_missing_file(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc.load_state()  # should not raise
        assert svc._save_sync_state.device_id is None

    def test_prune_orphaned_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.saves["99"] = RomSaveState()
        svc._save_sync_state.playtime["99"] = PlaytimeEntry.from_dict({"total_seconds": 100})
        svc._state["shortcut_registry"]["42"] = cast("ShortcutRegistryEntry", {})

        svc.prune_orphaned_state()
        assert "99" not in svc._save_sync_state.saves
        assert "99" not in svc._save_sync_state.playtime

    def test_prune_keeps_registered(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.saves["42"] = RomSaveState()
        svc._state["shortcut_registry"]["42"] = cast("ShortcutRegistryEntry", {})

        svc.prune_orphaned_state()
        assert "42" in svc._save_sync_state.saves


class TestPruneOrphanedEdgeCase:
    """Edge case for prune_orphaned_state not covered in TestStateManagement."""

    def test_empty_state_no_crash(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.saves = {}
        svc._save_sync_state.playtime = {}
        svc._state["shortcut_registry"] = {}

        svc.prune_orphaned_state()  # should not raise

        assert svc._save_sync_state.saves == {}
        assert svc._save_sync_state.playtime == {}


class TestStateBackwardCompat:
    """Backward compat: old state files without new fields load and work."""

    def test_old_state_without_server_device_id_loads_fine(self, tmp_path):
        """Existing state files without server_device_id should load without errors."""
        svc, _ = make_service(tmp_path)
        # Simulate old state without server_device_id
        svc._save_sync_state.device_id = "old-local-uuid"
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {"pokemon.srm": {"last_sync_hash": "abc123"}},
                "emulator": "retroarch",
                "system": "gba",
            }
        )
        # Remove the new field to simulate an old state file
        del svc._save_sync_state.server_device_id
        svc.save_state()

        # Reload into fresh service
        svc2, _ = make_service(tmp_path)
        svc2.load_state()

        # New field should be None (from init_state default)
        assert svc2._save_sync_state.server_device_id is None
        # Old data preserved
        assert svc2._save_sync_state.device_id == "old-local-uuid"
        assert "42" in svc2._save_sync_state.saves

    def test_old_per_game_entry_missing_new_fields_works_via_get(self, tmp_path):
        """Per-game entries without last_synced_core/active_slot still work via .get()."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.device_id = "old-local-uuid"
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {"pokemon.srm": {"last_sync_hash": "abc123"}},
                "emulator": "retroarch",
                "system": "gba",
            }
        )
        svc.save_state()

        svc2, _ = make_service(tmp_path)
        svc2.load_state()

        game_state = svc2._save_sync_state.saves["42"]
        assert game_state.last_synced_core is None
        # Missing ``active_slot`` defaults to None on the typed aggregate;
        # callers fall back to the global default_slot when they need a value.
        assert game_state.active_slot is None

    def test_make_default_state_includes_server_device_id(self):
        """make_default_state() must include server_device_id field."""
        state = SaveService.make_default_state()
        assert state.server_device_id is None

    def test_load_state_restores_server_device_id(self, tmp_path):
        """server_device_id saved to disk is restored on load_state."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.server_device_id = "romm-server-uuid"
        svc.save_state()

        svc2, _ = make_service(tmp_path)
        svc2.load_state()
        assert svc2._save_sync_state.server_device_id == "romm-server-uuid"

    def test_state_stores_emulator_tag_and_core(self, tmp_path):
        """After upload sync, state should contain emulator tag and core info."""
        svc, _fake = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
        )
        svc._config.settings["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._sync_engine.do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        game_state = svc._save_sync_state.saves["42"]
        assert game_state.emulator == "retroarch-mgba"
        assert game_state.last_synced_core == "mgba_libretro"
        assert game_state.active_slot == "default"

        # Per-file should have tracked_save_id
        file_state = game_state.files["pokemon.srm"]
        assert file_state.tracked_save_id is not None

    def test_download_sets_tracked_save_id_in_file_state(self, tmp_path):
        """After download sync, per-file state should contain tracked_save_id."""
        svc, _ = make_service(tmp_path)
        saves_dir = str(tmp_path / "saves" / "gba")
        os.makedirs(saves_dir, exist_ok=True)
        server_save = _server_save(save_id=99)

        svc._sync_engine.do_download_save(server_save, saves_dir, "pokemon.srm", "42", "gba")

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 99
        assert file_state.last_sync_server_save_id == 99


class TestSaveSyncSettingsOwnership:
    """The five feature toggles + device label live in settings.json (#822).

    StateService reads them straight out of the injected settings dict and
    flushes mutations through the SettingsPersister — never the save-sync
    aggregate.
    """

    def _make(self, tmp_path, settings):
        persister = FakeSettingsPersister()
        svc, _ = make_service(tmp_path, settings=settings, settings_persister=persister)
        return svc, persister

    def test_is_save_sync_enabled_reads_settings_dict(self, tmp_path):
        svc, _ = self._make(tmp_path, {"save_sync_enabled": True})
        assert svc._state_svc.is_save_sync_enabled() is True

    def test_is_save_sync_enabled_defaults_false_when_missing(self, tmp_path):
        svc, _ = self._make(tmp_path, {})
        assert svc._state_svc.is_save_sync_enabled() is False

    def test_get_settings_builds_view_from_settings_dict(self, tmp_path):
        svc, _ = self._make(
            tmp_path,
            {
                "save_sync_enabled": True,
                "sync_before_launch": False,
                "sync_after_exit": False,
                "default_slot": "alt",
                "autocleanup_limit": 5,
            },
        )
        settings = svc._state_svc.get_settings()
        assert settings.save_sync_enabled is True
        assert settings.sync_before_launch is False
        assert settings.sync_after_exit is False
        assert settings.default_slot == "alt"
        assert settings.autocleanup_limit == 5

    def test_get_settings_applies_default_coercions(self, tmp_path):
        """Missing keys fall back to the legacy defaults."""
        svc, _ = self._make(tmp_path, {})
        settings = svc._state_svc.get_settings()
        assert settings.save_sync_enabled is False
        assert settings.sync_before_launch is True
        assert settings.sync_after_exit is True
        assert settings.default_slot == "default"
        assert settings.autocleanup_limit == 10

    def test_get_settings_preserves_none_default_slot(self, tmp_path):
        """``default_slot=None`` is the no-slots mode and must survive."""
        svc, _ = self._make(tmp_path, {"default_slot": None})
        assert svc._state_svc.get_settings().default_slot is None

    def test_get_settings_empty_default_slot_collapses_to_none(self, tmp_path):
        svc, _ = self._make(tmp_path, {"default_slot": ""})
        assert svc._state_svc.get_settings().default_slot is None

    def test_get_settings_autocleanup_zero_guarded_to_ten(self, tmp_path):
        svc, _ = self._make(tmp_path, {"autocleanup_limit": 0})
        assert svc._state_svc.get_settings().autocleanup_limit == 10

    def test_get_save_sync_settings_returns_five_knob_dict(self, tmp_path):
        svc, _ = self._make(tmp_path, {"save_sync_enabled": True, "default_slot": "alt"})
        result = svc._state_svc.get_save_sync_settings()
        assert result == {
            "save_sync_enabled": True,
            "sync_before_launch": True,
            "sync_after_exit": True,
            "default_slot": "alt",
            "autocleanup_limit": 10,
        }

    def test_update_writes_to_settings_dict_and_flushes(self, tmp_path):
        settings = {"save_sync_enabled": False}
        svc, persister = self._make(tmp_path, settings)

        result = svc._state_svc.update_save_sync_settings(
            {"save_sync_enabled": True, "default_slot": "alt", "autocleanup_limit": 3}
        )

        assert result["success"] is True
        assert result["settings"]["save_sync_enabled"] is True
        assert result["settings"]["default_slot"] == "alt"
        assert result["settings"]["autocleanup_limit"] == 3
        # Written into the live settings dict, not the aggregate.
        assert settings["save_sync_enabled"] is True
        assert settings["default_slot"] == "alt"
        assert settings["autocleanup_limit"] == 3
        # Flushed through the settings persister, not save_sync_state.json.
        assert persister.save_count == 1

    def test_update_ignores_unknown_keys(self, tmp_path):
        settings = {}
        svc, persister = self._make(tmp_path, settings)

        svc._state_svc.update_save_sync_settings({"not_a_knob": "x", "save_sync_enabled": True})

        assert "not_a_knob" not in settings
        assert settings["save_sync_enabled"] is True
        assert persister.save_count == 1

    def test_update_autocleanup_limit_floored_to_one(self, tmp_path):
        settings = {}
        svc, _ = self._make(tmp_path, settings)
        svc._state_svc.update_save_sync_settings({"autocleanup_limit": 0})
        assert settings["autocleanup_limit"] == 1

    def test_get_device_name_reads_settings_dict(self, tmp_path):
        svc, _ = self._make(tmp_path, {"device_name": "steamdeck"})
        assert svc._state_svc.get_device_name() == "steamdeck"

    def test_get_device_name_none_when_unset(self, tmp_path):
        svc, _ = self._make(tmp_path, {})
        assert svc._state_svc.get_device_name() is None

    def test_set_device_name_writes_to_settings_and_flushes(self, tmp_path):
        settings = {}
        svc, persister = self._make(tmp_path, settings)

        svc._state_svc.set_device_name("steamdeck")

        assert settings["device_name"] == "steamdeck"
        assert persister.save_count == 1
