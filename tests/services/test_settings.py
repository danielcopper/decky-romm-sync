"""Tests for SettingsService."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from models.state import PluginState, make_default_plugin_state

from services.settings import SettingsService, SettingsServiceConfig


@pytest.fixture
def settings() -> dict:
    return {}


@pytest.fixture
def state() -> PluginState:
    return make_default_plugin_state()


@pytest.fixture
def settings_persister() -> MagicMock:
    return MagicMock()


@pytest.fixture
def steam_config() -> MagicMock:
    cfg = MagicMock()
    cfg.check_retroarch_input_driver = MagicMock(return_value=None)
    cfg.fix_retroarch_input_driver = MagicMock(
        return_value={"success": True, "message": "Changed input_driver to sdl2"},
    )
    cfg.set_steam_input_config = MagicMock()
    return cfg


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_settings")


@pytest.fixture
def service(settings, state, logger, settings_persister, steam_config) -> SettingsService:
    return SettingsService(
        config=SettingsServiceConfig(
            settings=settings,
            state=state,
            logger=logger,
            settings_persister=settings_persister,
            steam_config=steam_config,
        ),
    )


# ── save_settings ──────────────────────────────────────────────────────


class TestSaveSettings:
    def test_persists_credentials(self, service, settings, settings_persister):
        result = service.save_settings("http://romm.local", "alice", "secret")
        assert result == {"success": True, "message": "Settings saved"}
        assert settings["romm_url"] == "http://romm.local"
        assert settings["romm_user"] == "alice"
        assert settings["romm_pass"] == "secret"
        settings_persister.save_settings.assert_called_once_with()

    def test_masked_password_preserves_existing(self, service, settings, settings_persister):
        settings["romm_pass"] = "original"
        result = service.save_settings("http://romm.local", "alice", "••••")
        assert result["success"] is True
        assert settings["romm_pass"] == "original"
        settings_persister.save_settings.assert_called_once_with()

    def test_empty_password_preserves_existing(self, service, settings):
        settings["romm_pass"] = "original"
        service.save_settings("http://romm.local", "alice", "")
        assert settings["romm_pass"] == "original"

    def test_allow_insecure_ssl_none_does_not_touch_setting(self, service, settings):
        settings["romm_allow_insecure_ssl"] = True
        service.save_settings("http://romm.local", "alice", "secret", None)
        assert settings["romm_allow_insecure_ssl"] is True

    def test_allow_insecure_ssl_true(self, service, settings):
        service.save_settings("http://romm.local", "alice", "secret", True)
        assert settings["romm_allow_insecure_ssl"] is True

    def test_allow_insecure_ssl_false_overrides_true(self, service, settings):
        settings["romm_allow_insecure_ssl"] = True
        service.save_settings("http://romm.local", "alice", "secret", False)
        assert settings["romm_allow_insecure_ssl"] is False

    def test_persistence_failure_returns_error(self, service, settings_persister):
        settings_persister.save_settings.side_effect = OSError("disk full")
        result = service.save_settings("http://romm.local", "alice", "secret")
        assert result["success"] is False
        assert "disk full" in result["message"]


# ── get_settings ───────────────────────────────────────────────────────


class TestGetSettings:
    def test_happy_path(self, service, settings, steam_config):
        settings.update(
            {
                "romm_url": "http://romm.local",
                "romm_user": "alice",
                "romm_pass": "secret",
                "steam_input_mode": "force_on",
                "steamgriddb_api_key": "abc",
                "log_level": "info",
                "romm_allow_insecure_ssl": True,
                "collection_create_platform_groups": True,
            }
        )
        steam_config.check_retroarch_input_driver.return_value = {"warning": False}
        result = service.get_settings()
        assert result["romm_url"] == "http://romm.local"
        assert result["romm_user"] == "alice"
        assert result["romm_pass_masked"] == "••••"
        assert result["sgdb_api_key_masked"] == "••••"
        assert result["has_credentials"] is True
        assert result["steam_input_mode"] == "force_on"
        assert result["log_level"] == "info"
        assert result["romm_allow_insecure_ssl"] is True
        assert result["collection_create_platform_groups"] is True
        assert result["retroarch_input_check"] == {"warning": False}

    def test_masks_password_when_set(self, service, settings):
        settings["romm_pass"] = "secret"
        result = service.get_settings()
        assert result["romm_pass_masked"] == "••••"
        assert "secret" not in str(result)

    def test_empty_password_returns_empty_mask(self, service, settings):
        settings["romm_pass"] = ""
        result = service.get_settings()
        assert result["romm_pass_masked"] == ""

    def test_masks_sgdb_key_when_set(self, service, settings):
        settings["steamgriddb_api_key"] = "longkey"
        result = service.get_settings()
        assert result["sgdb_api_key_masked"] == "••••"
        assert "longkey" not in str(result)

    def test_empty_sgdb_key_returns_empty_mask(self, service, settings):
        settings["steamgriddb_api_key"] = ""
        result = service.get_settings()
        assert result["sgdb_api_key_masked"] == ""

    def test_no_credentials_reports_false(self, service, settings):
        settings["romm_user"] = ""
        settings["romm_pass"] = ""
        result = service.get_settings()
        assert result["has_credentials"] is False

    def test_user_without_password_reports_false(self, service, settings):
        settings["romm_user"] = "alice"
        settings["romm_pass"] = ""
        result = service.get_settings()
        assert result["has_credentials"] is False

    def test_defaults_when_keys_missing(self, service):
        result = service.get_settings()
        assert result["romm_url"] == ""
        assert result["romm_user"] == ""
        assert result["romm_pass_masked"] == ""
        assert result["sgdb_api_key_masked"] == ""
        assert result["has_credentials"] is False
        assert result["steam_input_mode"] == "default"
        assert result["log_level"] == "warn"
        assert result["romm_allow_insecure_ssl"] is False
        assert result["collection_create_platform_groups"] is False

    def test_includes_retroarch_input_check_payload(self, service, steam_config):
        steam_config.check_retroarch_input_driver.return_value = {
            "warning": True,
            "current": "x",
            "config_path": "/cfg",
        }
        result = service.get_settings()
        assert result["retroarch_input_check"]["warning"] is True
        assert result["retroarch_input_check"]["current"] == "x"


# ── save_log_level ─────────────────────────────────────────────────────


class TestSaveLogLevel:
    @pytest.mark.parametrize("level", ["debug", "info", "warn", "error"])
    def test_valid_levels(self, service, settings, settings_persister, level):
        result = service.save_log_level(level)
        assert result == {"success": True}
        assert settings["log_level"] == level
        settings_persister.save_settings.assert_called_once_with()

    def test_invalid_level(self, service, settings, settings_persister):
        result = service.save_log_level("verbose")
        assert result["success"] is False
        assert "Invalid log level" in result["message"]
        assert "log_level" not in settings
        settings_persister.save_settings.assert_not_called()

    def test_empty_string_rejected(self, service, settings):
        result = service.save_log_level("")
        assert result["success"] is False
        assert "log_level" not in settings


# ── frontend_log ───────────────────────────────────────────────────────


class TestFrontendLog:
    def test_warn_configured_drops_info(self, service, settings):
        settings["log_level"] = "warn"
        service._logger = MagicMock()
        service.frontend_log("info", "msg")
        service._logger.info.assert_not_called()
        service._logger.warning.assert_not_called()
        service._logger.error.assert_not_called()

    def test_warn_configured_drops_debug(self, service, settings):
        settings["log_level"] = "warn"
        service._logger = MagicMock()
        service.frontend_log("debug", "msg")
        service._logger.info.assert_not_called()

    def test_warn_configured_emits_warn(self, service, settings):
        settings["log_level"] = "warn"
        service._logger = MagicMock()
        service.frontend_log("warn", "watch out")
        service._logger.warning.assert_called_once_with("[FE] watch out")

    def test_warn_configured_emits_error(self, service, settings):
        settings["log_level"] = "warn"
        service._logger = MagicMock()
        service.frontend_log("error", "boom")
        service._logger.error.assert_called_once_with("[FE] boom")

    def test_debug_configured_emits_debug_and_info(self, service, settings):
        settings["log_level"] = "debug"
        service._logger = MagicMock()
        service.frontend_log("debug", "d")
        service.frontend_log("info", "i")
        # Both go through logger.info
        assert service._logger.info.call_args_list == [
            (("[FE] d",),),
            (("[FE] i",),),
        ]

    def test_unknown_level_treated_as_debug(self, service, settings):
        # Unknown level maps to threshold 0 (debug); with warn (2) configured it is dropped.
        settings["log_level"] = "warn"
        service._logger = MagicMock()
        service.frontend_log("trace", "noise")
        service._logger.info.assert_not_called()

    def test_missing_log_level_defaults_warn(self, service, settings):
        settings.pop("log_level", None)
        service._logger = MagicMock()
        service.frontend_log("info", "msg")
        service._logger.info.assert_not_called()
        service.frontend_log("warn", "msg2")
        service._logger.warning.assert_called_once_with("[FE] msg2")

    def test_returns_none(self, service):
        # Decky callable contract — frontend_log returns nothing meaningful.
        assert service.frontend_log("info", "msg") is None


# ── save_steam_input_setting ──────────────────────────────────────────


class TestSaveSteamInputSetting:
    @pytest.mark.parametrize("mode", ["default", "force_on", "force_off"])
    def test_valid_modes(self, service, settings, settings_persister, mode):
        result = service.save_steam_input_setting(mode)
        assert result == {"success": True}
        assert settings["steam_input_mode"] == mode
        settings_persister.save_settings.assert_called_once_with()

    def test_invalid_mode(self, service, settings, settings_persister):
        result = service.save_steam_input_setting("turbo")
        assert result["success"] is False
        assert "turbo" in result["message"]
        assert "steam_input_mode" not in settings
        settings_persister.save_settings.assert_not_called()

    def test_empty_string_rejected(self, service, settings):
        result = service.save_steam_input_setting("")
        assert result["success"] is False
        assert "steam_input_mode" not in settings


# ── apply_steam_input_setting ─────────────────────────────────────────


class TestApplySteamInputSetting:
    def test_applies_to_registered_shortcuts(self, service, settings, state, steam_config):
        settings["steam_input_mode"] = "force_on"
        state["shortcut_registry"] = {
            "1": {"app_id": 111},
            "2": {"app_id": 222},
            "3": {"name": "no app_id here"},
        }
        result = service.apply_steam_input_setting()
        assert result["success"] is True
        assert "force_on" in result["message"]
        steam_config.set_steam_input_config.assert_called_once_with([111, 222], mode="force_on")

    def test_empty_registry_returns_noop(self, service, state, steam_config):
        state["shortcut_registry"] = {}
        result = service.apply_steam_input_setting()
        assert result == {"success": True, "message": "No shortcuts to update"}
        steam_config.set_steam_input_config.assert_not_called()

    def test_all_entries_lack_app_id(self, service, state, steam_config):
        state["shortcut_registry"] = {"1": {"name": "x"}, "2": {"name": "y"}}
        result = service.apply_steam_input_setting()
        assert result["success"] is True
        assert "No shortcuts" in result["message"]
        steam_config.set_steam_input_config.assert_not_called()

    def test_default_mode_when_unset(self, service, state, steam_config):
        state["shortcut_registry"] = {"1": {"app_id": 1}}
        result = service.apply_steam_input_setting()
        assert result["success"] is True
        steam_config.set_steam_input_config.assert_called_once_with([1], mode="default")

    def test_adapter_failure_returns_error(self, service, state, steam_config):
        state["shortcut_registry"] = {"1": {"app_id": 1}}
        steam_config.set_steam_input_config.side_effect = OSError("boom")
        result = service.apply_steam_input_setting()
        assert result["success"] is False
        assert result["message"] == "Operation failed"


# ── fix_retroarch_input_driver ────────────────────────────────────────


class TestFixRetroarchInputDriver:
    def test_delegates_to_adapter(self, service, steam_config):
        steam_config.fix_retroarch_input_driver.return_value = {"success": True, "message": "ok"}
        result = service.fix_retroarch_input_driver()
        assert result == {"success": True, "message": "ok"}
        steam_config.fix_retroarch_input_driver.assert_called_once_with()


# ── whitelist ──────────────────────────────────────────────────────────


class TestGetWhitelistSettings:
    def test_defaults_to_empty_lists(self, service):
        result = service.get_whitelist_settings()
        assert result == {"disabled_defaults": [], "custom_names": []}

    def test_returns_stored_values(self, service, settings):
        settings["whitelist_disabled_defaults"] = ["chrome"]
        settings["whitelist_custom_names"] = ["My App"]
        result = service.get_whitelist_settings()
        assert result == {"disabled_defaults": ["chrome"], "custom_names": ["My App"]}


class TestUpdateWhitelistSettings:
    def test_happy_path(self, service, settings, settings_persister):
        result = service.update_whitelist_settings(["chrome"], ["My App"])
        assert result == {"success": True}
        assert settings["whitelist_disabled_defaults"] == ["chrome"]
        assert settings["whitelist_custom_names"] == ["My App"]
        settings_persister.save_settings.assert_called_once_with()

    def test_disabled_defaults_not_list_rejected(self, service, settings_persister):
        result = service.update_whitelist_settings("not-a-list", [])
        assert result["success"] is False
        assert "disabled_defaults" in result["message"]
        settings_persister.save_settings.assert_not_called()

    def test_custom_names_not_list_rejected(self, service, settings_persister):
        result = service.update_whitelist_settings([], "not-a-list")
        assert result["success"] is False
        assert "custom_names" in result["message"]
        settings_persister.save_settings.assert_not_called()

    def test_disabled_defaults_with_non_string_rejected(self, service, settings_persister):
        result = service.update_whitelist_settings([1, 2], [])
        assert result["success"] is False
        assert "disabled_defaults" in result["message"]
        settings_persister.save_settings.assert_not_called()

    def test_custom_names_with_non_string_rejected(self, service, settings_persister):
        result = service.update_whitelist_settings([], ["ok", 42])
        assert result["success"] is False
        assert "custom_names" in result["message"]
        settings_persister.save_settings.assert_not_called()

    def test_empty_lists_accepted(self, service, settings):
        result = service.update_whitelist_settings([], [])
        assert result == {"success": True}
        assert settings["whitelist_disabled_defaults"] == []
        assert settings["whitelist_custom_names"] == []


# ── save_collection_platform_groups ───────────────────────────────────


class TestSaveCollectionPlatformGroups:
    def test_enables(self, service, settings, settings_persister):
        result = service.save_collection_platform_groups(True)
        assert result == {"success": True}
        assert settings["collection_create_platform_groups"] is True
        settings_persister.save_settings.assert_called_once_with()

    def test_disables(self, service, settings, settings_persister):
        settings["collection_create_platform_groups"] = True
        result = service.save_collection_platform_groups(False)
        assert result == {"success": True}
        assert settings["collection_create_platform_groups"] is False
        settings_persister.save_settings.assert_called_once_with()

    def test_coerces_truthy(self, service, settings):
        service.save_collection_platform_groups(1)  # type: ignore[arg-type]
        assert settings["collection_create_platform_groups"] is True

    def test_coerces_falsy(self, service, settings):
        service.save_collection_platform_groups(0)  # type: ignore[arg-type]
        assert settings["collection_create_platform_groups"] is False
