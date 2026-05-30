"""Unit tests for the ``RomSaveState`` aggregate and its ``FileSyncState`` VO."""

from __future__ import annotations

import pytest

from domain.rom_save_state import FileSyncState, RomSaveState


class TestDefaults:
    def test_default_construction_sets_expected_defaults(self):
        state = RomSaveState()
        assert state.active_slot is None
        assert state.slot_confirmed is False
        assert state.emulator == "retroarch"
        assert state.system == ""
        assert state.last_synced_core is None
        assert state.own_upload_ids is None
        assert state.slots == {}
        assert state.files == {}
        assert state.last_sync_check_at is None

    def test_slots_and_files_are_independent_per_instance(self):
        a = RomSaveState()
        b = RomSaveState()
        a.slots["default"] = {"source": "local"}
        a.files["x.srm"] = FileSyncState()
        assert b.slots == {}
        assert b.files == {}


class TestFileSyncState:
    def test_defaults(self):
        fs = FileSyncState()
        assert fs.tracked_save_id is None
        assert fs.last_sync_hash is None
        assert fs.last_sync_at == ""
        assert fs.last_sync_server_updated_at == ""
        assert fs.last_sync_server_save_id is None
        assert fs.last_sync_server_size is None
        assert fs.last_sync_local_mtime is None
        assert fs.last_sync_local_size is None

    def test_is_frozen(self):
        fs = FileSyncState()
        with pytest.raises((AttributeError, Exception)):
            fs.last_sync_hash = "abc"  # type: ignore[misc]


class TestAdoptBaseline:
    def test_minimal_required_anchors_create_entry(self):
        state = RomSaveState()
        state.adopt_baseline("game.srm", tracked_save_id=11, last_sync_hash="deadbeef")
        fs = state.files["game.srm"]
        assert fs.tracked_save_id == 11
        assert fs.last_sync_hash == "deadbeef"
        # Untouched optionals fall back to FileSyncState defaults.
        assert fs.last_sync_at == ""
        assert fs.last_sync_server_updated_at == ""
        assert fs.last_sync_server_save_id is None
        assert fs.last_sync_server_size is None
        assert fs.last_sync_local_mtime is None
        assert fs.last_sync_local_size is None

    def test_all_fields_are_recorded(self):
        state = RomSaveState()
        state.adopt_baseline(
            "game.srm",
            tracked_save_id=11,
            last_sync_hash="deadbeef",
            last_sync_at="2026-05-28T10:00:00",
            last_sync_server_updated_at="2026-05-28T09:00:00",
            last_sync_server_save_id=99,
            last_sync_server_size=2048,
            last_sync_local_mtime=1716890400.0,
            last_sync_local_size=2048,
        )
        fs = state.files["game.srm"]
        assert fs.tracked_save_id == 11
        assert fs.last_sync_hash == "deadbeef"
        assert fs.last_sync_at == "2026-05-28T10:00:00"
        assert fs.last_sync_server_updated_at == "2026-05-28T09:00:00"
        assert fs.last_sync_server_save_id == 99
        assert fs.last_sync_server_size == 2048
        assert fs.last_sync_local_mtime == 1716890400.0
        assert fs.last_sync_local_size == 2048

    def test_re_adopt_replaces_existing_baseline(self):
        state = RomSaveState()
        state.adopt_baseline("game.srm", tracked_save_id=11, last_sync_hash="old")
        state.adopt_baseline(
            "game.srm",
            tracked_save_id=11,
            last_sync_hash="new",
            last_sync_server_size=4096,
        )
        assert len(state.files) == 1
        fs = state.files["game.srm"]
        assert fs.last_sync_hash == "new"
        assert fs.last_sync_server_size == 4096

    @pytest.mark.parametrize("bad_id", [0, -1])
    def test_non_positive_tracked_save_id_raises(self, bad_id: int):
        state = RomSaveState()
        with pytest.raises(ValueError, match="tracked_save_id must be positive"):
            state.adopt_baseline("game.srm", tracked_save_id=bad_id, last_sync_hash="x")
        assert state.files == {}

    def test_empty_hash_raises(self):
        state = RomSaveState()
        with pytest.raises(ValueError, match="last_sync_hash is required"):
            state.adopt_baseline("game.srm", tracked_save_id=11, last_sync_hash="")
        assert state.files == {}


