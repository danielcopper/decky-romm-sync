import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.persistence import PersistenceAdapter
from adapters.steam_config import SteamConfigAdapter

# conftest.py patches decky before this import
from main import Plugin
from services.library import LibraryService
from services.steamgrid import SteamGridService


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._metadata_cache = {}
    # Default to "/tmp" so the prune guard sees an existing home in tests that
    # don't override it. Tests exercising the guard set get_retrodeck_home to
    # return a non-existent path or empty string.
    p._retrodeck_paths = MagicMock()
    p._retrodeck_paths.get_retrodeck_home.return_value = "/tmp"
    # Default migration service mock — no migration pending. Tests that need
    # to exercise the @migration_blocked gate override this.
    p._migration_service = MagicMock()
    p._migration_service.is_retrodeck_migration_pending.return_value = False

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        romm_api=p._romm_api,
        steam_config=steam_config,
        state=p._state,
        settings=p.settings,
        metadata_cache=p._metadata_cache,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        plugin_dir=decky.DECKY_PLUGIN_DIR,
        emit=decky.emit,
        clock=FakeClock(),
        uuid_gen=FakeUuidGen(),
        sleeper=FakeSleeper(),
        save_state=p._save_state,
        save_settings_to_disk=p._save_settings_to_disk,
        log_debug=p._log_debug,
    )

    p._sgdb_service = SteamGridService(
        sgdb_api=MagicMock(),
        romm_api=p._romm_api,
        steam_config=steam_config,
        state=p._state,
        settings=p.settings,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
        save_state=MagicMock(),
        save_settings_to_disk=MagicMock(),
        get_pending_sync=lambda: p._sync_service._pending_sync,
    )
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

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["romm_pass"] = "original"
        await plugin.save_settings("http://example.com", "user", "••••")
        assert plugin.settings["romm_pass"] == "original"

    @pytest.mark.asyncio
    async def test_save_settings_updates_real_password(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["romm_pass"] = "old"
        await plugin.save_settings("http://example.com", "user", "newpass")
        assert plugin.settings["romm_pass"] == "newpass"


class TestConnection:
    @pytest.mark.asyncio
    async def test_test_connection_sets_version_on_romm_api(self, plugin):
        plugin.loop = asyncio.get_event_loop()
        plugin.settings["romm_url"] = "http://romm.local"
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        plugin._romm_api.list_platforms.return_value = [{"id": 1, "slug": "n64"}]
        result = await plugin.test_connection()
        assert result["success"] is True
        plugin._romm_api.set_version.assert_called_once_with("4.8.1")


class TestLogLevel:
    def test_log_debug_enabled(self, plugin):
        """_log_debug logs when log_level is 'debug'."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "debug"
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_called_once_with("test message")

    def test_log_debug_disabled_at_warn(self, plugin):
        """_log_debug does not log when log_level is 'warn' (default)."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "warn"
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_not_called()

    def test_log_debug_disabled_at_info(self, plugin):
        """_log_debug does not log when log_level is 'info'."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "info"
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_not_called()

    def test_log_debug_disabled_at_error(self, plugin):
        """_log_debug does not log when log_level is 'error'."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "error"
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_not_called()

    def test_log_debug_missing_setting_defaults_warn(self, plugin):
        """_log_debug does not log when log_level key is missing (defaults to warn)."""
        from unittest.mock import patch

        import decky

        plugin.settings.pop("log_level", None)
        with patch.object(decky.logger, "info") as mock_info:
            plugin._log_debug("test message")
            mock_info.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_log_level_valid(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        for level in ("debug", "info", "warn", "error"):
            result = await plugin.save_log_level(level)
            assert result["success"] is True
            assert plugin.settings["log_level"] == level

    @pytest.mark.asyncio
    async def test_save_log_level_invalid(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["log_level"] = "warn"
        result = await plugin.save_log_level("verbose")
        assert result["success"] is False
        assert plugin.settings["log_level"] == "warn"  # unchanged

    @pytest.mark.asyncio
    async def test_get_settings_includes_log_level(self, plugin):
        plugin.settings["log_level"] = "info"
        result = await plugin.get_settings()
        assert result["log_level"] == "info"

    @pytest.mark.asyncio
    async def test_get_settings_defaults_log_level_warn(self, plugin):
        plugin.settings.pop("log_level", None)
        result = await plugin.get_settings()
        assert result["log_level"] == "warn"

    @pytest.mark.asyncio
    async def test_frontend_log_respects_level(self, plugin):
        """frontend_log only logs when message level >= configured level."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "warn"
        with (
            patch.object(decky.logger, "info") as mock_info,
            patch.object(decky.logger, "warning") as mock_warning,
            patch.object(decky.logger, "error") as mock_error,
        ):
            await plugin.frontend_log("debug", "debug msg")
            await plugin.frontend_log("info", "info msg")
            await plugin.frontend_log("warn", "warn msg")
            await plugin.frontend_log("error", "error msg")
            mock_info.assert_not_called()
            mock_warning.assert_called_once_with("[FE] warn msg")
            mock_error.assert_called_once_with("[FE] error msg")

    @pytest.mark.asyncio
    async def test_frontend_log_debug_level_logs_all(self, plugin):
        """With log_level=debug, all levels are logged."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "debug"
        with (
            patch.object(decky.logger, "info") as mock_info,
            patch.object(decky.logger, "warning") as mock_warning,
            patch.object(decky.logger, "error") as mock_error,
        ):
            await plugin.frontend_log("debug", "d")
            await plugin.frontend_log("info", "i")
            await plugin.frontend_log("warn", "w")
            await plugin.frontend_log("error", "e")
            assert mock_info.call_count == 2  # debug + info both use logger.info
            mock_warning.assert_called_once_with("[FE] w")
            mock_error.assert_called_once_with("[FE] e")

    @pytest.mark.asyncio
    async def test_debug_log_backward_compat(self, plugin):
        """debug_log callable delegates to frontend_log('debug', ...)."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "debug"
        with patch.object(decky.logger, "info") as mock_info:
            await plugin.debug_log("test backward compat")
            mock_info.assert_called_once_with("[FE] test backward compat")

    def test_migration_debug_logging_true(self, plugin, tmp_path):
        """Old debug_logging=True migrates to log_level='debug'."""
        import logging

        from adapters.persistence import PersistenceAdapter
        from domain.state_migrations import migrate_settings

        settings_path = os.path.join(str(tmp_path), "settings.json")
        os.makedirs(str(tmp_path), exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump({"debug_logging": True, "romm_url": ""}, f)
        persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), logging.getLogger("test"))
        plugin.settings = migrate_settings(persistence.load_settings())
        assert "debug_logging" not in plugin.settings
        assert plugin.settings["log_level"] == "debug"

    def test_migration_debug_logging_false(self, plugin, tmp_path):
        """Old debug_logging=False migrates to log_level='warn' (default)."""
        import logging

        from adapters.persistence import PersistenceAdapter
        from domain.state_migrations import migrate_settings

        settings_path = os.path.join(str(tmp_path), "settings.json")
        os.makedirs(str(tmp_path), exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump({"debug_logging": False, "romm_url": ""}, f)
        persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), logging.getLogger("test"))
        plugin.settings = migrate_settings(persistence.load_settings())
        assert "debug_logging" not in plugin.settings
        assert plugin.settings["log_level"] == "warn"

    @pytest.mark.asyncio
    async def test_sgdb_artwork_silent_when_debug_off(self, plugin, tmp_path):
        """SGDB artwork info calls should not log when log_level is 'warn'."""
        from unittest.mock import patch

        import decky

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["log_level"] = "warn"
        with patch.object(decky.logger, "info") as mock_info:
            result = await plugin.get_sgdb_artwork_base64(1, 99)
            assert result["base64"] is None
            for call in mock_info.call_args_list:
                assert "SGDB artwork" not in str(call)

    @pytest.mark.asyncio
    async def test_sgdb_artwork_logs_when_debug_enabled(self, plugin, tmp_path):
        """SGDB artwork info calls should log when log_level is 'debug'."""
        from unittest.mock import patch

        import decky

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["log_level"] = "debug"
        plugin.settings["steamgriddb_api_key"] = ""
        plugin._state["shortcut_registry"]["1"] = {"sgdb_id": None, "igdb_id": None}
        with patch.object(decky.logger, "info") as mock_info:
            result = await plugin.get_sgdb_artwork_base64(1, 1)
            assert result["no_api_key"] is True
            logged_msgs = [str(c) for c in mock_info.call_args_list]
            assert any("SGDB artwork" in m for m in logged_msgs)


class TestInsecureSslSetting:
    def test_load_settings_defaults_false(self, plugin, tmp_path):
        import logging

        from adapters.persistence import PersistenceAdapter

        settings_path = os.path.join(str(tmp_path), "settings.json")
        os.makedirs(str(tmp_path), exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump({"romm_url": "https://romm.local"}, f)
        persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), logging.getLogger("test"))
        plugin.settings = persistence.load_settings()
        assert plugin.settings["romm_allow_insecure_ssl"] is False

    @pytest.mark.asyncio
    async def test_get_settings_includes_field(self, plugin):
        plugin.settings["romm_allow_insecure_ssl"] = True
        result = await plugin.get_settings()
        assert result["romm_allow_insecure_ssl"] is True

    @pytest.mark.asyncio
    async def test_get_settings_defaults_false(self, plugin):
        plugin.settings.pop("romm_allow_insecure_ssl", None)
        result = await plugin.get_settings()
        assert result["romm_allow_insecure_ssl"] is False

    @pytest.mark.asyncio
    async def test_save_settings_with_insecure_ssl(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        await plugin.save_settings("https://romm.local", "user", "pass", True)
        assert plugin.settings["romm_allow_insecure_ssl"] is True

    @pytest.mark.asyncio
    async def test_save_settings_without_param_preserves(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["romm_allow_insecure_ssl"] = True
        await plugin.save_settings("https://romm.local", "user", "pass")
        assert plugin.settings["romm_allow_insecure_ssl"] is True

    @pytest.mark.asyncio
    async def test_save_settings_explicit_false(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["romm_allow_insecure_ssl"] = True
        await plugin.save_settings("https://romm.local", "user", "pass", False)
        assert plugin.settings["romm_allow_insecure_ssl"] is False


class TestSettingsFilePermissions:
    def test_save_settings_creates_file_with_0600(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin.settings = {"romm_url": "http://example.com"}
        plugin._save_settings_to_disk()
        settings_path = tmp_path / "settings.json"
        mode = os.stat(settings_path).st_mode & 0o777
        assert mode == 0o600

    def test_load_settings_fixes_permissions(self, plugin, tmp_path):
        import logging

        from adapters.persistence import PersistenceAdapter

        settings_path = tmp_path / "settings.json"
        import json as _json

        with open(settings_path, "w") as f:
            _json.dump({"romm_url": "http://example.com"}, f)
        os.chmod(settings_path, 0o644)
        assert os.stat(settings_path).st_mode & 0o777 == 0o644
        persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), logging.getLogger("test"))
        persistence.load_settings()
        assert os.stat(settings_path).st_mode & 0o777 == 0o600


class TestPruneStaleState:
    def test_prunes_missing_files(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent/game.z64", "system": "n64"},
        }

        plugin._prune_stale_installed_roms()
        assert "1" not in plugin._state["installed_roms"]

    def test_keeps_existing_files(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
        }

        plugin._prune_stale_installed_roms()
        assert "1" in plugin._state["installed_roms"]

    def test_keeps_existing_rom_dir(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

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

        plugin._prune_stale_installed_roms()
        assert "1" in plugin._state["installed_roms"]

    def test_saves_state_only_when_pruned(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
        }

        # No pruning needed — state file should NOT be written
        state_path = tmp_path / "state.json"
        plugin._prune_stale_installed_roms()
        assert not state_path.exists()

    def test_prunes_mixed(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/gone/game.z64", "system": "snes"},
        }

        plugin._prune_stale_installed_roms()
        assert "1" in plugin._state["installed_roms"]
        assert "2" not in plugin._state["installed_roms"]


class TestPruneStaleStateEdgeCases:
    """Edge case tests for _prune_stale_installed_roms."""

    def test_empty_installed_roms_no_crash(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["installed_roms"] = {}
        plugin._prune_stale_installed_roms()
        # Should not crash, _save_state should NOT be called
        state_path = tmp_path / "state.json"
        assert not state_path.exists()

    def test_all_entries_stale(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/gone/a.z64", "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/gone/b.z64", "system": "snes"},
            "3": {"rom_id": 3, "file_path": "/gone/c.z64", "system": "gb"},
        }

        plugin._prune_stale_installed_roms()
        assert plugin._state["installed_roms"] == {}
        # _save_state should have been called (state.json written)
        state_path = tmp_path / "state.json"
        assert state_path.exists()

    def test_skips_when_retrodeck_home_unavailable(self, plugin, tmp_path):
        """Guard: if retrodeck home doesn't exist on disk (SD card not mounted yet
        at boot), prune skips entirely rather than wiping every entry whose
        file_path lives on the unmounted volume."""
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)
        plugin._retrodeck_paths.get_retrodeck_home.return_value = "/run/media/deck/Emulation/retrodeck-not-mounted"

        plugin._state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": "/run/media/deck/Emulation/retrodeck-not-mounted/roms/n64/a.z64",
                "system": "n64",
            },
        }

        plugin._prune_stale_installed_roms()
        # Entry preserved despite stale file_path — guard short-circuited.
        assert "1" in plugin._state["installed_roms"]

    def test_skips_when_retrodeck_home_unset(self, plugin, tmp_path):
        """Guard: empty retrodeck_home (first-run before path is detected) also
        skips the prune."""
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)
        plugin._retrodeck_paths.get_retrodeck_home.return_value = ""

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/somewhere/a.z64", "system": "n64"},
        }

        plugin._prune_stale_installed_roms()
        assert "1" in plugin._state["installed_roms"]

    def test_skip_preserves_entry_at_old_home_during_migration(self, plugin, tmp_path):
        """Pending migration: entries living under old home are preserved (#251)."""
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)
        plugin._retrodeck_paths.get_retrodeck_home.return_value = str(tmp_path)
        plugin._state["retrodeck_home_path_previous"] = "/run/media/old/retrodeck"

        plugin._state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": "/run/media/old/retrodeck/roms/n64/zelda.z64",
                "system": "n64",
            },
            "2": {
                "rom_id": 2,
                "file_path": "/run/media/old/retrodeck/roms/psx/FF7/FF7.m3u",
                "rom_dir": "/run/media/old/retrodeck/roms/psx/FF7",
                "system": "psx",
            },
        }

        plugin._prune_stale_installed_roms()
        assert "1" in plugin._state["installed_roms"]
        assert "2" in plugin._state["installed_roms"]

    def test_skip_does_not_preserve_unrelated_path(self, plugin, tmp_path):
        """Pending migration: ``old_home="/foo"`` does NOT preserve entry at ``/foobar/x``.

        Path comparison must use the trailing separator to avoid prefix
        false matches like /foo matching /foobar.
        """
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)
        plugin._retrodeck_paths.get_retrodeck_home.return_value = str(tmp_path)
        plugin._state["retrodeck_home_path_previous"] = "/foo"

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/foobar/x.z64", "system": "n64"},
        }

        plugin._prune_stale_installed_roms()
        # /foobar/x is NOT under /foo (with separator), file does not exist → pruned.
        assert "1" not in plugin._state["installed_roms"]

    def test_skip_does_not_fire_when_old_home_is_empty(self, plugin, tmp_path):
        """No migration marker — prune behaves normally."""
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)
        plugin._retrodeck_paths.get_retrodeck_home.return_value = str(tmp_path)
        plugin._state.pop("retrodeck_home_path_previous", None)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/gone/a.z64", "system": "n64"},
        }

        plugin._prune_stale_installed_roms()
        assert "1" not in plugin._state["installed_roms"]

    def test_skip_clears_after_migration_completes(self, plugin, tmp_path):
        """After migration completes (marker dropped), normal prune behavior resumes."""
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)
        plugin._retrodeck_paths.get_retrodeck_home.return_value = str(tmp_path)

        # First pass: marker set, entry preserved.
        plugin._state["retrodeck_home_path_previous"] = "/old/retrodeck"
        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/old/retrodeck/roms/n64/a.z64", "system": "n64"},
        }
        plugin._prune_stale_installed_roms()
        assert "1" in plugin._state["installed_roms"]

        # Second pass: marker cleared (post-migration), file still missing → pruned.
        plugin._state.pop("retrodeck_home_path_previous", None)
        plugin._prune_stale_installed_roms()
        assert "1" not in plugin._state["installed_roms"]

    def test_skip_logs_info_when_preserving(self, plugin, tmp_path, caplog):
        """Skip emits an info log naming the preserved rom_id and old home."""
        import logging

        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)
        plugin._retrodeck_paths.get_retrodeck_home.return_value = str(tmp_path)
        plugin._state["retrodeck_home_path_previous"] = "/old/retrodeck"

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/old/retrodeck/roms/n64/a.z64", "system": "n64"},
        }

        with caplog.at_level(logging.INFO):
            plugin._prune_stale_installed_roms()

        assert any("Skipping prune" in rec.message and "/old/retrodeck" in rec.message for rec in caplog.records)


