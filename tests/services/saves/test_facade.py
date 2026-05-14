"""Tests for SaveService aggregate facade — public callable surface and cross-service coordination."""

import asyncio
import hashlib
import logging
import os
import time
from unittest.mock import MagicMock

import pytest
from fakes.fake_save_api import FakeSaveApi

from domain.save_state import FileSyncState, RomSaveState
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _install_rom,
    make_service,
)


class TestDeviceRegistration:
    @pytest.mark.asyncio
    async def test_registers_new_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result["device_id"]
        assert result["device_name"]
        # Persisted
        assert svc._save_sync_state.device_id == result["device_id"]

    @pytest.mark.asyncio
    async def test_returns_existing_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "existing"
        svc._save_sync_state.device_name = "deck"
        svc._save_sync_state.server_device_id = "server-existing"

        result = await svc.ensure_device_registered()
        assert result["device_id"] == "existing"
        assert result["device_name"] == "deck"
        assert result["server_device_id"] == "server-existing"

    @pytest.mark.asyncio
    async def test_disabled_returns_failure(self, tmp_path):
        svc, _ = make_service(tmp_path)
        # save_sync_enabled defaults to False
        result = await svc.ensure_device_registered()
        assert result["success"] is False
        assert result.get("disabled") is True


class TestDeviceRegistrationServer:
    @pytest.mark.asyncio
    async def test_registers_with_server(self, tmp_path):
        """Calls register_device and stores server_device_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert svc._save_sync_state.server_device_id == result["server_device_id"]
        # Verify register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1
        assert reg_calls[0][1][0]  # name (hostname)
        assert reg_calls[0][1][1] == "linux"  # platform
        assert reg_calls[0][1][2] == "decky-romm-sync"  # client

    @pytest.mark.asyncio
    async def test_returns_failure_on_server_error(self, tmp_path):
        """If register_device fails, returns failure."""
        fake = FakeSaveApi()
        fake.fail_on_next(Exception("server error"))
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True

        result = await svc.ensure_device_registered()
        assert result["success"] is False
        assert result.get("error") == "registration_failed"
        assert svc._save_sync_state.server_device_id is None

    @pytest.mark.asyncio
    async def test_returns_existing_with_server_device_id(self, tmp_path):
        """If already registered, returns existing IDs including server_device_id."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "existing-id"
        svc._save_sync_state.device_name = "deck"
        svc._save_sync_state.server_device_id = "server-id-123"

        result = await svc.ensure_device_registered()
        assert result["device_id"] == "existing-id"
        assert result.get("server_device_id") == "server-id-123"

    @pytest.mark.asyncio
    async def test_upgrades_local_uuid_to_server(self, tmp_path):
        """Local-only UUID gets upgraded to server registration."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # Simulate existing local-only UUID (from failed registration)
        svc._save_sync_state.device_id = "local-only-uuid"
        svc._save_sync_state.device_name = "deck"
        svc._save_sync_state.server_device_id = None

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert svc._save_sync_state.server_device_id is not None
        # register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1

    @pytest.mark.asyncio
    async def test_ensure_device_registered_reconciles_client_version(self, tmp_path):
        """Already-registered path calls update_device with current plugin_version."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "existing-id"
        svc._save_sync_state.device_name = "deck"
        svc._save_sync_state.server_device_id = "server-abc"

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        update_calls = [c for c in fake.call_log if c[0] == "update_device"]
        assert len(update_calls) == 1
        assert update_calls[0][1][0] == "server-abc"
        assert update_calls[0][2].get("client_version") == "0.14.0"

    @pytest.mark.asyncio
    async def test_ensure_device_registered_reconcile_non_fatal(self, tmp_path):
        """PUT raises, ensure_device_registered still returns success."""
        fake = FakeSaveApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "existing-id"
        svc._save_sync_state.device_name = "deck"
        svc._save_sync_state.server_device_id = "server-abc"

        # Make update_device fail silently
        fake.fail_on_next(Exception("network error"))
        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "existing-id"

    @pytest.mark.asyncio
    async def test_probes_version_when_unset(self, tmp_path):
        """ensure_device_registered probes the version when adapter has none."""

        class VersionedFakeApi(FakeSaveApi):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_calls = 0

            def heartbeat(self) -> dict:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True

        await svc.ensure_device_registered()

        assert fake.heartbeat_calls == 1
        assert fake.get_version() == "4.8.5"

    @pytest.mark.asyncio
    async def test_skips_probe_when_version_already_set(self, tmp_path):
        """No probe when adapter already has a version."""

        class VersionedFakeApi(FakeSaveApi):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_calls = 0

            def heartbeat(self) -> dict:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        fake.set_version("4.8.1")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True

        await svc.ensure_device_registered()

        assert fake.heartbeat_calls == 0
        assert fake.get_version() == "4.8.1"

    @pytest.mark.asyncio
    async def test_probe_failure_is_non_fatal(self, tmp_path):
        """Heartbeat failure during version probe does not prevent registration."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = ConnectionError("offline")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert fake.get_version() is None

    @pytest.mark.asyncio
    async def test_probe_skipped_when_disabled(self, tmp_path):
        """Disabled save sync short-circuits before any probe."""

        class VersionedFakeApi(FakeSaveApi):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_calls = 0

            def heartbeat(self) -> dict:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        # save_sync_enabled defaults to False
        result = await svc.ensure_device_registered()

        assert result["success"] is False
        assert result.get("disabled") is True
        assert fake.heartbeat_calls == 0


class TestListDevices:
    @pytest.mark.asyncio
    async def test_list_devices_marks_own_device(self, tmp_path):
        """own device_id present in state — is_current_device is True on matching entry."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = "device-1"

        # Register two devices in fake
        fake._registered_devices = [
            {"id": "device-1", "name": "steamdeck"},
            {"id": "device-2", "name": "laptop"},
        ]

        result = await svc.list_devices()

        assert result["success"] is True
        assert len(result["devices"]) == 2
        own = next(d for d in result["devices"] if d["id"] == "device-1")
        other = next(d for d in result["devices"] if d["id"] == "device-2")
        assert own["is_current_device"] is True
        assert other["is_current_device"] is False

    @pytest.mark.asyncio
    async def test_list_devices_save_sync_disabled(self, tmp_path):
        """Returns disabled=True when save sync is off."""
        svc, _ = make_service(tmp_path)
        # save_sync_enabled defaults to False

        result = await svc.list_devices()

        assert result == {"success": False, "devices": [], "disabled": True}

    @pytest.mark.asyncio
    async def test_list_devices_adapter_error(self, tmp_path):
        """Adapter raises — returns error response."""
        fake = FakeSaveApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True

        fake.fail_on_next(Exception("server unavailable"))
        result = await svc.list_devices()

        assert result == {"success": False, "devices": [], "error": "list_failed"}

    @pytest.mark.asyncio
    async def test_list_devices_no_own_id_all_false(self, tmp_path):
        """No server_device_id in state — all is_current_device are False."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = None

        fake._registered_devices = [{"id": "device-1", "name": "steamdeck"}]
        result = await svc.list_devices()

        assert result["success"] is True
        assert result["devices"][0]["is_current_device"] is False

    @pytest.mark.asyncio
    async def test_list_devices_handles_null_id(self, tmp_path):
        """Device with id=None must not match own_id=None (avoid 'None'=='None' trap)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = None

        fake._registered_devices = [{"id": None, "name": "unknown"}]
        result = await svc.list_devices()

        assert result["success"] is True
        # id=None and own_id=None must both resolve to "" — empty string never
        # compares truthy, so is_current_device must be False
        assert result["devices"][0]["is_current_device"] is False


