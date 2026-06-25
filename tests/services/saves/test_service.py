"""Tests for SaveService aggregate root — public callable surface and cross-service coordination."""

import asyncio
import logging
import os
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_plugin_metadata_reader import FakePluginMetadataReader
from fakes.fake_save_api import FakeSaveApi

from domain.rom_save_state import FileSyncState, RomSaveState
from lib.errors import RommConnectionError
from services.saves._settings import sanitize_setting
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _get_device_id,
    _get_save_state,
    _install_rom,
    _seed_save_state,
    _set_device_id,
    make_service,
)


class TestDeviceRegistration:
    @pytest.mark.asyncio
    async def test_registers_new_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result["device_id"]
        assert result["device_name"]
        # Persisted
        assert _get_device_id(svc) == result["device_id"]

    @pytest.mark.asyncio
    async def test_returns_existing_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "existing")
        svc._config.settings["device_name"] = "deck"

        result = await svc.ensure_device_registered()
        assert result["device_id"] == "existing"
        assert result["device_name"] == "deck"
        assert result["server_device_id"] == "existing"

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
        svc._config.settings["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert _get_device_id(svc) == result["server_device_id"]
        # Verify register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1
        assert reg_calls[0][1][0]  # name (hostname)
        assert reg_calls[0][1][1] == "linux"  # platform
        assert reg_calls[0][1][2] == "decky-romm-sync"  # client

    @pytest.mark.asyncio
    async def test_returns_failure_on_server_error(self, tmp_path):
        """If register_device fails with a reachability error, returns the classified failure."""
        fake = FakeSaveApi()
        fake.set_version("4.8.1")  # skip the pre-register heartbeat probe
        fake.fail_on_next(RommConnectionError("server error"))
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._config.settings["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert _get_device_id(svc) is None

    @pytest.mark.asyncio
    async def test_returns_existing_with_server_device_id(self, tmp_path):
        """If already registered, returns existing IDs including server_device_id."""
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "server-id-123")
        svc._config.settings["device_name"] = "deck"

        result = await svc.ensure_device_registered()
        assert result["device_id"] == "server-id-123"
        assert result.get("server_device_id") == "server-id-123"

    @pytest.mark.asyncio
    async def test_upgrades_local_uuid_to_server(self, tmp_path):
        """A missing device id triggers a fresh server registration."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No persisted device id yet (e.g. from a failed registration)
        svc._config.settings["device_name"] = "deck"

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert _get_device_id(svc) is not None
        # register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1

    @pytest.mark.asyncio
    async def test_ensure_device_registered_reconciles_client_version(self, tmp_path):
        """Already-registered path calls update_device with current plugin_version."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "server-abc")
        svc._config.settings["device_name"] = "deck"

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "server-abc")
        svc._config.settings["device_name"] = "deck"

        # Make update_device fail silently
        fake.fail_on_next(Exception("network error"))
        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "server-abc"

    @pytest.mark.asyncio
    async def test_probes_version_when_unset(self, tmp_path):
        """ensure_device_registered probes the version when adapter has none."""

        class VersionedFakeApi(FakeSaveApi):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_calls = 0

            def heartbeat(self) -> dict[str, Any]:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._config.settings["save_sync_enabled"] = True

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

            def heartbeat(self) -> dict[str, Any]:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        fake.set_version("4.8.1")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._config.settings["save_sync_enabled"] = True

        await svc.ensure_device_registered()

        assert fake.heartbeat_calls == 0
        assert fake.get_version() == "4.8.1"

    @pytest.mark.asyncio
    async def test_probe_failure_is_non_fatal(self, tmp_path):
        """Heartbeat failure during version probe does not prevent registration."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = ConnectionError("offline")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._config.settings["save_sync_enabled"] = True

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

            def heartbeat(self) -> dict[str, Any]:
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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")

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

        assert result["disabled"] is True
        assert result["reason"] == "sync_disabled"

    @pytest.mark.asyncio
    async def test_list_devices_adapter_error(self, tmp_path):
        """Adapter raises — returns error response."""
        fake = FakeSaveApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._config.settings["save_sync_enabled"] = True

        fake.fail_on_next(Exception("server unavailable"))
        result = await svc.list_devices()

        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

    @pytest.mark.asyncio
    async def test_list_devices_no_own_id_all_false(self, tmp_path):
        """No server_device_id in state — all is_current_device are False."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device id persisted (kv_config empty by default)

        fake._registered_devices = [{"id": "device-1", "name": "steamdeck"}]
        result = await svc.list_devices()

        assert result["success"] is True
        assert result["devices"][0]["is_current_device"] is False

    @pytest.mark.asyncio
    async def test_list_devices_handles_null_id(self, tmp_path):
        """Device with id=None must not match own_id=None (avoid 'None'=='None' trap)."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device id persisted (kv_config empty by default)

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_post_exit_sync_skips_when_retrodeck_migration_pending(self, tmp_path):
        svc, _ = make_service(tmp_path, is_retrodeck_migration_pending=lambda: True)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
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
        the internal do_sync_rom_saves call path is never reached when migration
        is pending. Protects against accidental decorator removal at the public
        callable layer."""
        from main import Plugin

        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        plugin = Plugin()
        plugin._save_sync_service = svc
        plugin._migration_service = MagicMock()
        plugin._migration_service.is_retrodeck_migration_pending.return_value = True

        spy = MagicMock(name="do_sync_rom_saves_spy")
        svc._sync_engine.do_sync_rom_saves = spy  # type: ignore[method-assign]

        result = await plugin.sync_all_saves()

        assert result["blocked_by_migration"] is True
        assert result["success"] is False
        spy.assert_not_called()


