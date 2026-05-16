"""Tests for SlotsService — slot management, tracking, and slot lifecycle."""

import asyncio
import hashlib
import json

import pytest

from domain.save_state import FileSyncState, RomSaveState
from lib.errors import RommApiError
from tests.services.saves._helpers import (
    _create_save,
    _file_md5,
    _install_rom,
    _server_save,
    make_service,
)


class TestSaveSlots:
    """Tests for get_save_slots and _set_active_slot."""

    @pytest.mark.asyncio
    async def test_get_save_slots(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "server-dev-1"

        fake.saves[1] = {
            "id": 1,
            "rom_id": 123,
            "file_name": "a.srm",
            "updated_at": "2026-03-24T10:00:00",
            "slot": "default",
        }
        fake.saves[2] = {
            "id": 2,
            "rom_id": 123,
            "file_name": "b.srm",
            "updated_at": "2026-03-24T08:00:00",
            "slot": "desktop",
        }

        result = await svc.get_save_slots(123)
        assert result["success"] is True
        assert len(result["slots"]) == 2
        assert result["active_slot"] == "default"

    @pytest.mark.asyncio
    async def test_get_save_slots_latest_updated_at_from_server(self, tmp_path):
        """latest_updated_at is populated from nested latest.updated_at, not a flat key."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "server-dev-1"

        # Two saves in the default slot; the later one should win.
        fake.saves[1] = {
            "id": 1,
            "rom_id": 123,
            "file_name": "a.srm",
            "updated_at": "2026-04-16T13:00:00",
            "slot": "default",
        }
        fake.saves[2] = {
            "id": 2,
            "rom_id": 123,
            "file_name": "b.srm",
            "updated_at": "2026-04-17T20:00:00",
            "slot": "default",
        }

        result = await svc.get_save_slots(123)
        assert result["success"] is True
        slot = next(s for s in result["slots"] if s["slot"] == "default")
        assert slot["latest_updated_at"] == "2026-04-17T20:00:00"

        # Also verify the value is persisted in state (not None)
        persisted = svc._save_sync_state.saves["123"].slots["default"]
        assert persisted["latest_updated_at"] == "2026-04-17T20:00:00"

    @pytest.mark.asyncio
    async def test_get_save_slots_disabled(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.get_save_slots(123)
        assert result["success"] is False

    def test_set_active_slot(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.saves["123"] = RomSaveState(system="gba", active_slot="default")
        result = svc._slots._set_active_slot(123, "desktop")
        assert result["success"] is True
        assert svc._save_sync_state.saves["123"].active_slot == "desktop"

    def test_set_active_slot_creates_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        result = svc._slots._set_active_slot(456, "my-slot")
        assert result["success"] is True
        assert svc._save_sync_state.saves["456"].active_slot == "my-slot"

    def test_set_active_slot_empty_sets_none(self, tmp_path):
        """Empty string sets active_slot to None (legacy mode)."""
        svc, _ = make_service(tmp_path)
        result = svc._slots._set_active_slot(123, "")
        assert result["success"] is True
        assert result["active_slot"] is None
        assert svc._save_sync_state.saves["123"].active_slot is None

    @pytest.mark.asyncio
    async def test_set_active_slot_triggers_background_check(self, tmp_path):
        """_set_active_slot fires a background save status check task."""
        emitted = []

        async def fake_emit(event, *args):
            emitted.append((event, args))

        svc, _ = make_service(tmp_path, emit=fake_emit)
        _install_rom(svc, tmp_path)

        svc._slots._set_active_slot(42, "slot1")

        # Give the background task a chance to run
        await asyncio.sleep(0.1)

        assert any(e[0] == "save_status_updated" for e in emitted)


class TestSaveTrackingConfigured:
    def test_not_configured_by_default(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is False
        assert result["active_slot"] is None

    def test_configured_after_setting_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "slot_confirmed": True,
                "active_slot": "default",
                "files": {},
            }
        )
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is True
        assert result["active_slot"] == "default"

    def test_not_configured_when_slot_confirmed_false(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "slot_confirmed": False,
                "active_slot": "default",
                "files": {},
            }
        )
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is False
        assert result["active_slot"] is None

    def test_handles_missing_saves_section(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.saves = {}
        result = svc.is_save_tracking_configured(999)
        assert result["configured"] is False


class TestGetSaveSetupInfo:
    @pytest.mark.asyncio
    async def test_scenario_a_no_local_server_has_saves(self, tmp_path):
        """Scenario A: No local save, server has saves."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        # Don't create local save
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is False
        assert len(result["local_files"]) == 0
        assert len(result["server_slots"]) == 1
        assert result["server_slots"][0]["slot"] is None
        assert result["server_slots"][0]["count"] == 1
        assert result["slot_confirmed"] is False
        assert result["active_slot"] is None

    @pytest.mark.asyncio
    async def test_scenario_b_local_no_server(self, tmp_path):
        """Scenario B: Local save exists, no server saves."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert len(result["local_files"]) == 1
        assert result["local_files"][0]["filename"] == "pokemon.srm"
        assert len(result["server_slots"]) == 0
        assert result["slot_confirmed"] is False

    @pytest.mark.asyncio
    async def test_scenario_c_local_and_server_different_slots(self, tmp_path):
        """Scenario C: Local save, server has saves in different slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot="desktop")

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert len(result["server_slots"]) == 1
        assert result["server_slots"][0]["slot"] == "desktop"
        assert result["default_slot"] == "default"

    @pytest.mark.asyncio
    async def test_scenario_e_local_and_server_same_default_slot(self, tmp_path):
        """Scenario E: Local save, server has saves in default slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot="default")

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert len(result["server_slots"]) == 1
        assert result["server_slots"][0]["slot"] == "default"
        assert result["default_slot"] == "default"

    @pytest.mark.asyncio
    async def test_already_confirmed(self, tmp_path):
        """When slot is already confirmed, report it."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "slot_confirmed": True,
                "active_slot": "desktop",
                "files": {},
            }
        )
        _install_rom(svc, tmp_path)

        result = await svc.get_save_setup_info(42)
        assert result["slot_confirmed"] is True
        assert result["active_slot"] == "desktop"

    @pytest.mark.asyncio
    async def test_multiple_server_slots(self, tmp_path):
        """Server saves across multiple slots are grouped correctly."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot="default")
        fake.saves[2] = _server_save(save_id=2, slot="desktop", filename="pokemon.srm")

        result = await svc.get_save_setup_info(42)
        assert len(result["server_slots"]) == 2
        slot_names = {s["slot"] for s in result["server_slots"]}
        assert slot_names == {"default", "desktop"}

    @pytest.mark.asyncio
    async def test_server_error_returns_empty_slots(self, tmp_path):
        """Server API failure still returns local info with empty server_slots."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.fail_on_next(RommApiError(500, "Server error"))

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert result["server_slots"] == []

    @pytest.mark.asyncio
    async def test_no_rom_installed(self, tmp_path):
        """No installed ROM means no local files."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # Don't install any ROM
        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is False
        assert result["local_files"] == []

    @pytest.mark.asyncio
    async def test_get_save_setup_info_recommends_auto_confirm_when_local_saves_no_server_slots(self, tmp_path):
        """Local saves + no server slots -> wizard should auto-confirm the default slot."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert result["server_slots"] == []
        assert result["recommended_action"] == "auto_confirm_default"

    @pytest.mark.asyncio
    async def test_get_save_setup_info_recommends_wizard_when_server_has_slots(self, tmp_path):
        """Local saves + server has slots -> user must choose, wizard required."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot="desktop")

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert len(result["server_slots"]) == 1
        assert result["recommended_action"] == "show_wizard"

    @pytest.mark.asyncio
    async def test_get_save_setup_info_recommends_wizard_when_no_local_saves(self, tmp_path):
        """No local saves -> wizard required regardless of server state."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        # No _create_save call - no local saves
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is False
        assert result["recommended_action"] == "show_wizard"