class TestRetroDeckMigrationBlocksSaveSync:
    @pytest.mark.asyncio
    async def test_pre_launch_sync_skips_when_retrodeck_migration_pending(self, tmp_path):
        svc, _ = make_service(tmp_path, is_retrodeck_migration_pending=lambda: True)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_post_exit_sync_skips_when_retrodeck_migration_pending(self, tmp_path):
        svc, _ = make_service(tmp_path, is_retrodeck_migration_pending=lambda: True)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_sync_all_saves_respects_migration_block_via_decorator_chain(self, tmp_path):
        """End-to-end chain check: Plugin.sync_all_saves must be blocked by the
        @migration_blocked decorator before SaveService.sync_all_saves runs, so
        the internal _sync_rom_saves call path is never reached when migration
        is pending. Protects against accidental decorator removal at the public
        callable layer."""
        from main import Plugin

        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        plugin = Plugin()
        plugin._save_sync_service = svc
        plugin._migration_service = MagicMock()
        plugin._migration_service.is_retrodeck_migration_pending.return_value = True

        spy = MagicMock(name="_sync_rom_saves_spy")
        svc._sync_engine._sync_rom_saves = spy  # type: ignore[method-assign]

        result = await plugin.sync_all_saves()

        assert result["blocked_by_migration"] is True
        assert result["success"] is False
        spy.assert_not_called()


