import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_path_exists_reader import FakePathExistsReader
from fakes.fake_relaunch_options_resolver import FakeRelaunchOptionsResolver
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_settings_persister import FakeSettingsPersister
from fakes.fake_sgdb_artwork_cache import FakeSgdbArtworkCache
from fakes.fake_unit_of_work import FakeUnitOfWorkFactory
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.persistence import PersistenceAdapter, SettingsPersisterAdapter
from adapters.steam_config import SteamConfigAdapter
from lib.retrodeck_health import RetroDeckConfigHealth

# conftest.py patches decky before this import
from main import Plugin
from services.connection import ConnectionService, ConnectionServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.settings import SettingsService, SettingsServiceConfig
from services.startup_healing import StartupHealingService, StartupHealingServiceConfig
from services.steamgrid import SteamGridService, SteamGridServiceConfig


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    # Default to "/tmp" so the prune guard sees an existing home in tests that
    # don't override it. Tests exercising the guard rebuild this with a
    # non-existent path or empty string.
    p._retrodeck_paths = FakeRetroDeckPaths(home="/tmp")
    # Default migration service mock — no migration pending. Tests that need
    # to exercise the @migration_blocked gate override this.
    p._migration_service = MagicMock()
    p._migration_service.is_retrodeck_migration_pending.return_value = False

    import decky

    p._debug_logger = SettingsAwareDebugLogger(settings=p.settings, logger=decky.logger)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._settings_persister = FakeSettingsPersister()

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            settings_persister=p._settings_persister,
            log_debug=p._log_debug,
            artwork=FakeArtworkManager(),
            uow_factory=FakeUnitOfWorkFactory(),
            active_core=FakeActiveCoreResolver(default=(None, None)),
            disc_resolver=FakeDiscResolver(),
        ),
    )

    p._sgdb_service = SteamGridService(
        config=SteamGridServiceConfig(
            sgdb_api=MagicMock(),
            romm_api=p._romm_api,
            steam_config=steam_config,
            sgdb_artwork_cache=FakeSgdbArtworkCache(cache_root=decky.DECKY_PLUGIN_RUNTIME_DIR),
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            settings_persister=FakeSettingsPersister(),
            get_pending_sync=lambda: p._sync_service._pending_sync,
            log_debug=p._log_debug,
            uow_factory=FakeUnitOfWorkFactory(),
        ),
    )

    p._settings_service = SettingsService(
        config=SettingsServiceConfig(
            settings=p.settings,
            uow_factory=FakeUnitOfWorkFactory(),
            logger=decky.logger,
            settings_persister=p._settings_persister,
            steam_config=steam_config,
        ),
    )

    p._connection_service = ConnectionService(
        config=ConnectionServiceConfig(
            settings=p.settings,
            romm_api=p._romm_api,
            settings_persister=p._settings_persister,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            min_required_version=Plugin._MIN_REQUIRED_VERSION,
        ),
    )

    p._startup_healing_service = StartupHealingService(
        config=StartupHealingServiceConfig(
            logger=decky.logger,
            clock=FakeClock(),
            retrodeck_paths=p._retrodeck_paths,
            path_probe=FakePathExistsReader(),
            uow_factory=FakeUnitOfWorkFactory(),
            relaunch_options=FakeRelaunchOptionsResolver(),
        ),
    )
    return p