class TestUpdateBaselineHash:
    def test_creates_minimal_entry_when_untracked(self):
        state = RomSaveState()
        state.update_baseline_hash("game.srm", "abc123")
        fs = state.files["game.srm"]
        assert fs.last_sync_hash == "abc123"
        # No server anchor — the relaxed entry point leaves the rest at defaults.
        assert fs.tracked_save_id is None
        assert fs.last_sync_server_save_id is None
        assert fs.last_sync_at == ""

    def test_updates_hash_in_place_preserving_other_anchors(self):
        state = RomSaveState()
        state.adopt_baseline(
            "game.srm",
            tracked_save_id=11,
            last_sync_hash="old",
            last_sync_at="2026-05-28T10:00:00",
            last_sync_server_save_id=99,
            last_sync_server_size=2048,
        )
        state.update_baseline_hash("game.srm", "new")
        fs = state.files["game.srm"]
        assert fs.last_sync_hash == "new"
        # Every other anchor survives the relaxed update.
        assert fs.tracked_save_id == 11
        assert fs.last_sync_at == "2026-05-28T10:00:00"
        assert fs.last_sync_server_save_id == 99
        assert fs.last_sync_server_size == 2048

    def test_does_not_add_a_second_entry_on_update(self):
        state = RomSaveState()
        state.update_baseline_hash("game.srm", "a")
        state.update_baseline_hash("game.srm", "b")
        assert list(state.files) == ["game.srm"]
        assert state.files["game.srm"].last_sync_hash == "b"

    def test_empty_hash_raises(self):
        state = RomSaveState()
        with pytest.raises(ValueError, match="last_sync_hash is required"):
            state.update_baseline_hash("game.srm", "")
        assert state.files == {}


class TestTrackOwnUpload:
    def test_starts_list_when_unknown(self):
        state = RomSaveState()
        assert state.own_upload_ids is None
        state.track_own_upload(5)
        assert state.own_upload_ids == [5]

    def test_appends_new_id(self):
        state = RomSaveState()
        state.track_own_upload(5)
        state.track_own_upload(6)
        assert state.own_upload_ids == [5, 6]

    def test_idempotent_on_existing_id(self):
        state = RomSaveState()
        state.track_own_upload(5)
        state.track_own_upload(5)
        assert state.own_upload_ids == [5]

    def test_appends_to_explicitly_empty_list(self):
        state = RomSaveState()
        state.own_upload_ids = []
        state.track_own_upload(7)
        assert state.own_upload_ids == [7]


class TestConfirmSlot:
    def test_named_slot_sets_active_confirmed_and_key(self):
        state = RomSaveState()
        state.confirm_slot("manual")
        assert state.active_slot == "manual"
        assert state.slot_confirmed is True
        assert state.slots["manual"] == {"source": "local", "count": 0, "latest_updated_at": None}

    def test_none_uses_empty_string_key(self):
        state = RomSaveState()
        state.confirm_slot(None)
        assert state.active_slot is None
        assert state.slot_confirmed is True
        assert state.slots[""] == {"source": "local", "count": 0, "latest_updated_at": None}

    def test_empty_string_normalizes_to_none(self):
        state = RomSaveState()
        state.confirm_slot("")
        assert state.active_slot is None
        assert state.slot_confirmed is True
        assert "" in state.slots

    def test_does_not_overwrite_existing_slot_entry(self):
        state = RomSaveState()
        state.slots["manual"] = {"source": "server", "count": 3, "latest_updated_at": "2026-05-28T10:00:00"}
        state.confirm_slot("manual")
        assert state.slots["manual"] == {
            "source": "server",
            "count": 3,
            "latest_updated_at": "2026-05-28T10:00:00",
        }


class TestSwitchActiveSlot:
    def test_named_slot_sets_active_and_key_without_confirming(self):
        state = RomSaveState()
        state.switch_active_slot("manual")
        assert state.active_slot == "manual"
        assert state.slot_confirmed is False
        assert state.slots["manual"] == {"source": "local", "count": 0, "latest_updated_at": None}

    def test_does_not_clear_existing_confirmation(self):
        state = RomSaveState()
        state.confirm_slot("a")
        assert state.slot_confirmed is True
        state.switch_active_slot("b")
        assert state.active_slot == "b"
        assert state.slot_confirmed is True

    def test_none_uses_empty_string_key(self):
        state = RomSaveState()
        state.switch_active_slot(None)
        assert state.active_slot is None
        assert state.slots[""] == {"source": "local", "count": 0, "latest_updated_at": None}

    def test_empty_string_normalizes_to_none(self):
        state = RomSaveState()
        state.switch_active_slot("")
        assert state.active_slot is None
        assert "" in state.slots

    def test_does_not_overwrite_existing_slot_entry(self):
        state = RomSaveState()
        state.slots["manual"] = {"source": "server", "count": 3, "latest_updated_at": None}
        state.switch_active_slot("manual")
        assert state.slots["manual"]["source"] == "server"


class TestMarkSyncEvaluated:
    def test_sets_last_sync_check_at(self):
        state = RomSaveState()
        state.mark_sync_evaluated("2026-05-28T12:00:00")
        assert state.last_sync_check_at == "2026-05-28T12:00:00"