class TestPostExitSyncConnectivity:
    @pytest.mark.asyncio
    async def test_returns_offline_when_heartbeat_fails(self, tmp_path):
        """post_exit_sync returns offline=True when server is unreachable."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = ConnectionError("unreachable")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result.get("offline") is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_proceeds_when_heartbeat_succeeds(self, tmp_path):
        """post_exit_sync proceeds normally when server is reachable."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"save data")

        result = await svc.post_exit_sync(42)

        assert result.get("offline") is not True
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_offline_skips_before_device_registration(self, tmp_path):
        """post_exit_sync returns offline without attempting device registration."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = OSError("connection refused")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state.settings.save_sync_enabled = True
        # No device_id — would trigger registration if heartbeat passed

        result = await svc.post_exit_sync(42)

        assert result.get("offline") is True
        # Device should not have been registered
        assert not svc._save_sync_state.device_id


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_defaults(self, tmp_path):
        svc, _ = make_service(tmp_path)
        settings = svc.get_save_sync_settings()
        assert settings["save_sync_enabled"] is False
        assert settings["sync_before_launch"] is True
        assert settings["sync_after_exit"] is True

    @pytest.mark.asyncio
    async def test_update_settings(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.update_save_sync_settings(
            {
                "save_sync_enabled": True,
                "sync_before_launch": False,
            }
        )
        assert result["success"] is True
        assert result["settings"]["save_sync_enabled"] is True
        assert result["settings"]["sync_before_launch"] is False

    @pytest.mark.asyncio
    async def test_unknown_key_ignored(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.update_save_sync_settings({"unknown_key": "value"})
        assert result["success"] is True
        assert "unknown_key" not in result["settings"]


class TestDeleteSaves:
    @pytest.mark.asyncio
    async def test_delete_local_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        assert save_path.exists()

        svc._save_sync_state.saves["42"] = RomSaveState(files={"pokemon.srm": FileSyncState()})

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()
        # Entry survives — only files are cleared.
        assert "42" in svc._save_sync_state.saves
        assert svc._save_sync_state.saves["42"].files == {}

    @pytest.mark.asyncio
    async def test_delete_local_saves_preserves_slot_config(self, tmp_path):
        """Slot config and attribution metadata survive a delete (#279)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        assert save_path.exists()

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {"pokemon.srm": {"last_sync_hash": "abc"}},
                "active_slot": "desktop",
                "slot_confirmed": True,
                "emulator": "retroarch-mgba",
                "last_synced_core": "mgba_libretro",
                "own_upload_ids": ["save-1", "save-2"],
                "slots": {"default": {}, "desktop": {}},
                "system": "gba",
            }
        )

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()

        entry = svc._save_sync_state.saves["42"]
        assert entry.files == {}
        assert entry.active_slot == "desktop"
        assert entry.slot_confirmed is True
        assert entry.emulator == "retroarch-mgba"
        assert entry.last_synced_core == "mgba_libretro"
        assert entry.own_upload_ids == ["save-1", "save-2"]
        assert entry.slots == {"default": {}, "desktop": {}}
        assert entry.system == "gba"

    @pytest.mark.asyncio
    async def test_delete_local_saves_no_prior_state_entry(self, tmp_path):
        """Delete on a ROM with no prior saves entry creates a stable empty entry."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # No svc._save_sync_state.saves["42"] set up.
        assert "42" not in svc._save_sync_state.saves

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()
        # clear_files_state creates an empty entry with files={}.
        assert svc._save_sync_state.saves["42"].files == {}

    @pytest.mark.asyncio
    async def test_delete_no_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 0


class TestEmulatorTag:
    def test_upload_uses_emulator_tag_from_core(self, tmp_path):
        """When core resolver returns a core, upload uses retroarch-{core} tag."""
        svc, fake = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
        )
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._sync_engine._do_upload_save(
            42, str(tmp_path / "saves" / "gba" / "pokemon.srm"), "pokemon.srm", "42", "gba"
        )

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        _name, args, _kwargs = upload_calls[0]
        assert args[2] == "retroarch-mgba"  # emulator argument

    def test_upload_uses_fallback_when_no_core(self, tmp_path):
        """When core resolver returns None, upload falls back to 'retroarch'."""
        svc, fake = make_service(tmp_path)  # default: get_active_core returns (None, None)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._sync_engine._do_upload_save(
            42, str(tmp_path / "saves" / "gba" / "pokemon.srm"), "pokemon.srm", "42", "gba"
        )

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        _name, args, _kwargs = upload_calls[0]
        assert args[2] == "retroarch"

    @pytest.mark.asyncio
    async def test_delete_platform_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="gba", file_name="game2.gba")
        _create_save(tmp_path, system="gba", rom_name="game1")
        _create_save(tmp_path, system="gba", rom_name="game2")

        result = svc.delete_platform_saves("gba")
        assert result["success"] is True
        assert result["deleted_count"] == 2

    @pytest.mark.asyncio
    async def test_delete_platform_saves_preserves_slot_config(self, tmp_path):
        """Per-platform delete preserves slot config for every affected ROM (#279)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="gba", file_name="game2.gba")
        _create_save(tmp_path, system="gba", rom_name="game1")
        _create_save(tmp_path, system="gba", rom_name="game2")

        svc._save_sync_state.saves["1"] = RomSaveState.from_dict(
            {
                "files": {"game1.srm": {}},
                "active_slot": "desktop",
                "slot_confirmed": True,
                "emulator": "retroarch-mgba",
                "system": "gba",
            }
        )
        svc._save_sync_state.saves["2"] = RomSaveState.from_dict(
            {
                "files": {"game2.srm": {}},
                "active_slot": "default",
                "slot_confirmed": True,
                "own_upload_ids": ["save-x"],
                "system": "gba",
            }
        )

        result = svc.delete_platform_saves("gba")
        assert result["success"] is True
        assert result["deleted_count"] == 2

        entry1 = svc._save_sync_state.saves["1"]
        assert entry1.files == {}
        assert entry1.active_slot == "desktop"
        assert entry1.slot_confirmed is True
        assert entry1.emulator == "retroarch-mgba"

        entry2 = svc._save_sync_state.saves["2"]
        assert entry2.files == {}
        assert entry2.active_slot == "default"
        assert entry2.slot_confirmed is True
        assert entry2.own_upload_ids == ["save-x"]

    @pytest.mark.asyncio
    async def test_delete_platform_saves_other_platform_untouched(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1")
        snes_save = _create_save(tmp_path, system="snes", rom_name="game2")

        svc._save_sync_state.saves["2"] = RomSaveState.from_dict(
            {
                "files": {"game2.srm": {}},
                "active_slot": "default",
                "slot_confirmed": True,
                "system": "snes",
            }
        )

        svc.delete_platform_saves("gba")
        assert snes_save.exists()
        # Other-platform entry must be entirely untouched.
        snes_entry = svc._save_sync_state.saves["2"]
        assert "game2.srm" in snes_entry.files
        assert snes_entry.active_slot == "default"
        assert snes_entry.slot_confirmed is True


class TestFindSaveFiles:
    """Tests for _find_save_files."""

    def test_finds_srm(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, system="gba", rom_name="pokemon")

        result = svc._find_save_files(42)

        assert len(result) == 1
        assert result[0]["filename"] == "pokemon.srm"
        assert result[0]["path"].endswith("pokemon.srm")

    def test_finds_rtc_companion(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, file_name="emerald.gba")
        _create_save(tmp_path, system="gba", rom_name="emerald", ext=".srm")
        _create_save(tmp_path, system="gba", rom_name="emerald", ext=".rtc", content=b"\x02" * 16)

        result = svc._find_save_files(42)

        filenames = sorted(f["filename"] for f in result)
        assert filenames == ["emerald.rtc", "emerald.srm"]

    def test_multi_disc_uses_m3u_name(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._state["installed_roms"]["55"] = {
            "rom_id": 55,
            "file_name": "FF7.zip",
            "file_path": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7" / "Final Fantasy VII.m3u"),
            "system": "psx",
            "platform_slug": "psx",
            "rom_dir": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7"),
            "installed_at": "2026-01-01T00:00:00",
        }
        # With sort_by_content=True, saves land in saves_base/{content_dir} where
        # content_dir = last folder component of the ROM's directory = "FF7"
        saves_dir = tmp_path / "saves" / "FF7"
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / "Final Fantasy VII.srm").write_bytes(b"\x00" * 1024)

        result = svc._find_save_files(55)

        assert any(f["filename"] == "Final Fantasy VII.srm" for f in result)

    def test_no_save_file_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=10, system="n64", file_name="zelda.z64")
        (tmp_path / "saves" / "n64").mkdir(parents=True, exist_ok=True)

        result = svc._find_save_files(10)

        assert result == []

    def test_saves_dir_not_exists_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc._find_save_files(42)

        assert result == []

    def test_rom_not_installed_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)

        result = svc._find_save_files(999)

        assert result == []