class TestConfirmSlotChoice:
    @pytest.mark.asyncio
    async def test_confirm_sets_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        result = await svc.confirm_slot_choice(42, "default")
        assert result["success"] is True
        state = svc._save_sync_state.saves["42"]
        assert state.slot_confirmed is True
        assert state.active_slot == "default"

    @pytest.mark.asyncio
    async def test_confirm_empty_slot_rejected(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.confirm_slot_choice(42, "")
        assert result["success"] is False
        assert "empty" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_confirm_whitespace_slot_rejected(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.confirm_slot_choice(42, "   ")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_confirm_preserves_existing_files_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {"pokemon.srm": {"last_sync_hash": "abc"}},
                "active_slot": "old",
            }
        )
        result = await svc.confirm_slot_choice(42, "new-slot")
        assert result["success"] is True
        state = svc._save_sync_state.saves["42"]
        assert state.active_slot == "new-slot"
        assert state.slot_confirmed is True
        # Existing files state preserved
        assert state.files["pokemon.srm"].last_sync_hash == "abc"

    @pytest.mark.asyncio
    async def test_confirm_persists_to_disk(self, tmp_path):
        svc, _ = make_service(tmp_path)
        await svc.confirm_slot_choice(42, "default")
        # State file should exist

        state_path = tmp_path / "save_sync_state.json"
        assert state_path.exists()
        saved = json.loads(state_path.read_text())
        assert saved["saves"]["42"]["slot_confirmed"] is True

    @pytest.mark.asyncio
    async def test_confirm_with_legacy_no_slot_migration(self, tmp_path):
        """Migrate: re-upload to new slot, delete old.

        ``None`` for ``migrate_from_slot`` means "migrate from legacy
        no-slot server saves". Facade translates ``None`` to the
        no-migration sentinel, so this exercises ``SlotsService`` directly
        where the legacy ``None`` semantics still live.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        # Old save on server with slot=None (legacy)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc._slots.confirm_slot_choice(42, "default", migrate_from_slot=None)
        assert result["success"] is True
        # New save should have been uploaded
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) >= 1
        # Check it was uploaded with the new slot
        assert upload_calls[0][2].get("slot") == "default"
        # Old save should have been deleted
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 1
        assert 1 in delete_calls[0][1][0]  # save_id 1 in the list

    @pytest.mark.asyncio
    async def test_confirm_migration_no_old_saves(self, tmp_path):
        """Migration with no matching old saves is a no-op."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        # Server save is in "default" slot, but we're migrating from "desktop"
        fake.saves[1] = _server_save(save_id=1, slot="default")

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot="desktop")
        assert result["success"] is True
        # No upload or delete should happen (no saves in "desktop" slot)
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 0
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0

    @pytest.mark.asyncio
    async def test_confirm_migration_failure_still_confirms_slot(self, tmp_path):
        """Migration failure should still confirm the slot but report the issue.

        Exercises ``SlotsService`` directly because the facade translates
        ``None`` to the no-migration sentinel; legacy ``None`` migration
        semantics live on the slots service.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        # Make upload_save fail during migration
        def failing_upload(*args, **kwargs):
            raise RommApiError(500, "Server error")

        fake.upload_save = failing_upload

        result = await svc._slots.confirm_slot_choice(42, "default", migrate_from_slot=None)
        assert result["success"] is True
        assert "migration failed" in result["message"].lower()
        # Slot is still confirmed despite migration failure
        assert svc._save_sync_state.saves["42"].slot_confirmed is True

    @pytest.mark.asyncio
    async def test_facade_translates_none_to_no_migration(self, tmp_path):
        """Facade: ``None`` for ``migrate_from_slot`` skips migration."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot=None)
        assert result["success"] is True
        # No migration occurred — no uploads / no deletes
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 0
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0
        assert svc._save_sync_state.saves["42"].slot_confirmed is True

    @pytest.mark.asyncio
    async def test_facade_translates_no_migration_string_to_no_migration(self, tmp_path):
        """Facade: ``"__no_migration__"`` string (from frontend) skips migration."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot="__no_migration__")
        assert result["success"] is True
        # No migration occurred — no uploads / no deletes
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 0
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0
        assert svc._save_sync_state.saves["42"].slot_confirmed is True

    @pytest.mark.asyncio
    async def test_is_configured_after_confirm(self, tmp_path):
        """is_save_tracking_configured returns True after confirm_slot_choice."""
        svc, _ = make_service(tmp_path)
        assert svc.is_save_tracking_configured(42)["configured"] is False
        await svc.confirm_slot_choice(42, "default")
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is True
        assert result["active_slot"] == "default"


class TestGetSlotSaves:
    """Tests for get_slot_saves — lightweight server save listing by slot."""

    @pytest.mark.asyncio
    async def test_happy_path(self, tmp_path):
        """Returns mapped save dicts for the requested slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = "server-dev-1"

        fake.saves[1] = {
            "id": 1,
            "rom_id": 42,
            "file_name": "mario.srm",
            "updated_at": "2026-03-24T10:00:00Z",
            "file_size_bytes": 2048,
            "emulator": "retroarch",
            "slot": "default",
        }
        fake.saves[2] = {
            "id": 2,
            "rom_id": 42,
            "file_name": "mario.state",
            "updated_at": "2026-03-24T09:00:00Z",
            "file_size_bytes": 512,
            "emulator": "retroarch",
            "slot": "default",
        }

        result = await svc.get_slot_saves(42, "default")

        assert result["success"] is True
        assert result["slot"] == "default"
        assert len(result["saves"]) == 2
        save = next(s for s in result["saves"] if s["id"] == 1)
        assert save["filename"] == "mario.srm"
        assert save["size"] == 2048
        assert save["updated_at"] == "2026-03-24T10:00:00Z"
        assert save["emulator"] == "retroarch"
        # Verify list_saves was called with the correct slot kwarg
        assert any(call[0] == "list_saves" and call[2].get("slot") == "default" for call in fake.call_log)

    @pytest.mark.asyncio
    async def test_empty_slot(self, tmp_path):
        """Returns empty saves list when server has no saves for the slot."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = "server-dev-1"
        # No saves added to fake

        result = await svc.get_slot_saves(42, "desktop")

        assert result["success"] is True
        assert result["slot"] == "desktop"
        assert result["saves"] == []

    @pytest.mark.asyncio
    async def test_server_error(self, tmp_path):
        """Returns error response when list_saves raises an exception."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = "server-dev-1"
        fake.fail_on_next(RommApiError("connection timeout"))

        result = await svc.get_slot_saves(42, "default")

        assert result["success"] is False
        assert result["slot"] == "default"
        assert result["saves"] == []
        assert "connection timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_sync_disabled(self, tmp_path):
        """Returns error response when save sync is disabled."""
        svc, _ = make_service(tmp_path)
        # save_sync_enabled defaults to False

        result = await svc.get_slot_saves(42, "default")

        assert result["success"] is False
        assert result["slot"] == "default"
        assert result["saves"] == []
        assert "disabled" in result["error"].lower()


