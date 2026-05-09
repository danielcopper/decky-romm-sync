"""Tests for services.saves.state.StateService."""

import logging

from services.saves.state import StateService


def _make_state_svc(tmp_path) -> StateService:
    save_sync_state = StateService.make_default_state()
    state: dict = {"shortcut_registry": {}, "installed_roms": {}}
    return StateService(
        save_sync_state=save_sync_state,
        state=state,
        runtime_dir=str(tmp_path),
        logger=logging.getLogger("test"),
    )


class TestClearFilesState:
    def test_clears_files_preserves_slot_config(self, tmp_path):
        svc = _make_state_svc(tmp_path)
        svc.data["saves"]["42"] = {
            "files": {"pokemon.srm": {"last_sync_hash": "abc"}},
            "active_slot": "desktop",
            "slot_confirmed": True,
            "emulator": "retroarch-mgba",
            "last_synced_core": "mgba_libretro",
            "own_upload_ids": ["save-1"],
            "slots": {"default": {}, "desktop": {}},
            "system": "gba",
        }

        svc.clear_files_state("42")

        entry = svc.data["saves"]["42"]
        assert entry["files"] == {}
        assert entry["active_slot"] == "desktop"
        assert entry["slot_confirmed"] is True
        assert entry["emulator"] == "retroarch-mgba"
        assert entry["last_synced_core"] == "mgba_libretro"
        assert entry["own_upload_ids"] == ["save-1"]
        assert entry["slots"] == {"default": {}, "desktop": {}}
        assert entry["system"] == "gba"

    def test_creates_empty_entry_when_missing(self, tmp_path):
        svc = _make_state_svc(tmp_path)
        assert "999" not in svc.data["saves"]

        svc.clear_files_state("999")

        assert svc.data["saves"]["999"] == {"files": {}}

    def test_does_not_persist_on_its_own(self, tmp_path):
        """clear_files_state must not write to disk — caller orchestrates persistence."""
        svc = _make_state_svc(tmp_path)
        svc.data["saves"]["42"] = {"files": {"pokemon.srm": {}}}

        svc.clear_files_state("42")

        # No file should have been written.
        assert not (tmp_path / "save_sync_state.json").exists()

    def test_idempotent(self, tmp_path):
        svc = _make_state_svc(tmp_path)
        svc.data["saves"]["42"] = {
            "files": {"pokemon.srm": {}},
            "active_slot": "desktop",
            "slot_confirmed": True,
        }

        svc.clear_files_state("42")
        svc.clear_files_state("42")

        entry = svc.data["saves"]["42"]
        assert entry["files"] == {}
        assert entry["active_slot"] == "desktop"
        assert entry["slot_confirmed"] is True

    def test_creates_saves_dict_if_missing(self, tmp_path):
        """Defensive: if the top-level 'saves' key were missing, recreate it."""
        svc = _make_state_svc(tmp_path)
        del svc.data["saves"]

        svc.clear_files_state("42")

        assert svc.data["saves"] == {"42": {"files": {}}}