class TestPersistenceAttributeIsLoud:
    """Regression for #350: dropped the lazy-property fallback.

    Pre-``_main()`` access to ``self._persistence`` must raise
    ``AttributeError`` instead of silently constructing a second
    ``PersistenceAdapter`` instance.
    """

    def test_attribute_missing_on_bare_plugin(self):
        """Direct access to _persistence pre-_main() raises — no lazy fallback."""
        from main import Plugin

        bare = Plugin()

        with pytest.raises(AttributeError, match="_persistence"):
            _ = bare._persistence

    def test_settings_persister_missing_on_bare_plugin(self):
        """``_settings_persister`` is bound only by ``_main()``; bare access raises."""
        from main import Plugin

        bare = Plugin()

        with pytest.raises(AttributeError, match="_settings_persister"):
            _ = bare._settings_persister


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_settings_reports_token_present(self, plugin):
        plugin.settings["romm_api_token"] = "rmm_abc"
        result = await plugin.get_settings()
        assert result["has_token"] is True
        # The token itself is never sent to the frontend.
        assert "rmm_abc" not in str(result)

    @pytest.mark.asyncio
    async def test_get_settings_reports_token_absent(self, plugin):
        plugin.settings["romm_api_token"] = None
        result = await plugin.get_settings()
        assert result["has_token"] is False

    @pytest.mark.asyncio
    async def test_save_server_url_persists_url(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        result = await plugin.save_server_url("http://example.com")
        assert result["success"] is True
        assert plugin.settings["romm_url"] == "http://example.com"

    @pytest.mark.asyncio
    async def test_save_server_url_does_not_touch_token(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["romm_api_token"] = "rmm_keep"
        await plugin.save_server_url("http://example.com")
        assert plugin.settings["romm_api_token"] == "rmm_keep"


class TestConnection:
    @pytest.mark.asyncio
    async def test_test_connection_sets_version_on_romm_api(self, plugin):
        import decky

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_token"
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        plugin._romm_api.list_platforms.return_value = [{"id": 1, "slug": "n64"}]
        # Rebuild connection service with the live event loop so executor
        # callbacks dispatch on the same loop the test awaits.
        plugin._connection_service = ConnectionService(
            config=ConnectionServiceConfig(
                settings=plugin.settings,
                romm_api=plugin._romm_api,
                settings_persister=MagicMock(),
                loop=plugin.loop,
                logger=decky.logger,
                min_required_version=Plugin._MIN_REQUIRED_VERSION,
            ),
        )
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

        plugin.settings["log_level"] = "debug"
        plugin.settings["steamgriddb_api_key"] = ""
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
    async def test_save_server_url_with_insecure_ssl(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        await plugin.save_server_url("https://romm.local", True)
        assert plugin.settings["romm_allow_insecure_ssl"] is True

    @pytest.mark.asyncio
    async def test_save_server_url_without_param_preserves(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["romm_allow_insecure_ssl"] = True
        await plugin.save_server_url("https://romm.local")
        assert plugin.settings["romm_allow_insecure_ssl"] is True

    @pytest.mark.asyncio
    async def test_save_server_url_explicit_false(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)
        plugin.settings["romm_allow_insecure_ssl"] = True
        await plugin.save_server_url("https://romm.local", False)
        assert plugin.settings["romm_allow_insecure_ssl"] is False


class TestGetSettingsResetNotice:
    """The get_settings_reset_notice callable reads the persistent
    ``_settings_reset_notice`` marker from the live settings dict (non-consuming).
    """

    @pytest.mark.asyncio
    async def test_no_marker_returns_not_pending(self, plugin):
        plugin.settings = {"romm_url": "http://romm.local"}
        result = await plugin.get_settings_reset_notice()
        assert result == {"pending": False, "backed_up_to": None}

    @pytest.mark.asyncio
    async def test_marker_present_returns_pending_with_backup(self, plugin):
        plugin.settings = {"_settings_reset_notice": {"backed_up_to": "settings.json.corrupt-1781697600"}}
        result = await plugin.get_settings_reset_notice()
        assert result == {"pending": True, "backed_up_to": "settings.json.corrupt-1781697600"}

    @pytest.mark.asyncio
    async def test_non_consuming_repeated_reads_stay_pending(self, plugin):
        """Unlike the old one-shot drain, repeated reads keep reporting pending —
        the marker is cleared only by an explicit ack, not by reading."""
        plugin.settings = {"_settings_reset_notice": {"backed_up_to": "settings.json.corrupt-42"}}
        first = await plugin.get_settings_reset_notice()
        second = await plugin.get_settings_reset_notice()
        assert first == {"pending": True, "backed_up_to": "settings.json.corrupt-42"}
        assert second == first

    @pytest.mark.asyncio
    async def test_marker_without_backup_key_returns_none_backup(self, plugin):
        """A malformed marker (missing backed_up_to) still reports pending with a
        None backup rather than raising."""
        plugin.settings = {"_settings_reset_notice": {}}
        result = await plugin.get_settings_reset_notice()
        assert result == {"pending": True, "backed_up_to": None}


class TestDismissSettingsResetNotice:
    """The dismiss_settings_reset_notice callable pops the persistent marker and
    persists the dismissal — the user's explicit QAM acknowledgement."""

    @pytest.mark.asyncio
    async def test_pops_marker_and_persists(self, plugin):
        # Mutate the live dict in place (the SettingsService binds this same ref).
        plugin.settings["_settings_reset_notice"] = {"backed_up_to": "settings.json.corrupt-42"}
        before = plugin._settings_persister.save_count

        result = await plugin.dismiss_settings_reset_notice()

        assert result == {"success": True}
        assert "_settings_reset_notice" not in plugin.settings
        # Read-side now reports not-pending.
        assert await plugin.get_settings_reset_notice() == {"pending": False, "backed_up_to": None}
        # The dismissal was persisted.
        assert plugin._settings_persister.save_count == before + 1

    @pytest.mark.asyncio
    async def test_idempotent_when_no_marker(self, plugin):
        """Acking with no marker present is a harmless persisted no-op."""
        assert "_settings_reset_notice" not in plugin.settings
        before = plugin._settings_persister.save_count

        result = await plugin.dismiss_settings_reset_notice()

        assert result == {"success": True}
        assert "_settings_reset_notice" not in plugin.settings
        assert plugin._settings_persister.save_count == before + 1


class TestSettingsFilePermissions:
    def test_save_settings_creates_file_with_0600(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin.settings = {"romm_url": "http://example.com"}
        SettingsPersisterAdapter(plugin._persistence, plugin.settings).save_settings()
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


class TestAtomicSettingsWrite:
    def test_settings_written_atomically(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)

        plugin.settings = {"romm_url": "http://example.com", "romm_user": "user"}
        SettingsPersisterAdapter(plugin._persistence, plugin.settings).save_settings()

        settings_path = tmp_path / "settings.json"
        with open(settings_path) as f:
            data = json.load(f)
        assert data["romm_url"] == "http://example.com"
        assert data["romm_user"] == "user"

    def test_settings_no_tmp_left_after_write(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)

        plugin.settings = {"romm_url": "http://example.com"}
        SettingsPersisterAdapter(plugin._persistence, plugin.settings).save_settings()

        tmp_file = tmp_path / "settings.json.tmp"
        assert not tmp_file.exists()

    def test_settings_crash_preserves_original(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), decky.DECKY_PLUGIN_RUNTIME_DIR, decky.logger)

        # Write initial settings
        plugin.settings = {"romm_url": "http://original.com"}
        persister = SettingsPersisterAdapter(plugin._persistence, plugin.settings)
        persister.save_settings()

        # Now simulate a crash during json.dump
        plugin.settings["romm_url"] = "http://corrupted.com"
        with patch("json.dump", side_effect=OSError("disk full")), pytest.raises(OSError):
            persister.save_settings()

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


class TestRefreshMigrationState:
    @pytest.mark.asyncio
    async def test_delegates_to_migration_service_refresh_state(self, plugin):
        """Plugin callable forwards to MigrationService.refresh_state."""
        from unittest.mock import AsyncMock

        sentinel = {
            "retrodeck": {"pending": True, "old_path": "/a", "new_path": "/b"},
            "save_sort": {"pending": True, "saves_count": 3},
        }
        plugin._migration_service = MagicMock()
        plugin._migration_service.refresh_state = AsyncMock(return_value=sentinel)
        result = await plugin.refresh_migration_state()
        plugin._migration_service.refresh_state.assert_awaited_once_with()
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_propagates_exceptions(self, plugin):
        from unittest.mock import AsyncMock

        plugin._migration_service = MagicMock()
        plugin._migration_service.refresh_state = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await plugin.refresh_migration_state()


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
    "connect_with_credentials",
    "save_server_url",
    "get_settings",
    "get_whitelist_settings",
    "update_whitelist_settings",
    "save_collection_platform_groups",
    # Persistent corrupt-settings-reset notice — the read (banner/card) and the
    # user's explicit QAM ack must both work regardless of a pending migration.
    "get_settings_reset_notice",
    "dismiss_settings_reset_notice",
    # Read-only RetroDECK path-resolution health probe (for the frontend banner).
    "get_retrodeck_status",
    # Cancel / pause operations — must remain callable mid-operation when
    # migration marker fires so the user can stop in-flight work. (Resume,
    # which re-begins a filesystem transfer, IS migration-blocked.)
    "cancel_sync",
    "sync_cancel_preview",
    "cancel_download",
    "pause_download",
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
    "get_platform_core_info",
    # Read-only disc-picker state query (the pin-write select_disc IS decorated).
    "get_disc_selection",
    "get_platforms",
    "get_collections",
    "sync_heartbeat",
    "report_unit_results",
    "get_registry_platforms",
    "report_removal_results",
    "get_artwork_base64",
    "get_sync_status",
    "get_sync_stats",
    "get_rom_by_steam_app_id",
    "get_download_queue",
    "get_installed_rom",
    "evaluate_launch",
    # Launch-gate offline funnel: a local-only drift hash check, a version-free
    # reachability heartbeat, a fire-and-forget read-only save-status refresh,
    # and the pre-launch relaunch re-confirm read (#1150). None mutate RetroDECK
    # state, so all stay callable mid-migration.
    "check_local_drift",
    "probe_reachability",
    "refresh_save_status",
    "get_rom_relaunch_options",
    # End-of-session orchestration — composes record_session_end (whitelisted),
    # post_exit_sync (decorator-gated, but SessionLifecycleService applies its
    # own ``is_retrodeck_migration_pending`` check internally so the
    # destructive sync stays gated), the fire-and-forget achievement refresh,
    # and refresh_migration_state (whitelisted). Whitelisting the umbrella
    # callable matches pre-PR behaviour: the playtime record and migration
    # refresh ran regardless of pending migration; only the save sync was
    # gated, and the lifecycle service preserves that gate inline.
    "finalize_game_session",
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
    "get_all_playtime",
    "reconcile_playtime",
    # SteamGridDB / Steam shortcut artwork (Steam-side, not retrodeck).
    "get_sgdb_artwork_base64",
    "verify_sgdb_api_key",
    "save_sgdb_api_key",
    "save_shortcut_icon",
    "get_sgdb_resolution",
    "search_sgdb_games",
    "apply_sgdb_game_id",
    # Metadata cache reads.
    "get_rom_metadata",
    "get_all_metadata_cache",
    "get_app_id_rom_id_map",
    # Read-only startup launch-options reconcile pull (#1043) — heals drifted
    # shortcut launch commands; pure read, never gated.
    "get_installed_relaunch_options",
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
    """Lock-in test for the #251 startup-order invariant: ``detect_retrodeck_path_change``
    must run BEFORE ``prune_stale_installed_roms`` so the prune skips entries living
    under a pending migration's previous home. Brittle by design — the assertion
    is intentionally narrow."""

    @pytest.mark.asyncio
    async def test_main_calls_detect_path_change_before_prune(self):
        from unittest.mock import patch

        from bootstrap import (
            AdapterBundle,
            BootstrapHandles,
            BootstrapResult,
            CallbackBundle,
            RuntimeAdaptersBundle,
            StateBundle,
        )

        from main import Plugin

        plugin = Plugin()

        call_order: list[str] = []

        # Mocks for the call-order check.
        migration_service = MagicMock()
        migration_service.detect_retrodeck_path_change.side_effect = lambda: call_order.append(
            "detect_retrodeck_path_change"
        )
        migration_service.detect_save_sort_change = MagicMock()

        save_sync_service = MagicMock()

        sgdb_service = MagicMock()
        sgdb_service.prune_orphaned_artwork_cache = MagicMock()

        artwork_service = MagicMock()
        artwork_service.prune_orphaned_staging_artwork = MagicMock()

        download_service = MagicMock()
        download_service.cleanup_leftover_tmp_files = MagicMock()

        firmware_service = MagicMock()
        firmware_service.load_bios_registry = MagicMock()

        startup_healing_service = MagicMock()
        startup_healing_service.prune_stale_installed_roms.side_effect = lambda: call_order.append(
            "prune_stale_installed_roms"
        )
        startup_healing_service.reconcile_orphaned_sync_runs.side_effect = lambda: call_order.append(
            "reconcile_orphaned_sync_runs"
        )

        connection_service = MagicMock()
        connection_service.migrate_legacy_credentials = AsyncMock()

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
            "settings_service": MagicMock(),
            "core_service": MagicMock(),
            "disc_service": MagicMock(),
            "connection_service": connection_service,
            "startup_healing_service": startup_healing_service,
            "launch_gate_service": MagicMock(),
            "session_lifecycle_service": MagicMock(),
            "relaunch_options_resolver": MagicMock(),
        }

        bootstrap_result = BootstrapResult(
            adapters=AdapterBundle(
                http_adapter=MagicMock(),
                romm_api=MagicMock(),
                steam_config=MagicMock(),
                sgdb_adapter=MagicMock(),
                cover_art_file_store=MagicMock(),
                sgdb_artwork_cache=MagicMock(),
                download_file_store=MagicMock(),
                firmware_file_store=MagicMock(),
                migration_file_store=MagicMock(),
                rom_file_store=MagicMock(),
                save_file_store=MagicMock(),
                path_probe=MagicMock(),
                core_info_provider=MagicMock(),
            ),
            stores=StateBundle(
                settings={},
            ),
            callbacks=CallbackBundle(
                retrodeck_paths=MagicMock(),
                get_save_layout=MagicMock(),
                get_core_name=MagicMock(),
                platform_core_reader=MagicMock(),
                m3u_support=MagicMock(),
                system_extensions=MagicMock(),
                list_rom_dir_files=MagicMock(),
                settings_persister=MagicMock(),
                log_debug=MagicMock(),
                plugin_metadata=MagicMock(),
                uow_factory=MagicMock(),
            ),
            runtime_adapters=RuntimeAdaptersBundle(
                clock=MagicMock(),
                uuid_gen=MagicMock(),
                sleeper=MagicMock(),
                hostname_provider=MagicMock(),
                machine_id_provider=MagicMock(),
            ),
            handles=BootstrapHandles(debug_logger=MagicMock(), persistence=MagicMock()),
        )

        with (
            patch("main.bootstrap", return_value=bootstrap_result),
            patch("main.wire_services", return_value=wired_services),
        ):
            await plugin._main()

        assert "detect_retrodeck_path_change" in call_order
        assert "prune_stale_installed_roms" in call_order
        assert call_order.index("detect_retrodeck_path_change") < call_order.index("prune_stale_installed_roms")


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


class TestRetroDeckStatus:
    @pytest.mark.asyncio
    async def test_ok_status_carries_paths(self, plugin):
        plugin._retrodeck_paths = FakeRetroDeckPaths(
            home="/retrodeck",
            config_path="/cfg/retrodeck.json",
            health=RetroDeckConfigHealth.OK,
        )
        result = await plugin.get_retrodeck_status()
        assert result == {
            "status": "ok",
            "config_path": "/cfg/retrodeck.json",
            "resolved_home": "/retrodeck",
        }

    @pytest.mark.asyncio
    async def test_status_is_plain_string_not_enum(self, plugin):
        """The discriminant must serialize as a plain str for the WebSocket bridge."""
        plugin._retrodeck_paths = FakeRetroDeckPaths(health=RetroDeckConfigHealth.UNREADABLE)
        result = await plugin.get_retrodeck_status()
        assert result["status"] == "unreadable"
        assert type(result["status"]) is str

    @pytest.mark.asyncio
    async def test_root_missing_status(self, plugin):
        plugin._retrodeck_paths = FakeRetroDeckPaths(
            home="/missing",
            health=RetroDeckConfigHealth.ROOT_MISSING,
        )
        result = await plugin.get_retrodeck_status()
        assert result["status"] == "root_missing"
        assert result["resolved_home"] == "/missing"

    @pytest.mark.asyncio
    async def test_absent_status(self, plugin):
        plugin._retrodeck_paths = FakeRetroDeckPaths(health=RetroDeckConfigHealth.ABSENT)
        result = await plugin.get_retrodeck_status()
        assert result["status"] == "absent"
