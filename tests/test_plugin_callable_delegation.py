"""Coverage for ``main.Plugin`` callable delegation seams.

Every callable on ``Plugin`` is a thin one-liner that forwards to a
backend service. These tests exercise each delegation by:

- attaching a ``MagicMock`` service to the bare ``Plugin``,
- invoking the callable,
- asserting the service method was called with the expected args,
- asserting the return value is propagated.

For a handful of representative callables we also assert that
exceptions raised by the underlying service propagate through the
callable (no swallowed errors). These tests are aimed at line coverage
of the delegation surface — they intentionally do NOT exercise the
underlying service logic, which is covered elsewhere.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import _make_testable_plugin


@pytest.fixture
def plugin():
    """Bare ``Plugin`` with every service replaced by a ``MagicMock``.

    ``_migration_service.is_retrodeck_migration_pending`` returns False
    so ``@migration_blocked`` callables fall through to the wrapped
    method instead of returning the blocked sentinel.
    """
    p = _make_testable_plugin()
    p._sync_service = MagicMock()
    p._download_service = MagicMock()
    p._rom_removal_service = MagicMock()
    p._firmware_service = MagicMock()
    p._sgdb_service = MagicMock()
    p._metadata_service = MagicMock()
    p._achievements_service = MagicMock()
    p._game_detail_service = MagicMock()
    p._artwork_service = MagicMock()
    p._shortcut_removal_service = MagicMock()
    p._settings_service = MagicMock()
    p._core_service = MagicMock()
    p._connection_service = MagicMock()
    p._save_sync_service = MagicMock()
    p._playtime_service = MagicMock()
    p._launch_gate_service = MagicMock()
    p._session_lifecycle_service = MagicMock()
    return p


# ── Settings / connection / log-level callables ───────────────────────


class TestSettingsCallableDelegation:
    @pytest.mark.asyncio
    async def test_save_settings_delegates(self, plugin):
        plugin._settings_service.save_settings.return_value = {"ok": True}
        result = await plugin.save_settings("http://x", "u", "p", True)
        plugin._settings_service.save_settings.assert_called_once_with(
            "http://x",
            "u",
            "p",
            True,
        )
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_frontend_log_delegates(self, plugin):
        await plugin.frontend_log("warn", "msg")
        plugin._settings_service.frontend_log.assert_called_once_with("warn", "msg")

    @pytest.mark.asyncio
    async def test_save_log_level_delegates(self, plugin):
        plugin._settings_service.save_log_level.return_value = {"success": True}
        result = await plugin.save_log_level("debug")
        plugin._settings_service.save_log_level.assert_called_once_with("debug")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_save_steam_input_setting_delegates(self, plugin):
        plugin._settings_service.save_steam_input_setting.return_value = {"ok": True}
        result = await plugin.save_steam_input_setting("default")
        plugin._settings_service.save_steam_input_setting.assert_called_once_with("default")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_apply_steam_input_setting_delegates(self, plugin):
        plugin._settings_service.apply_steam_input_setting.return_value = {"ok": True}
        result = await plugin.apply_steam_input_setting()
        plugin._settings_service.apply_steam_input_setting.assert_called_once_with()
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_fix_retroarch_input_driver_delegates(self, plugin):
        plugin._settings_service.fix_retroarch_input_driver.return_value = {"ok": True}
        result = await plugin.fix_retroarch_input_driver()
        plugin._settings_service.fix_retroarch_input_driver.assert_called_once_with()
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_get_settings_delegates(self, plugin):
        plugin._settings_service.get_settings.return_value = {"romm_url": "x"}
        result = await plugin.get_settings()
        plugin._settings_service.get_settings.assert_called_once_with()
        assert result == {"romm_url": "x"}

    @pytest.mark.asyncio
    async def test_get_whitelist_settings_delegates(self, plugin):
        plugin._settings_service.get_whitelist_settings.return_value = {"disabled_defaults": []}
        result = await plugin.get_whitelist_settings()
        plugin._settings_service.get_whitelist_settings.assert_called_once_with()
        assert result == {"disabled_defaults": []}

    @pytest.mark.asyncio
    async def test_update_whitelist_settings_delegates(self, plugin):
        plugin._settings_service.update_whitelist_settings.return_value = {"success": True}
        result = await plugin.update_whitelist_settings(["a"], ["b"])
        plugin._settings_service.update_whitelist_settings.assert_called_once_with(["a"], ["b"])
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_save_collection_platform_groups_delegates(self, plugin):
        plugin._settings_service.save_collection_platform_groups.return_value = {"ok": True}
        result = await plugin.save_collection_platform_groups(True)
        plugin._settings_service.save_collection_platform_groups.assert_called_once_with(True)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_debug_log_routes_through_frontend_log(self, plugin):
        await plugin.debug_log("hello")
        plugin._settings_service.frontend_log.assert_called_once_with("debug", "hello")


class TestConnectionCallableDelegation:
    @pytest.mark.asyncio
    async def test_test_connection_delegates(self, plugin):
        plugin._connection_service.test_connection = AsyncMock(return_value={"success": True})
        result = await plugin.test_connection()
        plugin._connection_service.test_connection.assert_awaited_once_with()
        assert result == {"success": True}


# ── Migration callables ────────────────────────────────────────────────


class TestMigrationCallableDelegation:
    @pytest.mark.asyncio
    async def test_migrate_retrodeck_files_delegates(self, plugin):
        plugin._migration_service.migrate_retrodeck_files = AsyncMock(return_value={"ok": True})
        result = await plugin.migrate_retrodeck_files("overwrite")
        plugin._migration_service.migrate_retrodeck_files.assert_awaited_once_with("overwrite")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_get_migration_status_delegates(self, plugin):
        plugin._migration_service.get_migration_status = AsyncMock(return_value={"pending": False})
        result = await plugin.get_migration_status()
        plugin._migration_service.get_migration_status.assert_awaited_once_with()
        assert result == {"pending": False}

    @pytest.mark.asyncio
    async def test_get_save_sort_migration_status_delegates(self, plugin):
        plugin._migration_service.get_save_sort_migration_status = AsyncMock(return_value={"pending": False})
        result = await plugin.get_save_sort_migration_status()
        plugin._migration_service.get_save_sort_migration_status.assert_awaited_once_with()
        assert result == {"pending": False}

    @pytest.mark.asyncio
    async def test_migrate_save_sort_files_delegates(self, plugin):
        plugin._migration_service.migrate_save_sort_files = AsyncMock(return_value={"ok": True})
        result = await plugin.migrate_save_sort_files("skip")
        plugin._migration_service.migrate_save_sort_files.assert_awaited_once_with("skip")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_dismiss_save_sort_migration_delegates(self, plugin):
        plugin._migration_service.dismiss_save_sort_migration.return_value = {"ok": True}
        result = await plugin.dismiss_save_sort_migration()
        plugin._migration_service.dismiss_save_sort_migration.assert_called_once_with()
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_dismiss_retrodeck_migration_delegates(self, plugin):
        plugin._migration_service.dismiss_retrodeck_migration.return_value = {"ok": True}
        result = await plugin.dismiss_retrodeck_migration()
        plugin._migration_service.dismiss_retrodeck_migration.assert_called_once_with()
        assert result == {"ok": True}


# ── Core / firmware / BIOS callables ───────────────────────────────────


class TestCoreCallableDelegation:
    @pytest.mark.asyncio
    async def test_set_system_core_delegates(self, plugin):
        plugin._core_service.set_system_core = AsyncMock(return_value={"success": True})
        result = await plugin.set_system_core("snes", "core_a")
        plugin._core_service.set_system_core.assert_awaited_once_with("snes", "core_a")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_set_game_core_delegates(self, plugin):
        plugin._core_service.set_game_core = AsyncMock(return_value={"success": True})
        result = await plugin.set_game_core("snes", "/path", "core_a")
        plugin._core_service.set_game_core.assert_awaited_once_with("snes", "/path", "core_a")
        assert result == {"success": True}


class TestFirmwareCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_firmware_status_delegates(self, plugin):
        plugin._firmware_service.get_firmware_status = AsyncMock(return_value={"items": []})
        result = await plugin.get_firmware_status()
        plugin._firmware_service.get_firmware_status.assert_awaited_once_with()
        assert result == {"items": []}

    @pytest.mark.asyncio
    async def test_download_all_firmware_delegates(self, plugin):
        plugin._firmware_service.download_all_firmware = AsyncMock(return_value={"success": True})
        result = await plugin.download_all_firmware("snes")
        plugin._firmware_service.download_all_firmware.assert_awaited_once_with("snes")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_download_required_firmware_delegates(self, plugin):
        plugin._firmware_service.download_required_firmware = AsyncMock(return_value={"success": True})
        result = await plugin.download_required_firmware("snes")
        plugin._firmware_service.download_required_firmware.assert_awaited_once_with("snes")
        assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_check_platform_bios_delegates(self, plugin):
        plugin._firmware_service.check_platform_bios = AsyncMock(return_value={"present": []})
        result = await plugin.check_platform_bios("snes", "rom.bin")
        plugin._firmware_service.check_platform_bios.assert_awaited_once_with(
            "snes",
            rom_filename="rom.bin",
        )
        assert result == {"present": []}

    @pytest.mark.asyncio
    async def test_get_bios_status_delegates(self, plugin):
        plugin._game_detail_service.get_bios_status = AsyncMock(return_value={"present": True})
        result = await plugin.get_bios_status(7)
        plugin._game_detail_service.get_bios_status.assert_awaited_once_with(7)
        assert result == {"present": True}

    @pytest.mark.asyncio
    async def test_delete_platform_bios_delegates(self, plugin):
        plugin._firmware_service.delete_platform_bios = AsyncMock(return_value={"removed": 0})
        result = await plugin.delete_platform_bios("snes")
        plugin._firmware_service.delete_platform_bios.assert_awaited_once_with("snes")
        assert result == {"removed": 0}


# ── Sync / library callables ───────────────────────────────────────────


class TestLibrarySyncCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_platforms_delegates(self, plugin):
        plugin._sync_service.get_platforms = AsyncMock(return_value=[])
        result = await plugin.get_platforms()
        plugin._sync_service.get_platforms.assert_awaited_once_with()
        assert result == []

    @pytest.mark.asyncio
    async def test_save_platform_sync_delegates(self, plugin):
        plugin._sync_service.save_platform_sync.return_value = {"ok": True}
        result = await plugin.save_platform_sync(1, True)
        plugin._sync_service.save_platform_sync.assert_called_once_with(1, True)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_set_all_platforms_sync_delegates(self, plugin):
        plugin._sync_service.set_all_platforms_sync = AsyncMock(return_value={"ok": True})
        result = await plugin.set_all_platforms_sync(True)
        plugin._sync_service.set_all_platforms_sync.assert_awaited_once_with(True)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_get_collections_delegates(self, plugin):
        plugin._sync_service.get_collections = AsyncMock(return_value=[])
        result = await plugin.get_collections()
        plugin._sync_service.get_collections.assert_awaited_once_with()
        assert result == []

    @pytest.mark.asyncio
    async def test_save_collection_sync_delegates(self, plugin):
        plugin._sync_service.save_collection_sync.return_value = {"ok": True}
        result = await plugin.save_collection_sync(2, False)
        plugin._sync_service.save_collection_sync.assert_called_once_with(2, False)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_set_all_collections_sync_delegates(self, plugin):
        plugin._sync_service.set_all_collections_sync = AsyncMock(return_value={"ok": True})
        result = await plugin.set_all_collections_sync(True, "favourites")
        plugin._sync_service.set_all_collections_sync.assert_awaited_once_with(True, "favourites")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_start_sync_delegates(self, plugin):
        plugin._sync_service.start_sync.return_value = {"started": True}
        result = await plugin.start_sync()
        plugin._sync_service.start_sync.assert_called_once_with()
        assert result == {"started": True}

    @pytest.mark.asyncio
    async def test_sync_heartbeat_delegates(self, plugin):
        plugin._sync_service.sync_heartbeat.return_value = {"alive": True}
        result = await plugin.sync_heartbeat()
        plugin._sync_service.sync_heartbeat.assert_called_once_with()
        assert result == {"alive": True}

    @pytest.mark.asyncio
    async def test_sync_preview_delegates(self, plugin):
        plugin._sync_service.sync_preview = AsyncMock(return_value={"preview_id": "abc"})
        result = await plugin.sync_preview()
        plugin._sync_service.sync_preview.assert_awaited_once_with()
        assert result == {"preview_id": "abc"}

    @pytest.mark.asyncio
    async def test_sync_apply_delta_delegates(self, plugin):
        plugin._sync_service.sync_apply_delta = AsyncMock(return_value={"applied": True})
        result = await plugin.sync_apply_delta("abc")
        plugin._sync_service.sync_apply_delta.assert_awaited_once_with("abc")
        assert result == {"applied": True}

    @pytest.mark.asyncio
    async def test_report_unit_results_delegates(self, plugin):
        plugin._sync_service.report_unit_results = AsyncMock(return_value={"ok": True})
        result = await plugin.report_unit_results({"1": 100})
        plugin._sync_service.report_unit_results.assert_awaited_once_with({"1": 100})
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_get_registry_platforms_delegates(self, plugin):
        plugin._sync_service.get_registry_platforms.return_value = [{"slug": "snes"}]
        result = await plugin.get_registry_platforms()
        plugin._sync_service.get_registry_platforms.assert_called_once_with()
        assert result == [{"slug": "snes"}]

    @pytest.mark.asyncio
    async def test_clear_sync_cache_delegates(self, plugin):
        plugin._sync_service.clear_sync_cache.return_value = {"ok": True}
        result = await plugin.clear_sync_cache()
        plugin._sync_service.clear_sync_cache.assert_called_once_with()
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_get_sync_stats_delegates(self, plugin):
        plugin._sync_service.get_sync_stats.return_value = {"roms": 5}
        result = await plugin.get_sync_stats()
        plugin._sync_service.get_sync_stats.assert_called_once_with()
        assert result == {"roms": 5}


class TestShortcutRemovalCallableDelegation:
    @pytest.mark.asyncio
    async def test_remove_platform_shortcuts_delegates(self, plugin):
        plugin._shortcut_removal_service.remove_platform_shortcuts = AsyncMock(return_value={"removed": 3})
        result = await plugin.remove_platform_shortcuts("snes")
        plugin._shortcut_removal_service.remove_platform_shortcuts.assert_awaited_once_with("snes")
        assert result == {"removed": 3}

    @pytest.mark.asyncio
    async def test_remove_all_shortcuts_delegates(self, plugin):
        plugin._shortcut_removal_service.remove_all_shortcuts.return_value = {"removed": 10}
        result = await plugin.remove_all_shortcuts()
        plugin._shortcut_removal_service.remove_all_shortcuts.assert_called_once_with()
        assert result == {"removed": 10}

    @pytest.mark.asyncio
    async def test_report_removal_results_delegates(self, plugin):
        plugin._shortcut_removal_service.report_removal_results = AsyncMock(return_value={"ok": True})
        result = await plugin.report_removal_results([1, 2])
        plugin._shortcut_removal_service.report_removal_results.assert_awaited_once_with([1, 2])
        assert result == {"ok": True}


class TestArtworkCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_artwork_base64_delegates(self, plugin):
        plugin._artwork_service.get_artwork_base64 = AsyncMock(return_value={"base64": None})
        result = await plugin.get_artwork_base64(42)
        plugin._artwork_service.get_artwork_base64.assert_awaited_once_with(42)
        assert result == {"base64": None}


# ── Launch / session lifecycle callables ───────────────────────────────


class TestLifecycleCallableDelegation:
    @pytest.mark.asyncio
    async def test_evaluate_launch_returns_asdict(self, plugin):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Verdict:
            allowed: bool
            reason: str

        plugin._launch_gate_service.evaluate = AsyncMock(return_value=Verdict(allowed=True, reason="ok"))
        result = await plugin.evaluate_launch(12345)
        plugin._launch_gate_service.evaluate.assert_awaited_once_with(12345)
        assert result == {"allowed": True, "reason": "ok"}

    @pytest.mark.asyncio
    async def test_finalize_game_session_returns_asdict(self, plugin):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Outcome:
            synced: bool

        plugin._session_lifecycle_service.finalize = AsyncMock(return_value=Outcome(synced=False))
        result = await plugin.finalize_game_session(7)
        plugin._session_lifecycle_service.finalize.assert_awaited_once_with(7)
        assert result == {"synced": False}


# ── Download callables ─────────────────────────────────────────────────


class TestDownloadCallableDelegation:
    @pytest.mark.asyncio
    async def test_start_download_delegates(self, plugin):
        plugin._download_service.start_download = AsyncMock(return_value={"queued": True})
        result = await plugin.start_download(42)
        plugin._download_service.start_download.assert_awaited_once_with(42)
        assert result == {"queued": True}

    @pytest.mark.asyncio
    async def test_cancel_download_delegates(self, plugin):
        plugin._download_service.cancel_download.return_value = {"cancelled": True}
        result = await plugin.cancel_download(42)
        plugin._download_service.cancel_download.assert_called_once_with(42)
        assert result == {"cancelled": True}

    @pytest.mark.asyncio
    async def test_get_download_queue_delegates(self, plugin):
        plugin._download_service.get_download_queue.return_value = []
        result = await plugin.get_download_queue()
        plugin._download_service.get_download_queue.assert_called_once_with()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_installed_rom_delegates(self, plugin):
        plugin._download_service.get_installed_rom.return_value = {"installed": True}
        result = await plugin.get_installed_rom(42)
        plugin._download_service.get_installed_rom.assert_called_once_with(42)
        assert result == {"installed": True}


class TestRomRemovalCallableDelegation:
    @pytest.mark.asyncio
    async def test_remove_rom_delegates(self, plugin):
        plugin._rom_removal_service.remove_rom = AsyncMock(return_value={"removed": True})
        result = await plugin.remove_rom(42)
        plugin._rom_removal_service.remove_rom.assert_awaited_once_with(42)
        assert result == {"removed": True}

    @pytest.mark.asyncio
    async def test_uninstall_all_roms_delegates(self, plugin):
        plugin._rom_removal_service.uninstall_all_roms = AsyncMock(return_value={"removed": 3})
        result = await plugin.uninstall_all_roms()
        plugin._rom_removal_service.uninstall_all_roms.assert_awaited_once_with()
        assert result == {"removed": 3}


# ── Saves callables ───────────────────────────────────────────────────


class TestSavesCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_save_status_delegates(self, plugin):
        plugin._save_sync_service.get_save_status = AsyncMock(return_value={"status": "ok"})
        result = await plugin.get_save_status(42)
        plugin._save_sync_service.get_save_status.assert_awaited_once_with(42)
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_check_core_change_delegates(self, plugin):
        plugin._save_sync_service.check_core_change.return_value = {"changed": False}
        result = await plugin.check_core_change(42)
        plugin._save_sync_service.check_core_change.assert_called_once_with(42)
        assert result == {"changed": False}

    @pytest.mark.asyncio
    async def test_get_save_slots_delegates(self, plugin):
        plugin._save_sync_service.get_save_slots = AsyncMock(return_value=["default"])
        result = await plugin.get_save_slots(42)
        plugin._save_sync_service.get_save_slots.assert_awaited_once_with(42)
        assert result == ["default"]

    @pytest.mark.asyncio
    async def test_get_slot_saves_delegates(self, plugin):
        plugin._save_sync_service.get_slot_saves = AsyncMock(return_value=[])
        result = await plugin.get_slot_saves(42, "default")
        plugin._save_sync_service.get_slot_saves.assert_awaited_once_with(42, "default")
        assert result == []

    @pytest.mark.asyncio
    async def test_switch_slot_delegates(self, plugin):
        plugin._save_sync_service.switch_slot = AsyncMock(return_value={"ok": True})
        result = await plugin.switch_slot(42, "slot_2")
        plugin._save_sync_service.switch_slot.assert_awaited_once_with(42, "slot_2")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_delegates(self, plugin):
        plugin._save_sync_service.get_slot_delete_info = AsyncMock(return_value={"local": 1})
        result = await plugin.get_slot_delete_info(42, "default")
        plugin._save_sync_service.get_slot_delete_info.assert_awaited_once_with(42, "default")
        assert result == {"local": 1}

    @pytest.mark.asyncio
    async def test_delete_slot_delegates(self, plugin):
        plugin._save_sync_service.delete_slot = AsyncMock(return_value={"deleted": True})
        result = await plugin.delete_slot(42, "default")
        plugin._save_sync_service.delete_slot.assert_awaited_once_with(42, "default")
        assert result == {"deleted": True}

    @pytest.mark.asyncio
    async def test_is_save_tracking_configured_delegates(self, plugin):
        plugin._save_sync_service.is_save_tracking_configured.return_value = True
        result = await plugin.is_save_tracking_configured(42)
        plugin._save_sync_service.is_save_tracking_configured.assert_called_once_with(42)
        assert result is True

    @pytest.mark.asyncio
    async def test_get_save_setup_info_delegates(self, plugin):
        plugin._save_sync_service.get_save_setup_info = AsyncMock(return_value={"info": "x"})
        result = await plugin.get_save_setup_info(42)
        plugin._save_sync_service.get_save_setup_info.assert_awaited_once_with(42)
        assert result == {"info": "x"}

    @pytest.mark.asyncio
    async def test_confirm_slot_choice_delegates(self, plugin):
        plugin._save_sync_service.confirm_slot_choice = AsyncMock(return_value={"ok": True})
        result = await plugin.confirm_slot_choice(42, "default", "slot_1")
        plugin._save_sync_service.confirm_slot_choice.assert_awaited_once_with(42, "default", "slot_1")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_resolve_sync_conflict_delegates(self, plugin):
        plugin._save_sync_service.resolve_sync_conflict = AsyncMock(return_value={"ok": True})
        result = await plugin.resolve_sync_conflict(42, "save.srm", 99, "keep_local")
        plugin._save_sync_service.resolve_sync_conflict.assert_awaited_once_with(
            42,
            "save.srm",
            99,
            "keep_local",
        )
        assert result == {"ok": True}


# ── SteamGridDB callables ──────────────────────────────────────────────


class TestSgdbCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_sgdb_artwork_base64_delegates(self, plugin):
        plugin._sgdb_service.get_sgdb_artwork_base64 = AsyncMock(return_value={"base64": "data"})
        result = await plugin.get_sgdb_artwork_base64(42, 1)
        plugin._sgdb_service.get_sgdb_artwork_base64.assert_awaited_once_with(42, 1)
        assert result == {"base64": "data"}

    @pytest.mark.asyncio
    async def test_verify_sgdb_api_key_delegates(self, plugin):
        plugin._sgdb_service.verify_sgdb_api_key = AsyncMock(return_value={"valid": True})
        result = await plugin.verify_sgdb_api_key("abc")
        plugin._sgdb_service.verify_sgdb_api_key.assert_awaited_once_with("abc")
        assert result == {"valid": True}

    @pytest.mark.asyncio
    async def test_save_sgdb_api_key_delegates(self, plugin):
        plugin._sgdb_service.save_sgdb_api_key.return_value = {"ok": True}
        result = await plugin.save_sgdb_api_key("abc")
        plugin._sgdb_service.save_sgdb_api_key.assert_called_once_with("abc")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_save_shortcut_icon_delegates(self, plugin):
        plugin._sgdb_service.save_shortcut_icon = AsyncMock(return_value={"ok": True})
        result = await plugin.save_shortcut_icon(123, "data")
        plugin._sgdb_service.save_shortcut_icon.assert_awaited_once_with(123, "data")
        assert result == {"ok": True}


# ── Metadata / achievements / game-detail callables ────────────────────


class TestMetadataCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_rom_metadata_delegates(self, plugin):
        plugin._metadata_service.get_rom_metadata.return_value = {"name": "x"}
        result = await plugin.get_rom_metadata(42)
        plugin._metadata_service.get_rom_metadata.assert_called_once_with(42)
        assert result == {"name": "x"}

    @pytest.mark.asyncio
    async def test_get_all_metadata_cache_delegates(self, plugin):
        plugin._metadata_service.get_all_metadata_cache.return_value = {}
        result = await plugin.get_all_metadata_cache()
        plugin._metadata_service.get_all_metadata_cache.assert_called_once_with()
        assert result == {}

    @pytest.mark.asyncio
    async def test_get_app_id_rom_id_map_delegates(self, plugin):
        plugin._metadata_service.get_app_id_rom_id_map.return_value = {"100": 42}
        result = await plugin.get_app_id_rom_id_map()
        plugin._metadata_service.get_app_id_rom_id_map.assert_called_once_with()
        assert result == {"100": 42}


class TestAchievementsCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_achievements_delegates(self, plugin):
        plugin._achievements_service.get_achievements = AsyncMock(return_value=[])
        result = await plugin.get_achievements(42)
        plugin._achievements_service.get_achievements.assert_awaited_once_with(42)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_achievement_progress_delegates(self, plugin):
        plugin._achievements_service.get_achievement_progress = AsyncMock(return_value={"completed": 0})
        result = await plugin.get_achievement_progress(42)
        plugin._achievements_service.get_achievement_progress.assert_awaited_once_with(42)
        assert result == {"completed": 0}


class TestGameDetailCallableDelegation:
    @pytest.mark.asyncio
    async def test_get_cached_game_detail_delegates(self, plugin):
        plugin._game_detail_service.get_cached_game_detail.return_value = {"detail": "x"}
        result = await plugin.get_cached_game_detail("12345")
        plugin._game_detail_service.get_cached_game_detail.assert_called_once_with("12345")
        assert result == {"detail": "x"}


# ── Error-propagation tests ────────────────────────────────────────────
#
# Each callable below wraps a service call without any try/except — an
# exception from the service must propagate cleanly. We pick one
# representative callable per service family for the assertion; the
# delegation tests above already cover the happy paths.


class TestCallableErrorPropagation:
    @pytest.mark.asyncio
    async def test_save_settings_propagates(self, plugin):
        plugin._settings_service.save_settings.side_effect = ValueError("bad")
        with pytest.raises(ValueError, match="bad"):
            await plugin.save_settings("x", "u", "p")

    @pytest.mark.asyncio
    async def test_test_connection_propagates(self, plugin):
        plugin._connection_service.test_connection = AsyncMock(side_effect=RuntimeError("down"))
        with pytest.raises(RuntimeError, match="down"):
            await plugin.test_connection()

    @pytest.mark.asyncio
    async def test_migrate_retrodeck_files_propagates(self, plugin):
        plugin._migration_service.migrate_retrodeck_files = AsyncMock(side_effect=OSError("io"))
        with pytest.raises(OSError, match="io"):
            await plugin.migrate_retrodeck_files()

    @pytest.mark.asyncio
    async def test_get_firmware_status_propagates(self, plugin):
        plugin._firmware_service.get_firmware_status = AsyncMock(side_effect=RuntimeError("api"))
        with pytest.raises(RuntimeError, match="api"):
            await plugin.get_firmware_status()

    @pytest.mark.asyncio
    async def test_sync_preview_propagates(self, plugin):
        plugin._sync_service.sync_preview = AsyncMock(side_effect=RuntimeError("preview"))
        with pytest.raises(RuntimeError, match="preview"):
            await plugin.sync_preview()

    @pytest.mark.asyncio
    async def test_start_download_propagates(self, plugin):
        plugin._download_service.start_download = AsyncMock(side_effect=RuntimeError("dl"))
        with pytest.raises(RuntimeError, match="dl"):
            await plugin.start_download(42)

    @pytest.mark.asyncio
    async def test_remove_rom_propagates(self, plugin):
        plugin._rom_removal_service.remove_rom = AsyncMock(side_effect=RuntimeError("rm"))
        with pytest.raises(RuntimeError, match="rm"):
            await plugin.remove_rom(42)

    @pytest.mark.asyncio
    async def test_get_save_status_propagates(self, plugin):
        plugin._save_sync_service.get_save_status = AsyncMock(side_effect=RuntimeError("save"))
        with pytest.raises(RuntimeError, match="save"):
            await plugin.get_save_status(42)

    @pytest.mark.asyncio
    async def test_get_sgdb_artwork_base64_propagates(self, plugin):
        plugin._sgdb_service.get_sgdb_artwork_base64 = AsyncMock(side_effect=RuntimeError("sgdb"))
        with pytest.raises(RuntimeError, match="sgdb"):
            await plugin.get_sgdb_artwork_base64(42, 1)

    @pytest.mark.asyncio
    async def test_get_achievements_propagates(self, plugin):
        plugin._achievements_service.get_achievements = AsyncMock(side_effect=RuntimeError("ach"))
        with pytest.raises(RuntimeError, match="ach"):
            await plugin.get_achievements(42)

    @pytest.mark.asyncio
    async def test_get_artwork_base64_propagates(self, plugin):
        plugin._artwork_service.get_artwork_base64 = AsyncMock(side_effect=RuntimeError("art"))
        with pytest.raises(RuntimeError, match="art"):
            await plugin.get_artwork_base64(42)

    @pytest.mark.asyncio
    async def test_evaluate_launch_propagates(self, plugin):
        plugin._launch_gate_service.evaluate = AsyncMock(side_effect=RuntimeError("gate"))
        with pytest.raises(RuntimeError, match="gate"):
            await plugin.evaluate_launch(42)


# ── _unload lifecycle hook ─────────────────────────────────────────────


class TestUnloadHook:
    """Cover the ``_unload`` shutdown sequence."""

    @pytest.mark.asyncio
    async def test_unload_calls_shutdown_on_sync_and_download(self, plugin):
        # DownloadService.shutdown is async (it awaits the poll task);
        # the bare-MagicMock default returns a non-awaitable.
        plugin._download_service.shutdown = AsyncMock()
        await plugin._unload()
        plugin._sync_service.shutdown.assert_called_once_with()
        plugin._download_service.shutdown.assert_awaited_once_with()