class TestSwitchSlot:
    """Tests for SaveService.switch_slot — guarded slot switch with immediate download."""

    def _synced_state(self, local_hash: str, save_id: int = 100) -> RomSaveState:
        """Return a save state where the file appears fully synced."""
        return RomSaveState(
            active_slot="default",
            slot_confirmed=True,
            files={
                "pokemon.srm": FileSyncState(
                    last_sync_hash=local_hash,
                    last_sync_at="2026-01-01T00:00:00Z",
                    last_sync_server_updated_at="2026-01-01T00:00:00Z",
                    last_sync_server_save_id=save_id,
                    last_sync_server_size=1024,
                    tracked_save_id=save_id,
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_happy_path(self, tmp_path):
        """Files fully synced + server has saves in new slot → downloads and returns success."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Slot already synced — hash matches
        svc._save_sync_state.saves["42"] = self._synced_state(local_hash)

        # Server has a save in "desktop" slot
        fake.saves[200] = _server_save(save_id=200, slot="desktop")

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is True
        assert "save_status" in result
        # active_slot was updated
        assert svc._save_sync_state.saves["42"].active_slot == "desktop"
        # The server save was downloaded
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) >= 1

    @pytest.mark.asyncio
    async def test_pending_uploads_blocked(self, tmp_path):
        """Local file changed since last sync → switch blocked with reason + file list."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"modified save data")

        # State records an *old* hash — hash mismatch simulates pending upload
        old_hash = hashlib.md5(b"original save data").hexdigest()
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "last_sync_hash": old_hash,
                        "last_sync_at": "2026-01-01T00:00:00Z",
                        "tracked_save_id": 100,
                    },
                },
                "active_slot": "default",
                "slot_confirmed": True,
            }
        )

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "pending_uploads"
        assert "pokemon.srm" in result["files"]
        # No downloads should have happened
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0

    @pytest.mark.asyncio
    async def test_never_synced_not_blocked(self, tmp_path):
        """Local save exists but was never synced (no last_sync_hash) → switch NOT blocked.

        Never-synced files will be deleted during the switch, so they must not block it.
        After the switch to an empty slot the local file should be gone.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # State has the game entry but no last_sync_hash for the file
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {},  # no entry for pokemon.srm at all
                "active_slot": "default",
                "slot_confirmed": True,
            }
        )

        # No server saves in "desktop" slot → switch succeeds and deletes local file
        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is True
        assert not save_path.exists()

    @pytest.mark.asyncio
    async def test_server_unreachable(self, tmp_path):
        """list_saves raises → switch blocked with reason=server_unreachable."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Files synced so readiness check passes
        svc._save_sync_state.saves["42"] = self._synced_state(local_hash)

        fake.fail_on_next(RommApiError(503, "Service unavailable"))

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

    @pytest.mark.asyncio
    async def test_sync_disabled(self, tmp_path):
        """Save sync disabled → immediate error, no API calls."""
        svc, fake = make_service(tmp_path)
        # save_sync_enabled defaults to False
        _install_rom(svc, tmp_path)

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "sync_disabled"
        assert len(fake.call_log) == 0

    @pytest.mark.asyncio
    async def test_not_installed(self, tmp_path):
        """ROM not installed → returns not_installed error."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # ROM 42 is NOT installed

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "not_installed"

    @pytest.mark.asyncio
    async def test_empty_new_slot(self, tmp_path):
        """New slot has no saves on server → deletes local files and updates active_slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Files synced so readiness check passes
        svc._save_sync_state.saves["42"] = self._synced_state(local_hash)

        # Server has no saves in "newslot" (all fake saves are in other slots)
        fake.saves[300] = _server_save(save_id=300, slot="other")

        result = await svc.switch_slot(42, "newslot")

        assert result["success"] is True
        assert svc._save_sync_state.saves["42"].active_slot == "newslot"
        # No downloads
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0
        # Local file deleted (fresh start for empty slot)
        assert not save_path.exists()
        # File tracking state cleared
        assert svc._save_sync_state.saves["42"].files == {}

    @pytest.mark.asyncio
    async def test_empty_slot_deletes_local_files(self, tmp_path):
        """New slot is empty → local save files deleted and file tracking cleared."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Current slot is fully synced
        svc._save_sync_state.saves["42"] = self._synced_state(local_hash)

        # No server saves for "brand-new-slot"
        result = await svc.switch_slot(42, "brand-new-slot")

        assert result["success"] is True
        assert svc._save_sync_state.saves["42"].active_slot == "brand-new-slot"
        # Local save file removed
        assert not save_path.exists()
        # File tracking state cleared so next play starts fresh
        assert svc._save_sync_state.saves["42"].files == {}
        # No downloads happened
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0

    @pytest.mark.asyncio
    async def test_with_server_saves_downloads(self, tmp_path):
        """New slot has server saves → downloads them, replacing local file."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"old local save")
        local_hash = _file_md5(str(save_path))

        # Current slot is fully synced
        svc._save_sync_state.saves["42"] = self._synced_state(local_hash)

        # Target slot has a server save
        fake.saves[500] = _server_save(save_id=500, slot="target-slot")

        result = await svc.switch_slot(42, "target-slot")

        assert result["success"] is True
        assert svc._save_sync_state.saves["42"].active_slot == "target-slot"
        # Server save was downloaded (replaces local)
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) >= 1

    @pytest.mark.asyncio
    async def test_no_local_files_is_ready(self, tmp_path):
        """ROM installed but no local save files → readiness check passes (nothing pending)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        # No save file created on disk
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {},
                "active_slot": "default",
                "slot_confirmed": True,
            }
        )

        fake.saves[100] = _server_save(save_id=100, slot="desktop")

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is True
        assert svc._save_sync_state.saves["42"].active_slot == "desktop"

    @pytest.mark.asyncio
    async def test_switch_to_legacy_slot(self, tmp_path):
        """switch_slot("") sets active_slot=None, persists "" in slots dict, returns success."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Start in a named slot, fully synced
        svc._save_sync_state.saves["42"] = self._synced_state(local_hash)
        svc._save_sync_state.saves["42"].active_slot = "default"

        # Server has a legacy save (slot=None)
        fake.saves[200] = _server_save(save_id=200, slot=None)

        result = await svc.switch_slot(42, "")

        assert result["success"] is True
        assert "save_status" in result
        # active_slot in state is None (legacy)
        assert svc._save_sync_state.saves["42"].active_slot is None
        # Legacy slot "" appears in the slots dict
        slots_dict = svc._save_sync_state.saves["42"].slots
        assert "" in slots_dict

    @pytest.mark.asyncio
    async def test_legacy_slot_persisted_in_get_save_slots(self, tmp_path):
        """get_save_slots includes the "" entry when active_slot is None and "" is in slots dict."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True

        # Set up state with legacy slot explicitly
        svc._save_sync_state.saves["99"] = RomSaveState.from_dict(
            {
                "active_slot": None,
                "slot_confirmed": True,
                "files": {},
                "slots": {"": {"source": "local", "count": 0, "latest_updated_at": None}},
            }
        )

        # Server returns no slots
        result = await svc.get_save_slots(99)

        assert result["success"] is True
        # The "" entry should be in the response slots list
        slot_names = [s["slot"] for s in result["slots"]]
        assert "" in slot_names
        # active_slot is None (legacy)
        assert result["active_slot"] is None

    @pytest.mark.asyncio
    async def test_server_legacy_save_maps_to_empty_string_not_default(self, tmp_path):
        """Server saves with slot=None (legacy) must map to "" not "default" in get_save_slots."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "dev-1"

        # Server has a legacy save with slot=None
        fake.saves[1] = {
            "id": 1,
            "rom_id": 77,
            "file_name": "game.srm",
            "updated_at": "2026-04-07T10:00:00",
            "slot": None,
        }

        result = await svc.get_save_slots(77)

        assert result["success"] is True
        slot_names = [s["slot"] for s in result["slots"]]
        # Must be "" (legacy key), NOT "default"
        assert "" in slot_names
        assert "default" not in slot_names