class TestAtomicSettingsWrite:
    def test_settings_written_atomically(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)

        plugin.settings = {"romm_url": "http://example.com", "romm_user": "user"}
        plugin._save_settings_to_disk()

        settings_path = tmp_path / "settings.json"
        with open(settings_path) as f:
            data = json.load(f)
        assert data["romm_url"] == "http://example.com"
        assert data["romm_user"] == "user"

    def test_settings_no_tmp_left_after_write(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)

        plugin.settings = {"romm_url": "http://example.com"}
        plugin._save_settings_to_disk()

        tmp_file = tmp_path / "settings.json.tmp"
        assert not tmp_file.exists()

    def test_settings_crash_preserves_original(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)

        # Write initial settings
        plugin.settings = {"romm_url": "http://original.com"}
        plugin._save_settings_to_disk()

        # Now simulate a crash during json.dump
        plugin.settings = {"romm_url": "http://corrupted.com"}
        with patch("json.dump", side_effect=OSError("disk full")), pytest.raises(OSError):
            plugin._save_settings_to_disk()

        # Original file should still be intact
        settings_path = tmp_path / "settings.json"
        with open(settings_path) as f:
            data = json.load(f)
        assert data["romm_url"] == "http://original.com"


class TestWhitelistSettings:
    @pytest.mark.asyncio
    async def test_get_whitelist_defaults_empty(self, plugin):
        """Returns empty lists when no whitelist keys exist in settings."""
        plugin.settings.pop("whitelist_disabled_defaults", None)
        plugin.settings.pop("whitelist_custom_names", None)
        result = await plugin.get_whitelist_settings()
        assert result == {"disabled_defaults": [], "custom_names": []}

    @pytest.mark.asyncio
    async def test_update_and_get_whitelist(self, plugin, tmp_path):
        """Round-trip: update then get returns the stored values."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        await plugin.update_whitelist_settings(["chrome"], ["My App"])
        result = await plugin.get_whitelist_settings()
        assert result["disabled_defaults"] == ["chrome"]
        assert result["custom_names"] == ["My App"]

    @pytest.mark.asyncio
    async def test_update_whitelist_validates_disabled_defaults(self, plugin):
        """Rejects non-list disabled_defaults."""
        result = await plugin.update_whitelist_settings("not-a-list", [])
        assert result["success"] is False
        assert "disabled_defaults" in result["message"]

    @pytest.mark.asyncio
    async def test_update_whitelist_validates_custom_names(self, plugin):
        """Rejects non-list custom_names."""
        result = await plugin.update_whitelist_settings([], "not-a-list")
        assert result["success"] is False
        assert "custom_names" in result["message"]

    @pytest.mark.asyncio
    async def test_update_whitelist_validates_inner_types(self, plugin):
        """Rejects lists containing non-string items."""
        result_dd = await plugin.update_whitelist_settings([1, 2], [])
        assert result_dd["success"] is False
        assert "disabled_defaults" in result_dd["message"]

        result_cn = await plugin.update_whitelist_settings([], ["valid", 42])
        assert result_cn["success"] is False
        assert "custom_names" in result_cn["message"]

    @pytest.mark.asyncio
    async def test_update_whitelist_persists(self, plugin, tmp_path):
        """Verifies values are stored in plugin.settings dict after update."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        result = await plugin.update_whitelist_settings(["moonlight"], ["Custom Game"])
        assert result["success"] is True
        assert plugin.settings["whitelist_disabled_defaults"] == ["moonlight"]
        assert plugin.settings["whitelist_custom_names"] == ["Custom Game"]