class TestPostExitSyncConnectivity:
    @pytest.mark.asyncio
    async def test_returns_offline_when_heartbeat_fails(self, tmp_path):
        """post_exit_sync returns offline=True when the server is genuinely unreachable."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = RommConnectionError("unreachable")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result.get("offline") is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_proceeds_when_heartbeat_succeeds(self, tmp_path):
        """post_exit_sync proceeds normally when server is reachable."""
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"save data")

        result = await svc.post_exit_sync(42)

        assert result.get("offline") is not True
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_offline_skips_before_device_registration(self, tmp_path):
        """post_exit_sync returns offline without attempting device registration."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = RommConnectionError("connection refused")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id — would trigger registration if heartbeat passed

        result = await svc.post_exit_sync(42)

        assert result.get("offline") is True
        # Device should not have been registered
        assert not _get_device_id(svc)


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

        _seed_save_state(
            svc, 42, RomSaveState(files={"pokemon.srm": FileSyncState(tracked_save_id=1, last_sync_hash="abc")})
        )

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()
        # Entry survives — only files are cleared.
        entry = _get_save_state(svc, 42)
        assert entry is not None
        assert entry.files == {}

    @pytest.mark.asyncio
    async def test_delete_local_saves_preserves_slot_config(self, tmp_path):
        """Slot config and attribution metadata survive a delete (#279)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        assert save_path.exists()

        _seed_save_state(
            svc,
            42,
            RomSaveState(
                files={"pokemon.srm": FileSyncState(last_sync_hash="abc")},
                active_slot="desktop",
                slot_confirmed=True,
                emulator="retroarch-mgba",
                last_synced_core="mgba_libretro",
                own_upload_ids=[1, 2],
                slots={"default": {}, "desktop": {}},
                system="gba",
            ),
        )

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()

        entry = _get_save_state(svc, 42)
        assert entry is not None
        assert entry.files == {}
        assert entry.active_slot == "desktop"
        assert entry.slot_confirmed is True
        assert entry.emulator == "retroarch-mgba"
        assert entry.last_synced_core == "mgba_libretro"
        assert entry.own_upload_ids == [1, 2]
        assert entry.slots == {"default": {}, "desktop": {}}
        assert entry.system == "gba"

    @pytest.mark.asyncio
    async def test_delete_local_saves_no_prior_state_entry(self, tmp_path):
        """Delete on a ROM with no prior saves entry creates a stable empty entry."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # No save state entry for rom 42 yet.
        assert _get_save_state(svc, 42) is None

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()
        # The delete creates a stable empty entry with files={}.
        entry = _get_save_state(svc, 42)
        assert entry is not None
        assert entry.files == {}

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
            active_core=FakeActiveCoreResolver(default=("mgba_libretro", "mGBA")),
        )
        svc._config.settings["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._sync_engine.do_upload_save(
            42,
            str(tmp_path / "saves" / "gba" / "pokemon.srm"),
            "pokemon.srm",
            RomSaveState(),
            None,
            "gba",
            "mgba_libretro",
        )

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        _name, args, _kwargs = upload_calls[0]
        assert args[2] == "retroarch-mgba"  # emulator argument

    def test_upload_uses_fallback_when_no_core(self, tmp_path):
        """When the resolved core is None, upload falls back to 'retroarch'."""
        svc, fake = make_service(tmp_path)  # default: active_core returns (None, None)
        svc._config.settings["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._sync_engine.do_upload_save(
            42,
            str(tmp_path / "saves" / "gba" / "pokemon.srm"),
            "pokemon.srm",
            RomSaveState(),
            None,
            "gba",
            None,
        )

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        _name, args, _kwargs = upload_calls[0]
        assert args[2] == "retroarch"

    def test_resolve_core_differs_by_per_game_override(self, tmp_path):
        """RESULT-FLIP: two installed gba ROMs, one pinned + one default, stamp different cores.

        ``SyncEngine.resolve_core`` feeds the upload emulator tag. The pinned ROM
        resolves to its override core; the NULL ROM resolves to the system default.
        The stamped core flips on the override alone — keyed by rom_id, never by a
        per-call filename argument.
        """
        active_core = FakeActiveCoreResolver(
            default=("snes9x_libretro", "Snes9x"),
            per_rom={42: ("supafaust_libretro", "Supafaust")},
        )
        svc, _ = make_service(tmp_path, active_core=active_core)
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pinned.gba")
        _install_rom(svc, tmp_path, rom_id=43, system="gba", file_name="default.gba")

        assert svc._sync_engine.resolve_core(42) == "supafaust_libretro"
        assert svc._sync_engine.resolve_core(43) == "snes9x_libretro"
        assert active_core.calls == [42, 43]

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

        _seed_save_state(
            svc,
            1,
            RomSaveState(
                files={"game1.srm": FileSyncState(last_sync_hash="h")},
                active_slot="desktop",
                slot_confirmed=True,
                emulator="retroarch-mgba",
                system="gba",
            ),
        )
        _seed_save_state(
            svc,
            2,
            RomSaveState(
                files={"game2.srm": FileSyncState(last_sync_hash="h")},
                active_slot="default",
                slot_confirmed=True,
                own_upload_ids=[99],
                system="gba",
            ),
        )

        result = svc.delete_platform_saves("gba")
        assert result["success"] is True
        assert result["deleted_count"] == 2

        entry1 = _get_save_state(svc, 1)
        assert entry1 is not None
        assert entry1.files == {}
        assert entry1.active_slot == "desktop"
        assert entry1.slot_confirmed is True
        assert entry1.emulator == "retroarch-mgba"

        entry2 = _get_save_state(svc, 2)
        assert entry2 is not None
        assert entry2.files == {}
        assert entry2.active_slot == "default"
        assert entry2.slot_confirmed is True
        assert entry2.own_upload_ids == [99]

    @pytest.mark.asyncio
    async def test_delete_platform_saves_other_platform_untouched(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1")
        snes_save = _create_save(tmp_path, system="snes", rom_name="game2")

        _seed_save_state(
            svc,
            2,
            RomSaveState(
                files={"game2.srm": FileSyncState(last_sync_hash="h")},
                active_slot="default",
                slot_confirmed=True,
                system="snes",
            ),
            platform_slug="snes",
        )

        svc.delete_platform_saves("gba")
        assert snes_save.exists()
        # Other-platform entry must be entirely untouched.
        snes_entry = _get_save_state(svc, 2)
        assert snes_entry is not None
        assert "game2.srm" in snes_entry.files
        assert snes_entry.active_slot == "default"
        assert snes_entry.slot_confirmed is True


class TestSaveSyncSettingsSlotAndCleanup:
    """Tests for default_slot and autocleanup_limit settings."""

    def test_update_default_slot(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        result = svc.update_save_sync_settings({"default_slot": "desktop"})
        assert result["success"] is True
        assert result["settings"]["default_slot"] == "desktop"

    def test_update_default_slot_empty_string_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        svc._config.settings["default_slot"] = "default"
        result = svc.update_save_sync_settings({"default_slot": ""})
        assert result["settings"]["default_slot"] is None

    def test_empty_string_becomes_none(self, tmp_path):
        val, skip = sanitize_setting("default_slot", "")
        assert val is None
        assert skip is False

    def test_none_value_passes_through(self, tmp_path):
        val, skip = sanitize_setting("default_slot", None)
        assert val is None
        assert skip is False

    def test_whitespace_only_becomes_none(self, tmp_path):
        val, skip = sanitize_setting("default_slot", "   ")
        assert val is None
        assert skip is False

    def test_nonempty_string_trimmed(self, tmp_path):
        val, skip = sanitize_setting("default_slot", "  desktop  ")
        assert val == "desktop"
        assert skip is False

    def test_upload_uses_none_slot_when_active_slot_is_none(self, tmp_path):
        """When active_slot key is present but value is None, .get() returns None (legacy mode)."""
        _svc, _ = make_service(tmp_path)
        game_state: dict[str, Any] = {"active_slot": None}
        slot = game_state.get("active_slot", "default")
        assert slot is None

    def test_update_autocleanup_limit(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        result = svc.update_save_sync_settings({"autocleanup_limit": 5})
        assert result["success"] is True
        assert result["settings"]["autocleanup_limit"] == 5

    def test_update_autocleanup_limit_clamped(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
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
            active_core=FakeActiveCoreResolver(default=("supafaust_libretro", "Supafaust")),
        )
        svc._config.settings["save_sync_enabled"] = True
        _seed_save_state(
            svc,
            42,
            self._make_save_entry(
                system="snes",
                last_synced_core="snes9x_libretro",
            ),
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
            active_core=FakeActiveCoreResolver(default=("snes9x_libretro", "Snes9x")),
        )
        svc._config.settings["save_sync_enabled"] = True
        _seed_save_state(
            svc,
            42,
            self._make_save_entry(
                system="snes",
                last_synced_core="snes9x_libretro",
            ),
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_never_synced(self, tmp_path):
        """Returns changed=False when rom_id has no save entry (never synced)."""
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No entry for rom_id 42

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_no_stored_core(self, tmp_path):
        """Returns changed=False when save entry exists but last_synced_core is None."""
        svc, _ = make_service(
            tmp_path,
            active_core=FakeActiveCoreResolver(default=("snes9x_libretro", "Snes9x")),
        )
        svc._config.settings["save_sync_enabled"] = True
        _seed_save_state(
            svc,
            42,
            self._make_save_entry(
                system="snes",
                last_synced_core=None,
            ),
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_active_core_resolution_fails(self, tmp_path):
        """Returns changed=False when the resolver returns (None, None)."""
        svc, _ = make_service(
            tmp_path,
            # default: active_core returns (None, None)
        )
        svc._config.settings["save_sync_enabled"] = True
        _seed_save_state(
            svc,
            42,
            self._make_save_entry(
                system="snes",
                last_synced_core="snes9x_libretro",
            ),
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_save_sync_disabled(self, tmp_path):
        """Returns changed=False when save sync is disabled regardless of state."""
        svc, _ = make_service(
            tmp_path,
            active_core=FakeActiveCoreResolver(default=("supafaust_libretro", "Supafaust")),
        )
        # save_sync_enabled defaults to False
        _seed_save_state(
            svc,
            42,
            self._make_save_entry(
                system="snes",
                last_synced_core="snes9x_libretro",
            ),
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_check_core_change_differs_by_per_game_override(self, tmp_path):
        """RESULT-FLIP: two ROMs on one platform, one pinned + one NULL, differ by override.

        The pinned ROM resolves to the override core (≠ its stored core) → changed;
        the NULL ROM resolves to the same system default it was synced with →
        unchanged. The per-rom seam is keyed by ``rom_id`` — the outcome flips on
        the override alone, not on any per-call argument.
        """
        # rom 42: override resolves to a DIFFERENT core than what was synced.
        # rom 43: default resolves to the SAME core it was synced with.
        active_core = FakeActiveCoreResolver(
            default=("snes9x_libretro", "Snes9x"),
            per_rom={42: ("supafaust_libretro", "Supafaust")},
        )
        svc, _ = make_service(tmp_path, active_core=active_core)
        svc._config.settings["save_sync_enabled"] = True
        _seed_save_state(
            svc,
            42,
            self._make_save_entry(system="snes", last_synced_core="snes9x_libretro"),
            platform_slug="snes",
        )
        _seed_save_state(
            svc,
            43,
            self._make_save_entry(system="snes", last_synced_core="snes9x_libretro"),
            platform_slug="snes",
        )

        pinned = svc.check_core_change(42)
        plain = svc.check_core_change(43)

        # The override flips the outcome: pinned ROM sees a core change, NULL does not.
        assert pinned["changed"] is True
        assert pinned["new_core"] == "supafaust_libretro"
        assert plain == {"changed": False}
        assert active_core.calls == [42, 43]


class TestPathTraversalDefense:
    """Defense in depth against malicious filenames at the two choke points.

    1. Server-supplied ``file_extension`` flowing through ``local_save_target``.
    2. Frontend-supplied ``filename`` arriving at ``resolve_sync_conflict``.
    """

    def test_local_save_target_strips_traversal_in_extension(self, caplog):
        """A malicious ``file_extension`` cannot produce a path-escape filename."""
        from services.saves._helpers import local_save_target

        with caplog.at_level(logging.WARNING):
            target = local_save_target({"file_extension": "../etc/passwd"}, "pokemon")
        # Sanitization reduces to a simple basename — no separators, no parent refs.
        assert "/" not in target
        assert ".." not in target.split(".")
        assert os.path.basename(target) == target
        # The strip-and-warn path must log a warning identifying the sanitized field.
        assert any("Sanitized" in rec.message and "file_extension" in rec.message for rec in caplog.records)

    def test_local_save_target_happy_path_unchanged(self):
        """Clean ``file_extension`` produces ``<rom_name>.<ext>`` unchanged."""
        from services.saves._helpers import local_save_target

        assert local_save_target({"file_extension": "srm"}, "pokemon") == "pokemon.srm"

    def test_local_save_target_falls_back_to_srm_on_unusable_ext(self, caplog):
        """When the server's extension produces an empty/dot-only name, fall back to ``srm``."""
        from services.saves._helpers import local_save_target

        with caplog.at_level(logging.WARNING):
            # An ``ext`` that drives the basename to ``""`` after sanitization
            # (e.g. trailing separator) — the helper degrades to ``"srm"``.
            target = local_save_target({"file_extension": "evil/"}, "pokemon")
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
                server_save_id=100,
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
            server_save_id=100,
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

        # Spy timing on do_sync_rom_saves entry/exit. The lock is held in the
        # async wrapper around run_in_executor, so the inner call's
        # entry/exit windows for two concurrent invocations must not overlap.
        events: list[tuple[str, float]] = []
        original = svc._sync_engine.do_sync_rom_saves

        def wrapped(rom_id: int, *args):
            events.append(("enter", time.time()))
            # Sleep to ensure overlap is *possible* if the lock is broken.
            time.sleep(0.05)
            try:
                return original(rom_id, *args)
            finally:
                events.append(("exit", time.time()))

        svc._sync_engine.do_sync_rom_saves = wrapped  # type: ignore[method-assign]

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
        original = svc._sync_engine.do_sync_rom_saves

        def wrapped(rom_id: int, *args):
            events.append((rom_id, "enter", time.time()))
            time.sleep(0.05)
            try:
                return original(rom_id, *args)
            finally:
                events.append((rom_id, "exit", time.time()))

        svc._sync_engine.do_sync_rom_saves = wrapped  # type: ignore[method-assign]

        await asyncio.gather(svc.sync_rom_saves(1), svc.sync_rom_saves(2))

        # Both enters must happen before either exit (proves overlap).
        order = [(rid, kind) for rid, kind, _ts in events]
        enters = [i for i, e in enumerate(order) if e[1] == "enter"]
        exits = [i for i, e in enumerate(order) if e[1] == "exit"]
        assert min(exits) > max(enters), order


class TestHasTrackedSave:
    """Pure in-memory predicate consumed by the launch gate."""

    def test_returns_false_when_no_entry(self, tmp_path):
        """ROM with no entry in state.saves → False."""
        svc, _ = make_service(tmp_path)
        assert svc.has_tracked_save(42) is False

    def test_returns_false_for_empty_entry(self, tmp_path):
        """ROM with an empty RomSaveState (no files, no slots) → False."""
        svc, _ = make_service(tmp_path)
        _seed_save_state(svc, 42, RomSaveState())
        assert svc.has_tracked_save(42) is False

    def test_returns_true_when_files_tracked(self, tmp_path):
        """ROM with at least one tracked file → True."""
        svc, _ = make_service(tmp_path)
        _seed_save_state(
            svc,
            42,
            RomSaveState(files={"pokemon.srm": FileSyncState(tracked_save_id=7, last_sync_hash="abc")}),
        )
        assert svc.has_tracked_save(42) is True

    def test_returns_true_when_slots_configured(self, tmp_path):
        """ROM with at least one slot configured (no files yet) → True."""
        svc, _ = make_service(tmp_path)
        _seed_save_state(svc, 42, RomSaveState(slots={"default": {"label": "Default"}}))
        assert svc.has_tracked_save(42) is True

    def test_accepts_int_rom_id_casting_to_str_key(self, tmp_path):
        """``rom_id`` is int on the wire; the aggregate is keyed by int rom_id."""
        svc, _ = make_service(tmp_path)
        _seed_save_state(svc, 99, RomSaveState(files={"a.srm": FileSyncState(tracked_save_id=1, last_sync_hash="h")}))
        assert svc.has_tracked_save(99) is True
        # Wrong rom_id misses cleanly.
        assert svc.has_tracked_save(100) is False


class TestBadPathDeleteSavesPartialFailure:
    """Coverage for the per-file ``except`` arm in ``_delete_saves_for_roms``.

    Wires a ``FakeSaveFileStore`` into the service post-construction so
    one targeted ``remove`` call raises ``OSError`` while the rest succeed.
    """

    @staticmethod
    def _install_fake_save_file(svc, files: dict[str, bytes], remove_failures: set[str]):
        """Replace the real ``SaveFileAdapter`` with a fake on both consumers.

        The aggregate root and ``RomInfoService`` both hold the adapter
        reference — both must point at the same fake so file discovery
        and deletion run through the failure-injecting instance.
        """
        from fakes.fake_save_file_store import FakeSaveFileStore

        fake = FakeSaveFileStore(files=files)
        # Mark each saves-dir as present so ``find_save_files`` walks them.
        for path in files:
            fake.dirs.add(os.path.dirname(path))
        fake.remove_failures = set(remove_failures)
        svc._save_file_store = fake
        svc._rom_info._save_file_store = fake
        return fake

    @pytest.mark.asyncio
    async def test_delete_local_saves_partial_failure_returns_error_response(self, tmp_path):
        """One ``remove`` failure flips success=False but counts the rest."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        saves_dir = str(tmp_path / "saves" / "gba")
        good_path = os.path.join(saves_dir, "pokemon.srm")
        bad_path = os.path.join(saves_dir, "pokemon.rtc")
        fake = self._install_fake_save_file(
            svc,
            files={good_path: b"\x00" * 16, bad_path: b"\x01" * 16},
            remove_failures={bad_path},
        )

        result = svc.delete_local_saves(42)

        assert result["success"] is False
        # The successful remove still counts.
        assert result["deleted_count"] == 1
        assert "1 error(s)" in result["message"]
        # The failing path remains; the successful one is gone.
        assert bad_path in fake.files
        assert good_path not in fake.files

    @pytest.mark.asyncio
    async def test_delete_platform_saves_partial_failure_returns_error_response(self, tmp_path):
        """One ``remove`` failure across the platform flips success=False."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="gba", file_name="game2.gba")

        saves_dir = str(tmp_path / "saves" / "gba")
        good_path = os.path.join(saves_dir, "game1.srm")
        bad_path = os.path.join(saves_dir, "game2.srm")
        fake = self._install_fake_save_file(
            svc,
            files={good_path: b"\x00" * 16, bad_path: b"\x01" * 16},
            remove_failures={bad_path},
        )

        result = svc.delete_platform_saves("gba")

        assert result["success"] is False
        assert result["deleted_count"] == 1
        assert "1 error(s)" in result["message"]
        # The failing file remains in place; the successful one is gone.
        assert bad_path in fake.files
        assert good_path not in fake.files


class TestPluginVersionResolution:
    """SaveService.__init__ resolves the plugin version exactly once."""

    def test_reads_plugin_version_once_with_injected_plugin_dir(self, tmp_path):
        """One read at construction, scoped to the injected plugin_dir."""
        fake_reader = FakePluginMetadataReader(version="0.14.0")
        plugin_dir = str(tmp_path / "custom-plugin-dir")

        make_service(tmp_path, plugin_metadata=fake_reader, plugin_dir=plugin_dir)

        assert fake_reader.read_count == 1
        assert fake_reader.last_plugin_dir == plugin_dir
