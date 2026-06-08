"""Tests for ConnectionService."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from lib.errors import RommAuthError, RommConnectionError, RommForbiddenError, RommServerError
from services.connection import ConnectionService, ConnectionServiceConfig

_MIN_VERSION = (4, 8, 1)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_connection")


@pytest.fixture
def romm_api() -> MagicMock:
    api = MagicMock()
    api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
    api.list_platforms.return_value = [{"id": 1, "slug": "n64"}]
    api.mint_client_token.return_value = {"id": 42, "raw_token": "rmm_minted"}
    return api


@pytest.fixture
def settings_persister() -> MagicMock:
    return MagicMock()


def _make_service(
    *,
    settings: dict[str, Any],
    romm_api: MagicMock,
    loop: asyncio.AbstractEventLoop,
    logger: logging.Logger,
    settings_persister: MagicMock | None = None,
    min_required_version: tuple[int, ...] = _MIN_VERSION,
) -> ConnectionService:
    return ConnectionService(
        config=ConnectionServiceConfig(
            settings=settings,
            romm_api=romm_api,
            settings_persister=settings_persister if settings_persister is not None else MagicMock(),
            loop=loop,
            logger=logger,
            min_required_version=min_required_version,
        ),
    )


class TestTestConnectionHappyPath:
    def test_returns_success_with_version(self, event_loop, romm_api, logger):
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert result["message"] == "Connected to RomM 4.8.1"
        assert result["romm_version"] == "4.8.1"
        romm_api.set_version.assert_called_once_with("4.8.1")

    def test_version_exact_minimum_succeeds(self, event_loop, romm_api, logger):
        """Version equal to minimum tuple is accepted (>= comparison)."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert result["romm_version"] == "4.8.1"


class TestTestConnectionBadPath:
    def test_missing_url_returns_config_error(self, event_loop, romm_api, logger):
        settings = {"romm_url": ""}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result == {
            "success": False,
            "message": "No server URL configured",
            "error_code": "config_error",
        }
        romm_api.heartbeat.assert_not_called()

    def test_unset_url_key_returns_config_error(self, event_loop, romm_api, logger):
        """``romm_url`` absent from settings dict → config_error."""
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["error_code"] == "config_error"

    def test_no_token_returns_config_error_without_probing(self, event_loop, romm_api, logger):
        """A configured URL but no minted token short-circuits before any network
        call — an unauthenticated scoped probe is never fired (#928)."""
        settings = {"romm_url": "http://romm.local"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result == {
            "success": False,
            "message": "Not signed in — sign in to RomM first",
            "error_code": "config_error",
        }
        romm_api.heartbeat.assert_not_called()
        romm_api.list_platforms.assert_not_called()

    def test_heartbeat_connection_error_clears_version(self, event_loop, romm_api, logger):
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.side_effect = RommConnectionError("connection refused")
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["error_code"] == "connection_error"
        romm_api.set_version.assert_called_once_with(None)
        romm_api.list_platforms.assert_not_called()

    def test_list_platforms_server_error_prefixed(self, event_loop, romm_api, logger):
        """Non-auth/forbidden errors from list_platforms get prefixed."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.list_platforms.side_effect = RommServerError("boom", status_code=503)
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["error_code"] == "server_error"
        assert result["message"].startswith("Server reachable but API request failed: ")

    def test_list_platforms_auth_error_not_prefixed(self, event_loop, romm_api, logger):
        """auth_error / forbidden_error keep their original message — no prefix."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.list_platforms.side_effect = RommAuthError("bad credentials")
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["error_code"] == "auth_error"
        assert not result["message"].startswith("Server reachable")


class TestTestConnectionVersionGate:
    def test_version_below_minimum_rejected(self, event_loop, romm_api, logger):
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["error_code"] == "version_error"
        assert result["romm_version"] == "4.5.0"
        assert "4.8.1" in result["message"]
        assert "4.5.0" in result["message"]

    def test_version_one_patch_below_minimum_rejected(self, event_loop, romm_api, logger):
        """4.8.0 is below 4.8.1 — must be rejected."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.0"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["error_code"] == "version_error"

    def test_development_version_bypasses_gate(self, event_loop, romm_api, logger):
        """``development`` version string skips the minimum-version check."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "development"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert result["message"] == "Connected to RomM"
        assert result["romm_version"] == "development"


class TestTestConnectionEdgeCases:
    def test_heartbeat_without_system_field(self, event_loop, romm_api, logger):
        """Heartbeat dict without SYSTEM.VERSION → list_platforms still probed, success without romm_version."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert result["message"] == "Connected to RomM"
        assert "romm_version" not in result
        romm_api.set_version.assert_called_once_with(None)

    def test_heartbeat_returns_none_safely_handled(self, event_loop, romm_api, logger):
        """A ``None`` heartbeat payload is tolerated via ``contextlib.suppress``."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = None
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        # No version detected from heartbeat → set_version called with None.
        romm_api.set_version.assert_called_once_with(None)

    def test_heartbeat_with_malformed_system_field(self, event_loop, romm_api, logger):
        """SYSTEM field that is not a dict raises AttributeError, suppressed → version=None."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": "not-a-dict"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        romm_api.set_version.assert_called_once_with(None)

    def test_min_required_version_injected(self, event_loop, romm_api, logger):
        """Service uses the injected minimum, not a hard-coded tuple."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "5.0.0"}}
        # Inject a higher minimum so 5.0.0 is rejected.
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            min_required_version=(5, 1, 0),
        )
        result = event_loop.run_until_complete(service.test_connection())
        assert result["error_code"] == "version_error"
        assert "5.1.0" in result["message"]