class TestDeleteSlot:
    """Tests for SaveService.delete_slot and get_slot_delete_info."""

    def _setup_state_with_slots(
        self,
        svc,
        tmp_path,
        *,
        active_slot="default",
        extra_slots=None,
        files_state=None,
    ):
        """Set up a ROM with slot state for deletion tests."""
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        svc._save_sync_state.server_device_id = "server-dev-1"
        _install_rom(svc, tmp_path)

        slots = {
            "default": {"source": "server", "count": 1, "latest_updated_at": "2026-03-24T10:00:00"},
        }
        if extra_slots:
            slots.update(extra_slots)

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "active_slot": active_slot,
                "slot_confirmed": True,
                "slots": slots,
                "files": files_state or {},
            }
        )

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_server_slot(self, tmp_path):
        """Server slot returns save count and tracked file info."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 3, "latest_updated_at": None}},
            files_state={
                "pokemon.srm": {"tracked_save_id": 10, "last_sync_hash": "abc"},
                "zelda.srm": {"tracked_save_id": 11, "last_sync_hash": "def"},
                "unrelated.srm": {"tracked_save_id": 99, "last_sync_hash": "ghi"},
            },
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        fake.saves[11] = _server_save(save_id=11, rom_id=42, filename="zelda.srm", slot="save1")
        fake.saves[12] = _server_save(save_id=12, rom_id=42, filename="extra.srm", slot="save1")

        result = await svc.get_slot_delete_info(42, "save1")

        assert result["success"] is True
        assert result["server_save_count"] == 3
        assert set(result["server_save_ids"]) == {10, 11, 12}
        assert result["local_file_count"] == 2
        assert set(result["local_filenames"]) == {"pokemon.srm", "zelda.srm"}
        assert result["is_active"] is False

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_local_only_slot(self, tmp_path):
        """Local-only slot returns zero server saves."""
        svc, _fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"local1": {"source": "local", "count": 0, "latest_updated_at": None}},
        )

        result = await svc.get_slot_delete_info(42, "local1")

        assert result["success"] is True
        assert result["source"] == "local"
        assert result["server_save_count"] == 0
        assert result["local_file_count"] == 0

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_active_slot(self, tmp_path):
        """Info for the active slot still returns data (is_active=True)."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(svc, tmp_path, active_slot="default")
        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default")

        result = await svc.get_slot_delete_info(42, "default")

        assert result["success"] is True
        assert result["is_active"] is True
        assert result["server_save_count"] == 1

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_nonexistent_slot(self, tmp_path):
        """Non-existent slot returns not_found."""
        svc, _fake = make_service(tmp_path)
        self._setup_state_with_slots(svc, tmp_path)

        result = await svc.get_slot_delete_info(42, "nonexistent")

        assert result["success"] is False
        assert result["reason"] == "not_found"

    @pytest.mark.asyncio
    async def test_delete_slot_server_saves_success(self, tmp_path):
        """Deleting a server slot removes server saves and cleans up state."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 2, "latest_updated_at": None}},
            files_state={
                "pokemon.srm": {"tracked_save_id": 10, "last_sync_hash": "abc"},
                "zelda.srm": {"tracked_save_id": 11, "last_sync_hash": "def"},
            },
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        fake.saves[11] = _server_save(save_id=11, rom_id=42, filename="zelda.srm", slot="save1")

        result = await svc.delete_slot(42, "save1")

        assert result["success"] is True
        assert result["deleted_server_saves"] == 2
        assert result["cleaned_files"] == 2
        # Slot removed from state
        assert "save1" not in svc._save_sync_state.saves["42"].slots
        # File entries cleaned
        assert "pokemon.srm" not in svc._save_sync_state.saves["42"].files
        assert "zelda.srm" not in svc._save_sync_state.saves["42"].files
        # delete_server_saves called with correct IDs
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 1
        assert set(delete_calls[0][1][0]) == {10, 11}

    @pytest.mark.asyncio
    async def test_delete_slot_local_only_success(self, tmp_path):
        """Deleting a local-only slot skips server calls."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"local1": {"source": "local", "count": 0, "latest_updated_at": None}},
        )

        result = await svc.delete_slot(42, "local1")

        assert result["success"] is True
        assert result["deleted_server_saves"] == 0
        assert "local1" not in svc._save_sync_state.saves["42"].slots
        # No server calls made
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0

    @pytest.mark.asyncio
    async def test_delete_slot_blocks_active_slot(self, tmp_path):
        """Cannot delete the active slot."""
        svc, _fake = make_service(tmp_path)
        self._setup_state_with_slots(svc, tmp_path, active_slot="default")

        result = await svc.delete_slot(42, "default")

        assert result["success"] is False
        assert result["reason"] == "active_slot"
        # Slot still exists
        assert "default" in svc._save_sync_state.saves["42"].slots

    @pytest.mark.asyncio
    async def test_delete_slot_server_error(self, tmp_path):
        """Server error leaves slot intact (no partial cleanup)."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 1, "latest_updated_at": None}},
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        # First list_saves call succeeds, then delete_server_saves fails
        original_delete = fake.delete_server_saves

        def fail_delete(save_ids):
            raise RommApiError(500, "Server error")

        fake.delete_server_saves = fail_delete

        result = await svc.delete_slot(42, "save1")

        assert result["success"] is False
        assert result["reason"] == "server_error"
        # Slot NOT removed from state (rollback on failure)
        assert "save1" in svc._save_sync_state.saves["42"].slots

        fake.delete_server_saves = original_delete

    @pytest.mark.asyncio
    async def test_delete_slot_cleans_up_tracked_files(self, tmp_path):
        """Only file entries pointing to deleted saves are removed; unrelated entries preserved."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 2, "latest_updated_at": None}},
            files_state={
                "pokemon.srm": {"tracked_save_id": 10, "last_sync_hash": "abc"},
                "zelda.srm": {"tracked_save_id": 11, "last_sync_hash": "def"},
                "unrelated.srm": {"tracked_save_id": 99, "last_sync_hash": "ghi"},
            },
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        fake.saves[11] = _server_save(save_id=11, rom_id=42, filename="zelda.srm", slot="save1")

        result = await svc.delete_slot(42, "save1")

        assert result["success"] is True
        files = svc._save_sync_state.saves["42"].files
        assert "pokemon.srm" not in files
        assert "zelda.srm" not in files
        assert "unrelated.srm" in files
        assert files["unrelated.srm"].tracked_save_id == 99

    @pytest.mark.asyncio
    async def test_delete_slot_not_installed_rom(self, tmp_path):
        """ROM not installed returns failure."""
        svc, _fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # Don't install any ROM

        result = await svc.delete_slot(42, "default")

        assert result["success"] is False
        assert result["reason"] == "not_installed"

    @pytest.mark.asyncio
    async def test_delete_slot_sync_disabled(self, tmp_path):
        """Save sync disabled returns failure."""
        svc, _fake = make_service(tmp_path)
        # save_sync_enabled defaults to False

        result = await svc.delete_slot(42, "default")

        assert result["success"] is False
        assert result["reason"] == "disabled"