class TestPruneStaleRegistry:
    def test_prunes_missing_app_id(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A"},
        }
        plugin._prune_stale_registry()
        assert "1" not in plugin._state["shortcut_registry"]

    def test_prunes_zero_app_id(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 0, "name": "Game A"},
        }
        plugin._prune_stale_registry()
        assert "1" not in plugin._state["shortcut_registry"]

    def test_prunes_non_int_app_id(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": "abc", "name": "Game A"},
        }
        plugin._prune_stale_registry()
        assert "1" not in plugin._state["shortcut_registry"]

    def test_keeps_valid_entry(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 12345678, "name": "Game A"},
        }
        plugin._prune_stale_registry()
        assert "1" in plugin._state["shortcut_registry"]

    def test_saves_only_when_pruned(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 12345678, "name": "Game A"},
        }
        plugin._prune_stale_registry()
        # No pruning needed — state file should NOT be written
        state_path = tmp_path / "state.json"
        assert not state_path.exists()

    def test_empty_registry_no_crash(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(decky.DECKY_PLUGIN_SETTINGS_DIR, str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {}
        plugin._prune_stale_registry()
        # Should not crash, state file should NOT be written
        state_path = tmp_path / "state.json"
        assert not state_path.exists()


class TestRefreshMigrationState:
    @pytest.mark.asyncio
    async def test_calls_both_detect_methods(self, plugin):
        from unittest.mock import AsyncMock

        retrodeck_sentinel = {"pending": True, "old_path": "/a", "new_path": "/b"}
        save_sort_sentinel = {"pending": True, "saves_count": 3}
        plugin._migration_service = MagicMock()
        plugin._migration_service.detect_retrodeck_path_change = MagicMock()
        plugin._migration_service.detect_save_sort_change = MagicMock()
        plugin._migration_service.get_migration_status = AsyncMock(return_value=retrodeck_sentinel)
        plugin._migration_service.get_save_sort_migration_status = AsyncMock(return_value=save_sort_sentinel)
        result = await plugin.refresh_migration_state()
        plugin._migration_service.detect_retrodeck_path_change.assert_called_once_with()
        plugin._migration_service.detect_save_sort_change.assert_called_once_with()
        assert result == {"retrodeck": retrodeck_sentinel, "save_sort": save_sort_sentinel}

    @pytest.mark.asyncio
    async def test_propagates_exceptions(self, plugin):
        from unittest.mock import AsyncMock

        plugin._migration_service = MagicMock()
        plugin._migration_service.detect_retrodeck_path_change = MagicMock(side_effect=RuntimeError("boom"))
        plugin._migration_service.detect_save_sort_change = MagicMock()
        plugin._migration_service.get_migration_status = AsyncMock(return_value={"pending": False})
        plugin._migration_service.get_save_sort_migration_status = AsyncMock(return_value={"pending": False})
        with pytest.raises(RuntimeError, match="boom"):
            await plugin.refresh_migration_state()
        plugin._migration_service.detect_save_sort_change.assert_not_called()
        plugin._migration_service.get_migration_status.assert_not_called()
        plugin._migration_service.get_save_sort_migration_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_detect_order_preserved(self, plugin):
        from unittest.mock import AsyncMock

        plugin._migration_service = MagicMock()
        manager = MagicMock()
        plugin._migration_service.detect_retrodeck_path_change = manager.detect_retrodeck_path_change
        plugin._migration_service.detect_save_sort_change = manager.detect_save_sort_change
        plugin._migration_service.get_migration_status = AsyncMock(return_value={"pending": False})
        plugin._migration_service.get_save_sort_migration_status = AsyncMock(return_value={"pending": False})
        await plugin.refresh_migration_state()
        ordered = [name for name, _args, _kwargs in manager.mock_calls]
        assert ordered == ["detect_retrodeck_path_change", "detect_save_sort_change"]


_MIGRATION_BLOCKED_WHITELIST: set[str] = {
    # Migration management itself (the unblock pathway must work while pending).
    "migrate_retrodeck_files",
    "get_migration_status",
    "get_save_sort_migration_status",
    "migrate_save_sort_files",
    "dismiss_save_sort_migration",
    "dismiss_retrodeck_migration",
    "refresh_migration_state",
    # Connection / settings (read-only or non-retrodeck).
    "test_connection",
    "get_romm_version",
    "save_settings",
    "get_settings",
    "get_whitelist_settings",
    "update_whitelist_settings",
    "save_collection_platform_groups",
    # Cancel operations — must remain callable mid-operation when migration
    # marker fires so the user can stop in-flight work.
    "cancel_sync",
    "sync_cancel_preview",
    "cancel_download",
    # Frontend logging / diagnostic helpers.
    "frontend_log",
    "debug_log",
    "save_log_level",
    "save_steam_input_setting",
    "apply_steam_input_setting",
    "fix_retroarch_input_driver",
    # Read-only library / sync state queries.
    "get_cached_game_detail",
    "get_available_cores",
    "get_platforms",
    "get_collections",
    "get_sync_progress",
    "sync_heartbeat",
    "report_sync_results",
    "get_registry_platforms",
    "report_removal_results",
    "get_artwork_base64",
    "get_sync_stats",
    "get_rom_by_steam_app_id",
    "get_download_queue",
    "get_installed_rom",
    # Firmware / BIOS read-only checks.
    "get_firmware_status",
    "check_platform_bios",
    "get_bios_status",
    # Save sync read-only / device queries.
    "ensure_device_registered",
    "list_devices",
    "get_save_status",
    "check_core_change",
    "get_save_slots",
    "get_slot_saves",
    "get_slot_delete_info",
    "is_save_tracking_configured",
    "get_save_setup_info",
    "get_save_sync_settings",
    "saves_list_file_versions",
    # Playtime queries.
    "record_session_start",
    "record_session_end",
    "get_server_playtime",
    "get_all_playtime",
    # SteamGridDB / Steam shortcut artwork (Steam-side, not retrodeck).
    "get_sgdb_artwork_base64",
    "verify_sgdb_api_key",
    "save_sgdb_api_key",
    "save_shortcut_icon",
    # Metadata cache reads.
    "get_rom_metadata",
    "get_all_metadata_cache",
    "get_app_id_rom_id_map",
    # Achievements queries (server-side).
    "get_achievements",
    "get_achievement_progress",
    "sync_achievements_after_session",
}


class TestMigrationBlockedDecoratorCoverage:
    """Every Decky callable on Plugin must be classified: either explicitly
    whitelisted (read-only / unblock pathway / non-retrodeck) or decorated
    with @migration_blocked. Prevents new callables from being silently
    unguarded against pending migration corruption (#251)."""

    def test_all_callables_either_whitelisted_or_decorated(self):
        import inspect

        from main import Plugin

        unclassified: list[str] = []
        for name, value in inspect.getmembers(Plugin, predicate=inspect.iscoroutinefunction):
            if name.startswith("_"):
                continue  # lifecycle hooks (_main, _unload) are not callables
            if name in _MIGRATION_BLOCKED_WHITELIST:
                continue
            if getattr(value, "_migration_blocked", False):
                continue
            unclassified.append(name)

        assert not unclassified, (
            "Unclassified async callables on Plugin — every one must be in "
            "_MIGRATION_BLOCKED_WHITELIST or carry @migration_blocked: "
            f"{sorted(unclassified)}"
        )

    def test_no_callable_is_both_decorated_and_whitelisted(self):
        """A callable that is both decorated AND whitelisted is silently
        passing the coverage check — likely a misclassification. Catch it."""
        import inspect

        from main import Plugin

        double_classified: list[str] = []
        for name, value in inspect.getmembers(Plugin, predicate=inspect.iscoroutinefunction):
            if name.startswith("_"):
                continue
            if name in _MIGRATION_BLOCKED_WHITELIST and getattr(value, "_migration_blocked", False):
                double_classified.append(name)

        assert not double_classified, (
            "Callables both whitelisted AND decorated with @migration_blocked — "
            f"remove from one: {sorted(double_classified)}"
        )

    def test_whitelisted_callables_are_not_decorated(self):
        """Every name in _MIGRATION_BLOCKED_WHITELIST must NOT carry the
        @migration_blocked marker. Symmetric to the prior check, but reads
        from the whitelist side."""
        from main import Plugin

        decorated: list[str] = []
        for name in _MIGRATION_BLOCKED_WHITELIST:
            method = getattr(Plugin, name, None)
            if method is None:
                continue
            if getattr(method, "_migration_blocked", False) is True:
                decorated.append(name)

        assert not decorated, f"Whitelisted callables that also carry @migration_blocked: {sorted(decorated)}"


class TestMainStartupOrdering:
    """Lock-in test for the #251 startup-order invariant: detect_retrodeck_path_change
    must run BEFORE _prune_stale_installed_roms so the prune skips entries living
    under a pending migration's previous home. Brittle by design — the assertion
    is intentionally narrow."""

    @pytest.mark.asyncio
    async def test_main_calls_detect_path_change_before_prune(self):
        from unittest.mock import AsyncMock, patch

        from main import Plugin

        plugin = Plugin()
        plugin._persistence = MagicMock()
        plugin._persistence.load_settings.return_value = {}
        plugin._persistence.load_state.side_effect = lambda x: x
        plugin._persistence.load_metadata_cache.return_value = {}

        call_order: list[str] = []

        # Mocks for the call-order check.
        migration_service = MagicMock()
        migration_service.detect_retrodeck_path_change.side_effect = lambda: call_order.append(
            "detect_retrodeck_path_change"
        )
        migration_service.detect_save_sort_change = MagicMock()

        save_sync_service = MagicMock()
        save_sync_service.init_state = MagicMock()
        save_sync_service.load_state = MagicMock()
        save_sync_service.prune_orphaned_state = MagicMock()

        sgdb_service = MagicMock()
        sgdb_service.prune_orphaned_artwork_cache = MagicMock()

        artwork_service = MagicMock()
        artwork_service.prune_orphaned_staging_artwork = MagicMock()

        download_service = MagicMock()
        download_service.cleanup_leftover_tmp_files = MagicMock()
        download_service.poll_download_requests = AsyncMock()

        firmware_service = MagicMock()
        firmware_service.load_bios_registry = MagicMock()

        wired_services = {
            "save_sync_service": save_sync_service,
            "playtime_service": MagicMock(),
            "sync_service": MagicMock(),
            "download_service": download_service,
            "rom_removal_service": MagicMock(),
            "firmware_service": firmware_service,
            "sgdb_service": sgdb_service,
            "metadata_service": MagicMock(),
            "achievements_service": MagicMock(),
            "migration_service": migration_service,
            "game_detail_service": MagicMock(),
            "artwork_service": artwork_service,
            "shortcut_removal_service": MagicMock(),
        }

        bootstrapped_adapters = {
            "persistence": plugin._persistence,
            "http_adapter": MagicMock(),
            "romm_api": MagicMock(),
            "steam_config": MagicMock(),
            "sgdb_adapter": MagicMock(),
            "retrodeck_paths": MagicMock(),
            "retroarch_config": MagicMock(),
            "retroarch_core_info": MagicMock(),
            "clock": MagicMock(),
            "uuid_gen": MagicMock(),
            "sleeper": MagicMock(),
            "es_de_core_info": MagicMock(),
        }

        with (
            patch("main.bootstrap", return_value=bootstrapped_adapters),
            patch("main.wire_services", return_value=wired_services),
            patch.object(
                Plugin,
                "_prune_stale_installed_roms",
                lambda self: call_order.append("_prune_stale_installed_roms"),
            ),
            patch.object(
                Plugin,
                "_prune_stale_registry",
                lambda self: call_order.append("_prune_stale_registry"),
            ),
        ):
            await plugin._main()

        assert "detect_retrodeck_path_change" in call_order
        assert "_prune_stale_installed_roms" in call_order
        assert call_order.index("detect_retrodeck_path_change") < call_order.index("_prune_stale_installed_roms")


class TestCancelCallablesNotBlockedByMigration:
    """Cancel operations stop running work — they must remain callable when
    a migration marker fires so the user can interrupt in-flight operations."""

    @pytest.mark.asyncio
    async def test_cancel_sync_callable_when_migration_pending(self, plugin):
        plugin._migration_service.is_retrodeck_migration_pending.return_value = True
        plugin._sync_service.cancel_sync = MagicMock(return_value={"success": True, "stopped": True})
        result = await plugin.cancel_sync()
        assert result.get("blocked_by_migration") is not True
        plugin._sync_service.cancel_sync.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_cancel_preview_callable_when_migration_pending(self, plugin):
        plugin._migration_service.is_retrodeck_migration_pending.return_value = True
        plugin._sync_service.sync_cancel_preview = MagicMock(return_value={"success": True})
        result = await plugin.sync_cancel_preview()
        assert result.get("blocked_by_migration") is not True
        plugin._sync_service.sync_cancel_preview.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_download_callable_when_migration_pending(self, plugin):
        plugin._migration_service.is_retrodeck_migration_pending.return_value = True
        plugin._download_service = MagicMock()
        plugin._download_service.cancel_download = MagicMock(return_value={"success": True})
        result = await plugin.cancel_download(42)
        assert result.get("blocked_by_migration") is not True
        plugin._download_service.cancel_download.assert_called_once_with(42)


class TestMeetsMinVersion:
    """Direct unit tests for Plugin._meets_min_version static method."""

    def test_exact_minimum(self):
        assert Plugin._meets_min_version("4.8.1") is True

    def test_above_minimum(self):
        assert Plugin._meets_min_version("4.9.0") is True

    def test_below_minimum(self):
        assert Plugin._meets_min_version("4.6.1") is False

    def test_below_minimum_patch(self):
        assert Plugin._meets_min_version("4.8.0") is False

    def test_below_minimum_minor(self):
        assert Plugin._meets_min_version("4.7.0") is False

    def test_major_version_above(self):
        assert Plugin._meets_min_version("5.0.0") is True

    def test_two_part_version(self):
        # (4, 8) < (4, 8, 1) in tuple comparison
        assert Plugin._meets_min_version("4.8") is False

    def test_four_part_version(self):
        # (4, 8, 1, 1) >= (4, 8, 1)
        assert Plugin._meets_min_version("4.8.1.1") is True

    def test_malformed_string(self):
        assert Plugin._meets_min_version("abc") is False

    def test_empty_string(self):
        assert Plugin._meets_min_version("") is False

    def test_development_string(self):
        assert Plugin._meets_min_version("development") is False