class TestEstablishTokenHappyPath:
    def test_mints_and_stores_token(self, event_loop, romm_api, logger, settings_persister):
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is True
        assert result["romm_version"] == "4.8.1"
        assert settings["romm_api_token"] == "rmm_minted"
        assert settings["romm_api_token_id"] == 42
        # URL persisted + token persisted → at least two saves.
        assert settings_persister.save_settings.call_count >= 2

    def test_mints_with_no_preexisting_token(self, event_loop, romm_api, logger):
        """``establish_token`` is the path that mints the first token — it must
        proceed even though no token is stored yet (#928 guard applies only to
        ``test_connection``, never here)."""
        settings: dict[str, Any] = {}
        assert "romm_api_token" not in settings
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is True
        romm_api.mint_client_token.assert_called_once()
        assert settings["romm_api_token"] == "rmm_minted"

    def test_does_not_persist_credentials(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert "romm_user" not in settings
        assert "romm_pass" not in settings

    def test_wipes_preexisting_legacy_credentials(self, event_loop, romm_api, logger, settings_persister):
        """A pre-existing romm_user / romm_pass pair is dropped once a token is minted."""
        settings = {"romm_user": "alice", "romm_pass": "secret"}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is True
        assert settings["romm_api_token"] == "rmm_minted"
        assert settings["romm_api_token_id"] == 42
        assert "romm_user" not in settings
        assert "romm_pass" not in settings
        # The token-persist step saves after wiping the credentials.
        assert settings_persister.save_settings.call_count >= 2

    def test_token_name_uses_device_name(self, event_loop, romm_api, logger):
        settings = {"device_name": "MyDeck"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        romm_api.mint_client_token.assert_called_once()
        assert romm_api.mint_client_token.call_args.kwargs["token_name"] == "decky-romm-sync (MyDeck)"

    def test_token_name_defaults_to_steam_deck(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert romm_api.mint_client_token.call_args.kwargs["token_name"] == "decky-romm-sync (Steam Deck)"

    def test_persists_url_and_ssl_flag(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p", allow_insecure_ssl=True))
        assert settings["romm_url"] == "http://romm.local"
        assert settings["romm_allow_insecure_ssl"] is True


class TestEstablishTokenOldTokenDeletion:
    def test_deletes_old_token_before_mint(self, event_loop, romm_api, logger):
        settings = {"romm_api_token_id": 99}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        romm_api.delete_client_token.assert_called_once_with("u", "p", token_id=99)
        assert settings["romm_api_token_id"] == 42

    def test_no_delete_when_no_old_token(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        romm_api.delete_client_token.assert_not_called()

    def test_delete_failure_is_ignored(self, event_loop, romm_api, logger):
        settings = {"romm_api_token_id": 99}
        romm_api.delete_client_token.side_effect = RommServerError("boom", status_code=500)
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        # Delete failure must not abort the mint.
        assert result["success"] is True
        assert settings["romm_api_token"] == "rmm_minted"


class TestEstablishTokenBadPath:
    def test_empty_url_returns_config_error(self, event_loop, romm_api, logger):
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("", "u", "p"))
        assert result == {"success": False, "message": "No server URL configured", "error_code": "config_error"}
        romm_api.mint_client_token.assert_not_called()

    def test_unreachable_returns_connection_error_no_mint(self, event_loop, romm_api, logger):
        romm_api.heartbeat.side_effect = RommConnectionError("refused")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["error_code"] == "connection_error"
        romm_api.mint_client_token.assert_not_called()

    def test_version_too_old_returns_version_error_no_mint(self, event_loop, romm_api, logger):
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["error_code"] == "version_error"
        romm_api.mint_client_token.assert_not_called()

    def test_forbidden_mint_returns_actionable_message(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.side_effect = RommForbiddenError("403")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["error_code"] == "forbidden_error"
        assert "cannot create API tokens" in result["message"]

    def test_auth_error_mint_returns_auth_error(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.side_effect = RommAuthError("401")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["error_code"] == "auth_error"

    def test_missing_raw_token_returns_api_error(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.return_value = {"id": 42}  # no raw_token
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["error_code"] == "api_error"

    def test_missing_id_returns_api_error(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.return_value = {"raw_token": "rmm_x"}  # no id
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["error_code"] == "api_error"

    def test_persist_failure_returns_error_and_does_not_raise(self, event_loop, romm_api, logger, settings_persister):
        settings_persister.save_settings.side_effect = OSError("disk full")
        service = _make_service(
            settings={},
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["error_code"] == "unknown_error"
        assert "disk full" in result["message"]


class TestMigrateLegacyCredentials:
    def test_mints_and_wipes_credentials(self, event_loop, romm_api, logger, settings_persister):
        settings = {"romm_user": "alice", "romm_pass": "secret"}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        event_loop.run_until_complete(service.migrate_legacy_credentials())
        assert settings["romm_api_token"] == "rmm_minted"
        assert settings["romm_api_token_id"] == 42
        assert "romm_user" not in settings
        assert "romm_pass" not in settings
        settings_persister.save_settings.assert_called_once_with()

    def test_noop_when_token_already_present(self, event_loop, romm_api, logger):
        settings = {"romm_api_token": "rmm_existing", "romm_user": "alice", "romm_pass": "secret"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.migrate_legacy_credentials())
        romm_api.mint_client_token.assert_not_called()
        # Credentials untouched.
        assert settings["romm_user"] == "alice"

    def test_noop_when_no_credentials(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.migrate_legacy_credentials())
        romm_api.mint_client_token.assert_not_called()
        assert "romm_api_token" not in settings

    def test_noop_when_only_username(self, event_loop, romm_api, logger):
        settings = {"romm_user": "alice", "romm_pass": ""}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.migrate_legacy_credentials())
        romm_api.mint_client_token.assert_not_called()

    def test_failure_leaves_credentials_and_does_not_raise(self, event_loop, romm_api, logger):
        settings = {"romm_user": "alice", "romm_pass": "secret"}
        romm_api.mint_client_token.side_effect = RommForbiddenError("403")
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        # Must not raise.
        event_loop.run_until_complete(service.migrate_legacy_credentials())
        assert "romm_api_token" not in settings
        assert settings["romm_user"] == "alice"
        assert settings["romm_pass"] == "secret"

    def test_malformed_response_leaves_credentials(self, event_loop, romm_api, logger):
        settings = {"romm_user": "alice", "romm_pass": "secret"}
        romm_api.mint_client_token.return_value = {"id": 1}  # no raw_token
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.migrate_legacy_credentials())
        assert "romm_api_token" not in settings
        assert settings["romm_user"] == "alice"

    def test_persist_failure_is_swallowed(self, event_loop, romm_api, logger, settings_persister, caplog):
        # A disk-write failure during startup migration must not propagate out of
        # _main; the mint succeeds but the persist raises.
        settings = {"romm_user": "alice", "romm_pass": "secret"}
        settings_persister.save_settings.side_effect = OSError("disk full")
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        with caplog.at_level(logging.WARNING, logger="test_connection"):
            event_loop.run_until_complete(service.migrate_legacy_credentials())
        settings_persister.save_settings.assert_called_once_with()
        assert any("Legacy credential migration failed" in r.message for r in caplog.records)