class TestRecordSyncedCore:
    def test_sets_core_and_emulator(self):
        state = RomSaveState()
        state.record_synced_core("snes9x", "retroarch")
        assert state.last_synced_core == "snes9x"
        assert state.emulator == "retroarch"

    def test_overwrites_default_emulator(self):
        state = RomSaveState()
        state.record_synced_core("dolphin_core", "dolphin")
        assert state.emulator == "dolphin"

    def test_none_core_records_emulator_without_clobbering_core(self):
        state = RomSaveState()
        state.record_synced_core("snes9x", "retroarch")
        state.record_synced_core(None, "dolphin")
        # Emulator updated; the previously-known core survives the None.
        assert state.emulator == "dolphin"
        assert state.last_synced_core == "snes9x"

    def test_none_core_on_fresh_state_leaves_core_none(self):
        state = RomSaveState()
        state.record_synced_core(None, "retroarch")
        assert state.emulator == "retroarch"
        assert state.last_synced_core is None

    def test_empty_emulator_raises(self):
        state = RomSaveState()
        with pytest.raises(ValueError, match="emulator is required"):
            state.record_synced_core("snes9x", "")
        # Nothing recorded on rejection.
        assert state.emulator == "retroarch"
        assert state.last_synced_core is None


class TestPromoteSlotToServer:
    def test_flips_local_slot_to_server_and_seeds_count(self):
        state = RomSaveState()
        state.slots["manual"] = {"source": "local", "count": 0, "latest_updated_at": None}
        state.promote_slot_to_server("manual")
        assert state.slots["manual"]["source"] == "server"
        assert state.slots["manual"]["count"] == 1

    def test_noop_when_slot_already_server(self):
        state = RomSaveState()
        state.slots["manual"] = {"source": "server", "count": 5, "latest_updated_at": "2026-05-28T10:00:00"}
        state.promote_slot_to_server("manual")
        # Untouched — no double-count on a re-run.
        assert state.slots["manual"] == {
            "source": "server",
            "count": 5,
            "latest_updated_at": "2026-05-28T10:00:00",
        }

    def test_noop_when_slot_untracked(self):
        state = RomSaveState()
        state.promote_slot_to_server("ghost")
        assert "ghost" not in state.slots

    def test_empty_slot_raises(self):
        state = RomSaveState()
        with pytest.raises(ValueError, match="slot is required"):
            state.promote_slot_to_server("")


class TestDeleteFileTracking:
    def test_removes_tracked_file(self):
        state = RomSaveState()
        state.adopt_baseline("game.srm", tracked_save_id=1, last_sync_hash="abc")
        state.delete_file_tracking("game.srm")
        assert "game.srm" not in state.files

    def test_only_removes_named_file(self):
        state = RomSaveState()
        state.adopt_baseline("a.srm", tracked_save_id=1, last_sync_hash="abc")
        state.adopt_baseline("b.srm", tracked_save_id=2, last_sync_hash="def")
        state.delete_file_tracking("a.srm")
        assert list(state.files) == ["b.srm"]

    def test_noop_when_file_untracked(self):
        state = RomSaveState()
        state.adopt_baseline("a.srm", tracked_save_id=1, last_sync_hash="abc")
        state.delete_file_tracking("missing.srm")
        assert list(state.files) == ["a.srm"]


class TestDeleteSlotTracking:
    def test_removes_slot(self):
        state = RomSaveState()
        state.slots["manual"] = {"source": "server", "count": 1, "latest_updated_at": None}
        state.delete_slot_tracking("manual")
        assert "manual" not in state.slots

    def test_only_removes_named_slot(self):
        state = RomSaveState()
        state.slots["a"] = {"source": "local", "count": 0, "latest_updated_at": None}
        state.slots["b"] = {"source": "server", "count": 2, "latest_updated_at": None}
        state.delete_slot_tracking("a")
        assert list(state.slots) == ["b"]

    def test_noop_when_slot_untracked(self):
        state = RomSaveState()
        state.slots["a"] = {"source": "local", "count": 0, "latest_updated_at": None}
        state.delete_slot_tracking("ghost")
        assert list(state.slots) == ["a"]


class TestRefreshSlotListing:
    def test_replaces_slots(self):
        state = RomSaveState()
        state.slots = {"old": {"source": "local"}}
        merged = {"a": {"source": "server", "count": 1, "latest_updated_at": None}}
        state.refresh_slot_listing(merged)
        assert state.slots is merged
        assert "old" not in state.slots


class TestClearBaselines:
    def test_resets_files_to_empty(self):
        state = RomSaveState()
        state.adopt_baseline("game.srm", tracked_save_id=1, last_sync_hash="abc")
        assert state.files
        state.clear_baselines()
        assert state.files == {}