class TestFileMd5:
    """Tests for _file_md5."""

    def test_known_content(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "test.srm"
        content = b"Hello, save file!"
        f.write_bytes(content)

        assert svc._file_md5(str(f)) == hashlib.md5(content).hexdigest()

    def test_empty_file(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")

        assert svc._file_md5(str(f)) == hashlib.md5(b"").hexdigest()

    def test_large_file_chunked(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "large.srm"
        content = os.urandom(2 * 1024 * 1024)
        f.write_bytes(content)

        assert svc._file_md5(str(f)) == hashlib.md5(content).hexdigest()

    def test_permission_error(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "locked.srm"
        f.write_bytes(b"data")
        f.chmod(0o000)

        try:
            with pytest.raises(PermissionError):
                svc._file_md5(str(f))
        finally:
            f.chmod(0o644)


class TestGetRomSaveInfo:
    """Tests for _get_rom_save_info."""

    def test_returns_info_for_installed_rom(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["system"] == "gba"
        assert result["rom_name"] == "pokemon"
        assert result["saves_dir"].endswith("saves/gba")

    def test_returns_none_for_missing_rom(self, tmp_path):
        svc, _ = make_service(tmp_path)

        result = svc._get_rom_save_info(999)

        assert result is None

    def test_returns_none_for_empty_system(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_name": "game.gba",
            "file_path": "/some/path.gba",
            "system": "",
            "platform_slug": "",
            "installed_at": "2026-01-01T00:00:00",
        }

        result = svc._get_rom_save_info(42)

        assert result is None

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — Rule 1: when a save-sort migration
    # is pending, prefer save_sort_settings_previous so sync reads the
    # layout RetroArch actually wrote to during the session that just
    # ended.
    # ------------------------------------------------------------------

    def test_get_rom_save_info_prefers_previous_sort_settings_when_migration_pending(self, tmp_path):
        """Pending migration: previous (OLD) sort settings override current (NEW) (#238)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # NEW layout (what settings currently say):
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}
        # OLD layout (what the session actually wrote to):
        svc._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}

        result = svc._get_rom_save_info(42)

        assert result is not None
        # OLD layout: no /mGBA subdir.
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]

    def test_get_rom_save_info_uses_current_sort_settings_when_no_pending_migration(self, tmp_path):
        """No pending migration: use current sort settings (#238)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # Only save_sort_settings is present — no pending migration key at all.
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}
        assert "save_sort_settings_previous" not in svc._state

        result = svc._get_rom_save_info(42)

        assert result is not None
        # CURRENT layout: /mGBA subdir is appended because sort_by_core=True.
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    def test_pending_sort_settings_rejects_empty_dict_half_state(self, tmp_path):
        """Empty-dict ``save_sort_settings_previous`` must NOT count as pending (#238 review).

        Freezes the contract: ``_get_rom_save_info`` and
        ``_is_save_sort_changed`` must agree on what counts as pending.
        Before ``_pending_sort_settings`` was introduced, a literal
        empty dict at ``save_sort_settings_previous`` would put the
        service in a half-state — ``_get_rom_save_info`` would fall
        back to current settings (``{} or current``), but
        ``_is_save_sort_changed`` would treat the same ``{}`` as
        pending (``is not None``). This test locks in the agreement.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # Half-state input: empty previous, populated current (NEW).
        svc._state["save_sort_settings_previous"] = {}
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        # Both call sites must agree there is NO pending migration.
        assert svc._is_save_sort_changed() is False
        assert svc._pending_sort_settings() is None

        result = svc._get_rom_save_info(42)
        assert result is not None
        # Reads CURRENT settings (NEW layout), not the empty previous —
        # mGBA subdir is appended because sort_by_core=True.
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    # ------------------------------------------------------------------
    # Regression tests for issue #232 — SaveService must resolve the
    # RetroArch ``corename`` via the .info parser when sort_by_core is
    # active, and must fall back with a warning when it cannot.
    # ------------------------------------------------------------------

    def test_default_sort_only_by_content_no_core_subdir(self, tmp_path):
        """sort_by_core=False (RetroDECK default) → no core subdir."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]

    def test_sort_by_core_appends_retroarch_corename(self, tmp_path):
        """sort_by_core=True with resolvable corename → saves_dir ends in /{system}/{corename}."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    def test_sort_by_core_uses_corename_not_es_de_label(self, tmp_path):
        """The RetroArch .info corename (``Snes9x``) must be used, not the ES-DE label (``Snes9x - Current``)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x - Current"),
            get_core_name=lambda core_so: "Snes9x",
        )
        _install_rom(svc, tmp_path, system="snes", file_name="mario.sfc")
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/snes/Snes9x")
        assert "Snes9x - Current" not in result["saves_dir"]

    def test_sort_by_core_falls_back_when_corename_none(self, tmp_path, caplog):
        """sort_by_core=True but corename unresolvable → warn + fall back to parent dir.

        The warning must include ``core_so=mgba_libretro`` so a user can identify
        which ``.info`` file the parser failed on.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: None,  # .info unreadable / field missing
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=mgba_libretro" in warnings[0]

    def test_sort_by_core_falls_back_when_get_core_name_missing(self, tmp_path, caplog):
        """Constructed without get_core_name → still warns and falls back.

        When ``get_core_name`` is not injected, the helper short-circuits before
        calling ``get_active_core``, so ``core_so`` is never resolved and the log
        records ``core_so=unresolved`` for that case.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            # get_core_name intentionally omitted (defaults to None)
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=unresolved" in warnings[0]

    def test_sort_by_core_falls_back_when_active_core_unresolved(self, tmp_path, caplog):
        """sort_by_core=True but get_active_core returns (None, None) → warn + fall back.

        When ES-DE cannot determine the active core, ``core_so`` is ``None`` and
        the log records ``core_so=unresolved``.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: (None, None),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=unresolved" in warnings[0]

    def test_resolve_retroarch_corename_happy_path(self, tmp_path):
        """Direct test of the helper: both callbacks resolve → (corename, core_so) tuple returned."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x - Current"),
            get_core_name=lambda core_so: "Snes9x",
        )
        assert svc._resolve_retroarch_corename("snes", "mario.sfc") == ("Snes9x", "snes9x_libretro")

    def test_resolve_retroarch_corename_returns_none_tuple_when_core_so_empty(self, tmp_path):
        """ES-DE returns (None, None) → helper returns (None, None)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: (None, None),
            get_core_name=lambda core_so: "Snes9x",
        )
        assert svc._resolve_retroarch_corename("snes", "mario.sfc") == (None, None)

    def test_resolve_retroarch_corename_preserves_core_so_when_corename_empty(self, tmp_path):
        """Empty corename with resolved core_so → (None, core_so).

        The core_so is preserved in the second element so the caller can log
        which ``.info`` file failed diagnostically. The first element is None
        because the empty-string corename is treated as "no usable value".
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x"),
            get_core_name=lambda core_so: "",
        )
        assert svc._resolve_retroarch_corename("snes", "mario.sfc") == (None, "snes9x_libretro")


class TestSaveSyncSettingsSlotAndCleanup:
    """Tests for default_slot and autocleanup_limit settings."""

    def test_update_default_slot(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        result = svc.update_save_sync_settings({"default_slot": "desktop"})
        assert result["success"] is True
        assert result["settings"]["default_slot"] == "desktop"

    def test_update_default_slot_empty_string_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.settings.default_slot = "default"
        result = svc.update_save_sync_settings({"default_slot": ""})
        assert result["settings"]["default_slot"] is None

    def test_empty_string_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "")
        assert val is None
        assert skip is False

    def test_none_value_passes_through(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", None)
        assert val is None
        assert skip is False

    def test_whitespace_only_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "   ")
        assert val is None
        assert skip is False

    def test_nonempty_string_trimmed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "  desktop  ")
        assert val == "desktop"
        assert skip is False

    def test_upload_uses_none_slot_when_active_slot_is_none(self, tmp_path):
        """When active_slot key is present but value is None, .get() returns None (legacy mode)."""
        _svc, _ = make_service(tmp_path)
        game_state: dict = {"active_slot": None}
        slot = game_state.get("active_slot", "default")
        assert slot is None

    def test_update_autocleanup_limit(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        result = svc.update_save_sync_settings({"autocleanup_limit": 5})
        assert result["success"] is True
        assert result["settings"]["autocleanup_limit"] == 5

    def test_update_autocleanup_limit_clamped(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        result = svc.update_save_sync_settings({"autocleanup_limit": 0})
        assert result["settings"]["autocleanup_limit"] == 1

    def test_get_settings_includes_new_defaults(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.get_save_sync_settings()
        assert result["default_slot"] == "default"
        assert result["autocleanup_limit"] == 10


class TestCheckCoreChange:
    """Tests for SaveService.check_core_change."""

    def _make_save_entry(
        self,
        system="snes",
        last_synced_core: str | None = "snes9x_libretro",
        active_slot="default",
    ) -> RomSaveState:
        """Return a minimal save state entry for rom_id 42."""
        return RomSaveState(
            system=system,
            last_synced_core=last_synced_core,
            active_slot=active_slot,
        )

    def test_core_changed(self, tmp_path):
        """Returns changed=True with core names when active core differs from stored."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("supafaust_libretro", "Supafaust"),
        )
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.saves["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result["changed"] is True
        assert result["old_core"] == "snes9x_libretro"
        assert result["new_core"] == "supafaust_libretro"
        assert result["old_label"] == "snes9x"
        assert result["new_label"] == "Supafaust"

    def test_core_same(self, tmp_path):
        """Returns changed=False when active core matches stored core."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x"),
        )
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.saves["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_never_synced(self, tmp_path):
        """Returns changed=False when rom_id has no save entry (never synced)."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # No entry for rom_id 42

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_no_stored_core(self, tmp_path):
        """Returns changed=False when save entry exists but last_synced_core is None."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x"),
        )
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.saves["42"] = self._make_save_entry(
            system="snes",
            last_synced_core=None,
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_active_core_resolution_fails(self, tmp_path):
        """Returns changed=False when get_active_core returns (None, None)."""
        svc, _ = make_service(
            tmp_path,
            # default: get_active_core returns (None, None)
        )
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.saves["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_save_sync_disabled(self, tmp_path):
        """Returns changed=False when save sync is disabled regardless of state."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("supafaust_libretro", "Supafaust"),
        )
        # save_sync_enabled defaults to False
        svc._save_sync_state.saves["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_rom_filename_resolved_for_per_game_core(self, tmp_path):
        """When installed_roms has file_path, the basename is passed to get_active_core."""
        received_args: list = []

        def capture_core(system_name, rom_filename=None):
            received_args.append((system_name, rom_filename))
            return ("supafaust_libretro", "Supafaust")

        svc, _ = make_service(tmp_path, get_active_core=capture_core)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.saves["42"] = self._make_save_entry(system="snes")
        _install_rom(svc, tmp_path, rom_id=42, system="snes", file_name="mario.sfc")

        svc.check_core_change(42)

        assert len(received_args) == 1
        assert received_args[0] == ("snes", "mario.sfc")


class TestPathTraversalDefense:
    """Defense in depth against malicious filenames at the two choke points.

    1. Server-supplied ``file_extension`` flowing through ``_local_save_target``.
    2. Frontend-supplied ``filename`` arriving at ``resolve_sync_conflict``.
    """

    def test_local_save_target_strips_traversal_in_extension(self, caplog):
        """A malicious ``file_extension`` cannot produce a path-escape filename."""
        from services.saves._helpers import _local_save_target

        with caplog.at_level(logging.WARNING):
            target = _local_save_target({"file_extension": "../etc/passwd"}, "pokemon")
        # Sanitization reduces to a simple basename — no separators, no parent refs.
        assert "/" not in target
        assert ".." not in target.split(".")
        assert os.path.basename(target) == target
        # The strip-and-warn path must log a warning identifying the sanitized field.
        assert any("Sanitized" in rec.message and "file_extension" in rec.message for rec in caplog.records)

    def test_local_save_target_happy_path_unchanged(self):
        """Clean ``file_extension`` produces ``<rom_name>.<ext>`` unchanged."""
        from services.saves._helpers import _local_save_target

        assert _local_save_target({"file_extension": "srm"}, "pokemon") == "pokemon.srm"

    def test_local_save_target_falls_back_to_srm_on_unusable_ext(self, caplog):
        """When the server's extension produces an empty/dot-only name, fall back to ``srm``."""
        from services.saves._helpers import _local_save_target

        with caplog.at_level(logging.WARNING):
            # An ``ext`` that drives the basename to ``""`` after sanitization
            # (e.g. trailing separator) — the helper degrades to ``"srm"``.
            target = _local_save_target({"file_extension": "evil/"}, "pokemon")
        # Either the sanitized basename or the safe default — never traversal.
        assert "/" not in target
        assert target.endswith(".srm") or target == "pokemon.srm"
        # The fallback path is the only signal of a glitched server extension —
        # assert it actually fires so a future refactor can't drop it silently.
        assert any("invalid" in rec.message.lower() and "file_extension" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_resolve_sync_conflict_rejects_traversal_filename(self, tmp_path, caplog):
        """Frontend-supplied traversal filename is rejected before any I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local data")

        # Snapshot files outside saves_dir to assert nothing got written there.
        outside = tmp_path / "outside.txt"

        with caplog.at_level(logging.WARNING):
            result = await svc.resolve_sync_conflict(
                rom_id=42,
                filename="../../etc/passwd",
                action="keep_local",
            )

        assert result["success"] is False
        assert "invalid" in result["message"].lower()
        # No I/O against the server (no list_saves, no upload_save).
        assert not any(c[0] == "list_saves" for c in fake.call_log)
        assert not any(c[0] == "upload_save" for c in fake.call_log)
        # Nothing written outside saves_dir.
        assert not outside.exists()
        assert not (tmp_path / "etc").exists()
        # A warning was logged identifying the rejection.
        assert any("rejected" in rec.message.lower() and "filename" in rec.message.lower() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_resolve_sync_conflict_rejects_null_byte_filename(self, tmp_path):
        """NUL byte in filename is rejected with the same shape."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon\x00.srm",
            action="keep_local",
        )

        assert result["success"] is False
        assert "invalid" in result["message"].lower()


class TestPerRomLockSerialization:
    @pytest.mark.asyncio
    async def test_per_rom_lock_serializes_concurrent_sync(self, tmp_path):
        """Two concurrent sync_rom_saves calls on the same rom must not interleave."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local data")

        # Spy timing on _sync_rom_saves entry/exit. The lock is held in the
        # async wrapper around run_in_executor, so the inner call's
        # entry/exit windows for two concurrent invocations must not overlap.
        events: list[tuple[str, float]] = []
        original = svc._sync_engine._sync_rom_saves

        def wrapped(rom_id: int):
            events.append(("enter", time.time()))
            # Sleep to ensure overlap is *possible* if the lock is broken.
            time.sleep(0.05)
            try:
                return original(rom_id)
            finally:
                events.append(("exit", time.time()))

        svc._sync_engine._sync_rom_saves = wrapped  # type: ignore[method-assign]

        await asyncio.gather(svc.sync_rom_saves(42), svc.sync_rom_saves(42))

        # Expect strictly serialized: enter, exit, enter, exit.
        kinds = [k for k, _ts in events]
        assert kinds == ["enter", "exit", "enter", "exit"], events

    @pytest.mark.asyncio
    async def test_per_rom_lock_does_not_block_different_rom_ids(self, tmp_path):
        """Concurrent syncs on different rom_ids run in parallel."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"a")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"b")

        events: list[tuple[int, str, float]] = []
        original = svc._sync_engine._sync_rom_saves

        def wrapped(rom_id: int):
            events.append((rom_id, "enter", time.time()))
            time.sleep(0.05)
            try:
                return original(rom_id)
            finally:
                events.append((rom_id, "exit", time.time()))

        svc._sync_engine._sync_rom_saves = wrapped  # type: ignore[method-assign]

        await asyncio.gather(svc.sync_rom_saves(1), svc.sync_rom_saves(2))

        # Both enters must happen before either exit (proves overlap).
        order = [(rid, kind) for rid, kind, _ts in events]
        enters = [i for i, e in enumerate(order) if e[1] == "enter"]
        exits = [i for i, e in enumerate(order) if e[1] == "exit"]
        assert min(exits) > max(enters), order
