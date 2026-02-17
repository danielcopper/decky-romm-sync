import pytest
import json
import os

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._pending_sync = {}
    p._download_tasks = {}
    p._download_queue = {}
    p._download_in_progress = set()
    p._metadata_cache = {}
    return p


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_settings_masks_password(self, plugin):
        plugin.settings["romm_pass"] = "secret123"
        result = await plugin.get_settings()
        assert result["romm_pass_masked"] == "••••"
        assert "secret123" not in str(result)

    @pytest.mark.asyncio
    async def test_get_settings_empty_password(self, plugin):
        plugin.settings["romm_pass"] = ""
        result = await plugin.get_settings()
        assert result["romm_pass_masked"] == ""

    @pytest.mark.asyncio
    async def test_save_settings_skips_masked_password(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        plugin.settings["romm_pass"] = "original"
        await plugin.save_settings("http://example.com", "user", "••••")
        assert plugin.settings["romm_pass"] == "original"

    @pytest.mark.asyncio
    async def test_save_settings_updates_real_password(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        plugin.settings["romm_pass"] = "old"
        await plugin.save_settings("http://example.com", "user", "newpass")
        assert plugin.settings["romm_pass"] == "newpass"


class TestDebugLogging:
    def test_log_debug_enabled(self, plugin):
        """_log_debug logs when debug_logging is True."""
        from unittest.mock import patch
        import decky
        plugin.settings["debug_logging"] = True
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_called_once_with("test message")

    def test_log_debug_disabled(self, plugin):
        """_log_debug does not log when debug_logging is False."""
        from unittest.mock import patch
        import decky
        plugin.settings["debug_logging"] = False
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_not_called()

    def test_log_debug_missing_setting(self, plugin):
        """_log_debug does not log when debug_logging key is missing."""
        from unittest.mock import patch
        import decky
        plugin.settings.pop("debug_logging", None)
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_debug_logging_enables(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        result = await plugin.save_debug_logging(True)
        assert result["success"] is True
        assert plugin.settings["debug_logging"] is True

    @pytest.mark.asyncio
    async def test_save_debug_logging_disables(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        plugin.settings["debug_logging"] = True
        result = await plugin.save_debug_logging(False)
        assert result["success"] is True
        assert plugin.settings["debug_logging"] is False

    @pytest.mark.asyncio
    async def test_save_debug_logging_coerces_to_bool(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        await plugin.save_debug_logging(1)
        assert plugin.settings["debug_logging"] is True
        await plugin.save_debug_logging(0)
        assert plugin.settings["debug_logging"] is False

    @pytest.mark.asyncio
    async def test_get_settings_includes_debug_logging(self, plugin):
        plugin.settings["debug_logging"] = True
        result = await plugin.get_settings()
        assert result["debug_logging"] is True

    @pytest.mark.asyncio
    async def test_get_settings_defaults_debug_logging_false(self, plugin):
        plugin.settings.pop("debug_logging", None)
        result = await plugin.get_settings()
        assert result["debug_logging"] is False

    @pytest.mark.asyncio
    async def test_sgdb_artwork_silent_when_debug_off(self, plugin, tmp_path):
        """SGDB artwork info calls should not log when debug_logging is False."""
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["debug_logging"] = False
        with patch.object(decky.logger, "info") as mock_info:
            # Call with an invalid asset_type_num to trigger early return after the debug log
            result = await plugin.get_sgdb_artwork_base64(1, 99)
            assert result["base64"] is None
            # The SGDB artwork request log should NOT have been called since debug is off
            for call in mock_info.call_args_list:
                assert "SGDB artwork" not in str(call)

    @pytest.mark.asyncio
    async def test_sgdb_artwork_logs_when_debug_enabled(self, plugin, tmp_path):
        """SGDB artwork info calls should log when debug_logging is True."""
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["debug_logging"] = True
        # asset_type 1 = hero, no API key -> will log "skipped: no API key"
        plugin.settings["steamgriddb_api_key"] = ""
        plugin._state["shortcut_registry"]["1"] = {"sgdb_id": None, "igdb_id": None}
        with patch.object(decky.logger, "info") as mock_info:
            result = await plugin.get_sgdb_artwork_base64(1, 1)
            assert result["no_api_key"] is True
            # Should have logged debug messages
            logged_msgs = [str(c) for c in mock_info.call_args_list]
            assert any("SGDB artwork" in m for m in logged_msgs)


class TestPruneStaleState:
    def test_prunes_missing_files(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent/game.z64", "system": "n64"},
        }

        plugin._prune_stale_state()
        assert "1" not in plugin._state["installed_roms"]

    def test_keeps_existing_files(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
        }

        plugin._prune_stale_state()
        assert "1" in plugin._state["installed_roms"]

    def test_keeps_existing_rom_dir(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_dir = tmp_path / "FF7"
        rom_dir.mkdir()

        plugin._state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": str(rom_dir / "FF7.m3u"),  # file missing but dir exists
                "rom_dir": str(rom_dir),
                "system": "psx",
            },
        }

        plugin._prune_stale_state()
        assert "1" in plugin._state["installed_roms"]

    def test_saves_state_only_when_pruned(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
        }

        # No pruning needed — state file should NOT be written
        state_path = tmp_path / "state.json"
        plugin._prune_stale_state()
        assert not state_path.exists()

    def test_prunes_mixed(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/gone/game.z64", "system": "snes"},
        }

        plugin._prune_stale_state()
        assert "1" in plugin._state["installed_roms"]
        assert "2" not in plugin._state["installed_roms"]


class TestPruneStaleStateEdgeCases:
    """Edge case tests for _prune_stale_state."""

    def test_empty_installed_roms_no_crash(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["installed_roms"] = {}
        plugin._prune_stale_state()
        # Should not crash, _save_state should NOT be called
        state_path = tmp_path / "state.json"
        assert not state_path.exists()

    def test_all_entries_stale(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/gone/a.z64", "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/gone/b.z64", "system": "snes"},
            "3": {"rom_id": 3, "file_path": "/gone/c.z64", "system": "gb"},
        }

        plugin._prune_stale_state()
        assert plugin._state["installed_roms"] == {}
        # _save_state should have been called (state.json written)
        state_path = tmp_path / "state.json"
        assert state_path.exists()
