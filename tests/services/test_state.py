"""Tests for services.saves.state.StateService."""

import logging
from typing import cast

from fakes.fake_save_sync_state_persister import FakeSaveSyncStatePersister
from fakes.fake_settings_persister import FakeSettingsPersister
from models.state import ShortcutRegistryEntry, make_default_plugin_state

from domain.save_state import FileSyncState, PlaytimeEntry, RomSaveState
from services.saves.state import StateService, StateServiceConfig


def _make_state_svc(
    persister: FakeSaveSyncStatePersister | None = None,
    settings: dict | None = None,
) -> tuple[StateService, FakeSaveSyncStatePersister]:
    save_sync_state = StateService.make_default_state()
    state = make_default_plugin_state()
    p = persister or FakeSaveSyncStatePersister()
    return (
        StateService(
            config=StateServiceConfig(
                save_sync_state=save_sync_state,
                state=state,
                settings=settings if settings is not None else {},
                persister=p,
                settings_persister=FakeSettingsPersister(),
                logger=logging.getLogger("test"),
            ),
        ),
        p,
    )


class TestClearFilesState:
    def test_clears_files_preserves_slot_config(self):
        svc, _ = _make_state_svc()
        svc.state.saves["42"] = RomSaveState(
            files={"pokemon.srm": FileSyncState(last_sync_hash="abc")},
            active_slot="desktop",
            slot_confirmed=True,
            emulator="retroarch-mgba",
            last_synced_core="mgba_libretro",
            own_upload_ids=[1],
            slots={"default": {}, "desktop": {}},
            system="gba",
        )

        svc.clear_files_state("42")

        entry = svc.state.saves["42"]
        assert entry.files == {}
        assert entry.active_slot == "desktop"
        assert entry.slot_confirmed is True
        assert entry.emulator == "retroarch-mgba"
        assert entry.last_synced_core == "mgba_libretro"
        assert entry.own_upload_ids == [1]
        assert entry.slots == {"default": {}, "desktop": {}}
        assert entry.system == "gba"

    def test_creates_empty_entry_when_missing(self):
        svc, _ = _make_state_svc()
        assert "999" not in svc.state.saves

        svc.clear_files_state("999")

        assert svc.state.saves["999"].files == {}

    def test_does_not_persist_on_its_own(self):
        """clear_files_state must not write to disk — caller orchestrates persistence."""
        svc, persister = _make_state_svc()
        svc.state.saves["42"] = RomSaveState(files={"pokemon.srm": FileSyncState()})

        svc.clear_files_state("42")

        assert persister.save_count == 0

    def test_idempotent(self):
        svc, _ = _make_state_svc()
        svc.state.saves["42"] = RomSaveState(
            files={"pokemon.srm": FileSyncState()},
            active_slot="desktop",
            slot_confirmed=True,
        )

        svc.clear_files_state("42")
        svc.clear_files_state("42")

        entry = svc.state.saves["42"]
        assert entry.files == {}
        assert entry.active_slot == "desktop"
        assert entry.slot_confirmed is True


class TestSaveState:
    def test_save_state_forwards_aggregate_to_persister(self):
        svc, persister = _make_state_svc()
        svc.state.device_id = "abc123"
        svc.state.saves["42"] = RomSaveState(
            files={"game.srm": FileSyncState(tracked_save_id=7)},
        )

        svc.save_state()

        assert persister.save_count == 1
        assert persister.last_saved is not None
        assert persister.last_saved["device_id"] == "abc123"
        assert persister.last_saved["saves"]["42"]["files"]["game.srm"]["tracked_save_id"] == 7


class TestLoadState:
    def test_load_state_returns_early_when_persister_returns_none(self):
        """First-run / missing-file: persister returns None → defaults stay intact."""
        svc, persister = _make_state_svc(FakeSaveSyncStatePersister(canned_load=None))

        # Mutate one default so we can detect it was preserved (load did NOT clobber it).
        svc.state.device_id = "preserved"

        svc.load_state()

        assert persister.load_count == 1
        assert svc.state.device_id == "preserved"
        # The feature toggle reads from settings.json (empty → default False).
        assert svc.is_save_sync_enabled() is False

    def test_load_state_merges_top_level_fields(self):
        canned = {
            "version": 1,
            "device_id": "dev-1",
            "device_name": "deck",  # ignored — lives in settings.json now (#822)
            "server_device_id": "srv-1",
            "saves": {"42": {"files": {}}},
            "playtime": {"42": {"total_seconds": 3600}},
            "settings": {"save_sync_enabled": True, "default_slot": "alt"},  # ignored
        }
        svc, _ = _make_state_svc(FakeSaveSyncStatePersister(canned_load=canned))

        svc.load_state()

        assert svc.state.device_id == "dev-1"
        assert svc.state.server_device_id == "srv-1"
        assert "42" in svc.state.saves
        assert svc.state.playtime["42"].total_seconds == 3600
        # The legacy settings + device_name on the loaded payload are NOT
        # parsed onto the aggregate — they moved to settings.json (#822).
        on_disk = svc.state.to_dict()
        assert "settings" not in on_disk
        assert "device_name" not in on_disk

    def test_load_state_runs_migration_on_loaded_payload(self):
        """active_core → last_synced_core, drops dismissed_newer_save_id; the
        settings block on a legacy payload is ignored (it moved to
        settings.json, #822)."""
        canned = {
            "saves": {
                "42": {
                    "active_core": "mgba_libretro",
                    "files": {
                        "game.srm": {"tracked_save_id": 1, "dismissed_newer_save_id": 99},
                    },
                }
            },
            "settings": {"save_sync_enabled": True},  # ignored on load
        }
        svc, _ = _make_state_svc(FakeSaveSyncStatePersister(canned_load=canned))

        svc.load_state()

        entry = svc.state.saves["42"]
        assert entry.last_synced_core == "mgba_libretro"
        # ``active_core`` and ``dismissed_newer_save_id`` are stripped via
        # SaveSyncState.from_dict — verified at the domain layer in
        # tests/domain/test_save_state.py.
        on_disk = svc.state.to_dict()
        assert "active_core" not in on_disk["saves"]["42"]
        assert "dismissed_newer_save_id" not in on_disk["saves"]["42"]["files"]["game.srm"]
        assert "settings" not in on_disk


class TestPruneOrphanedState:
    def test_prune_persists_when_entries_removed(self):
        svc, persister = _make_state_svc()
        svc._state["shortcut_registry"] = {"42": cast("ShortcutRegistryEntry", {"app_id": 1})}
        svc.state.saves["42"] = RomSaveState()
        svc.state.saves["999"] = RomSaveState()  # orphaned
        svc.state.playtime["888"] = PlaytimeEntry()  # orphaned

        svc.prune_orphaned_state()

        assert "999" not in svc.state.saves
        assert "888" not in svc.state.playtime
        assert "42" in svc.state.saves
        assert persister.save_count == 1

    def test_prune_does_not_persist_when_nothing_removed(self):
        svc, persister = _make_state_svc()
        svc._state["shortcut_registry"] = {"42": cast("ShortcutRegistryEntry", {"app_id": 1})}
        svc.state.saves["42"] = RomSaveState()

        svc.prune_orphaned_state()

        assert persister.save_count == 0
