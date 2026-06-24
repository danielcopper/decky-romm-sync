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
            "reason": "config_error",
            "message": "No server URL configured",
        }
        romm_api.heartbeat.assert_not_called()

    def test_unset_url_key_returns_config_error(self, event_loop, romm_api, logger):
        """``romm_url`` absent from settings dict → config_error."""
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["reason"] == "config_error"

    def test_no_token_returns_config_error_without_probing(self, event_loop, romm_api, logger):
        """A configured URL but no minted token short-circuits before any network
        call — an unauthenticated scoped probe is never fired (#928)."""
        settings = {"romm_url": "http://romm.local"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result == {
            "success": False,
            "reason": "config_error",
            "message": "Not signed in — sign in to RomM first",
        }
        romm_api.heartbeat.assert_not_called()
        romm_api.list_platforms.assert_not_called()

    def test_heartbeat_connection_error_clears_version(self, event_loop, romm_api, logger):
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.side_effect = RommConnectionError("connection refused")
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        romm_api.set_version.assert_called_once_with(None)
        romm_api.list_platforms.assert_not_called()

    def test_list_platforms_server_error_prefixed(self, event_loop, romm_api, logger):
        """Non-auth/forbidden errors from list_platforms get prefixed."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.list_platforms.side_effect = RommServerError("boom", status_code=503)
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert result["message"].startswith("Server reachable but API request failed: ")

    def test_list_platforms_auth_error_not_prefixed(self, event_loop, romm_api, logger):
        """auth_error / forbidden_error keep their original message — no prefix."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.list_platforms.side_effect = RommAuthError("bad credentials")
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert not result["message"].startswith("Server reachable")


class TestTestConnectionVersionGate:
    def test_version_below_minimum_rejected(self, event_loop, romm_api, logger):
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["reason"] == "version_error"
        assert result["romm_version"] == "4.5.0"
        assert "4.8.1" in result["message"]
        assert "4.5.0" in result["message"]

    def test_version_one_patch_below_minimum_rejected(self, event_loop, romm_api, logger):
        """4.8.0 is below 4.8.1 — must be rejected."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.0"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["reason"] == "version_error"

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
        assert result["reason"] == "version_error"
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
        # The token's minting origin is stamped from the trimmed URL.
        assert settings["romm_api_token_origin"] == "http://romm.local"
        # url + ssl + token + id + origin commit in a SINGLE atomic save (#1015).
        assert settings_persister.save_settings.call_count == 1

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

    def test_successful_sign_in_preserves_settings_reset_marker(self, event_loop, romm_api, logger, settings_persister):
        """Sign-in no longer clears the corrupt-settings-reset marker — the notice
        is cleared only by an explicit user ack in the QAM
        (``dismiss_settings_reset_notice``), so a successful sign-in PRESERVES it.
        """
        settings = {"_settings_reset_notice": {"backed_up_to": "settings.json.corrupt-42"}}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is True
        # The marker survives the sign-in's token-persist save.
        assert settings["_settings_reset_notice"] == {"backed_up_to": "settings.json.corrupt-42"}

    def test_failed_sign_in_keeps_settings_reset_marker(self, event_loop, romm_api, logger, settings_persister):
        """A failed mint must not clear the marker either (it persists nothing)."""
        romm_api.mint_client_token.side_effect = RommConnectionError("offline")
        settings = {"_settings_reset_notice": {"backed_up_to": "settings.json.corrupt-42"}}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is False
        assert settings["_settings_reset_notice"] == {"backed_up_to": "settings.json.corrupt-42"}
        settings_persister.save_settings.assert_not_called()

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
        # The single token-persist save commits after wiping the credentials.
        assert settings_persister.save_settings.call_count == 1

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
    def test_deletes_old_token_when_origin_matches(self, event_loop, romm_api, logger):
        """Same-server re-auth (#1038): the old token is revoked on its origin."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "http://romm.local"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        romm_api.delete_client_token.assert_called_once_with("u", "p", token_id=99)
        assert settings["romm_api_token_id"] == 42

    def test_origin_match_ignores_trailing_slash_and_default_port(self, event_loop, romm_api, logger):
        """Origin comparison folds path / default port — still the same server."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "https://romm.local"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("https://romm.local:443/romm/", "u", "p"))
        romm_api.delete_client_token.assert_called_once_with("u", "p", token_id=99)

    def test_skips_delete_when_origin_differs(self, event_loop, romm_api, logger):
        """#1038: an old token from a DIFFERENT origin is NOT replayed as a DELETE."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "https://old.server"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["success"] is True
        romm_api.delete_client_token.assert_not_called()
        # The new token still mints + persists.
        assert settings["romm_api_token"] == "rmm_minted"

    def test_skips_delete_when_old_origin_unknown(self, event_loop, romm_api, logger):
        """A legacy token with no stored origin is not DELETE-replayed against the new host."""
        settings = {"romm_api_token_id": 99}  # no romm_api_token_origin
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        romm_api.delete_client_token.assert_not_called()

    def test_no_delete_when_no_old_token(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        romm_api.delete_client_token.assert_not_called()

    def test_delete_failure_is_ignored(self, event_loop, romm_api, logger):
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "http://romm.local"}
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
        assert result == {"success": False, "reason": "config_error", "message": "No server URL configured"}
        romm_api.mint_client_token.assert_not_called()

    @pytest.mark.parametrize("bad_url", ["romm.local", "ftp://romm.local", "   ", "https://"])
    def test_invalid_url_returns_config_error_without_probing(self, event_loop, romm_api, logger, bad_url):
        """A scheme-less / non-http(s) / hostless URL is rejected before any network call (#1015)."""
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token(bad_url, "u", "p"))
        assert result == {"success": False, "reason": "config_error", "message": "Enter a valid http(s):// server URL"}
        romm_api.heartbeat.assert_not_called()
        romm_api.mint_client_token.assert_not_called()

    def test_url_is_trimmed_before_use(self, event_loop, romm_api, logger):
        """Surrounding whitespace is stripped; the trimmed URL is what gets persisted."""
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("  http://romm.local  ", "u", "p"))
        assert result["success"] is True
        assert settings["romm_url"] == "http://romm.local"
        assert settings["romm_api_token_origin"] == "http://romm.local"

    def test_unreachable_returns_connection_error_no_mint(self, event_loop, romm_api, logger):
        romm_api.heartbeat.side_effect = RommConnectionError("refused")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        romm_api.mint_client_token.assert_not_called()

    def test_version_too_old_returns_version_error_no_mint(self, event_loop, romm_api, logger):
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["reason"] == "version_error"
        romm_api.mint_client_token.assert_not_called()

    def test_forbidden_mint_returns_actionable_message(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.side_effect = RommForbiddenError("403")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "cannot create API tokens" in result["message"]

    def test_auth_error_mint_returns_auth_error(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.side_effect = RommAuthError("401")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"

    def test_missing_raw_token_returns_api_error(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.return_value = {"id": 42}  # no raw_token
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

    def test_missing_id_returns_api_error(self, event_loop, romm_api, logger):
        romm_api.mint_client_token.return_value = {"raw_token": "rmm_x"}  # no id
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

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
        assert result["reason"] == "unknown"
        assert "disk full" in result["message"]


def _working_settings() -> dict[str, Any]:
    """A settings dict already signed in against the OLD server."""
    return {
        "romm_url": "https://old.server",
        "romm_allow_insecure_ssl": False,
        "romm_api_token": "rmm_old",
        "romm_api_token_id": 7,
        "romm_api_token_origin": "https://old.server",
    }


class TestEstablishTokenSnapshotRestore:
    """#1015: a failed sign-in must not clobber the previous working state.

    Nothing is persisted before the mint succeeds, and the in-memory dict is
    rolled back to the previous URL + token on any failure.
    """

    def _assert_old_state_intact(self, settings: dict[str, Any]) -> None:
        assert settings["romm_url"] == "https://old.server"
        assert settings["romm_api_token"] == "rmm_old"
        assert settings["romm_api_token_id"] == 7
        assert settings["romm_api_token_origin"] == "https://old.server"

    def test_probe_failure_restores_old_state_and_never_saves(self, event_loop, romm_api, logger, settings_persister):
        romm_api.heartbeat.side_effect = RommConnectionError("refused")
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["success"] is False
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

    def test_version_gate_failure_restores_old_state_and_never_saves(
        self, event_loop, romm_api, logger, settings_persister
    ):
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["reason"] == "version_error"
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

    def test_mint_failure_restores_old_state_and_never_saves(self, event_loop, romm_api, logger, settings_persister):
        romm_api.mint_client_token.side_effect = RommForbiddenError("403")
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["reason"] == "auth_failed"
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

    def test_persist_failure_restores_old_state(self, event_loop, romm_api, logger, settings_persister):
        settings_persister.save_settings.side_effect = OSError("disk full")
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["success"] is False
        self._assert_old_state_intact(settings)

    def test_clears_old_token_before_probe(self, event_loop, romm_api, logger):
        """The version probe must run with NO bearer — the old token is cleared first (#1039)."""
        seen: dict[str, Any] = {}

        def _capture_heartbeat():
            seen["token_during_probe"] = settings.get("romm_api_token")
            seen["origin_during_probe"] = settings.get("romm_api_token_origin")
            return {"SYSTEM": {"VERSION": "4.8.1"}}

        romm_api.heartbeat.side_effect = _capture_heartbeat
        settings = _working_settings()
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert seen["token_during_probe"] is None
        assert seen["origin_during_probe"] is None

    def test_successful_signin_to_new_origin_stamps_and_persists_once(
        self, event_loop, romm_api, logger, settings_persister
    ):
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["success"] is True
        assert settings["romm_url"] == "https://new.server"
        assert settings["romm_api_token"] == "rmm_minted"
        assert settings["romm_api_token_id"] == 42
        assert settings["romm_api_token_origin"] == "https://new.server"
        settings_persister.save_settings.assert_called_once_with()


class TestMigrateLegacyCredentials:
    def test_mints_and_wipes_credentials(self, event_loop, romm_api, logger, settings_persister):
        settings = {"romm_url": "https://romm.local", "romm_user": "alice", "romm_pass": "secret"}
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
        # The origin is stamped from the configured URL at migration time.
        assert settings["romm_api_token_origin"] == "https://romm.local"
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


class TestProbeReachability:
    def test_heartbeat_ok_reports_online(self, event_loop, romm_api, logger):
        """A successful heartbeat → {"online": True}; no version gate, no persist."""
        settings: dict[str, Any] = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        result = event_loop.run_until_complete(service.probe_reachability())

        assert result == {"online": True}
        # Fast-fail path: the SINGLE-attempt probe is used, not the retrying heartbeat.
        romm_api.heartbeat_once.assert_called_once_with()
        romm_api.heartbeat.assert_not_called()
        # Pure connectivity probe — never asserts a version or writes state.
        romm_api.set_version.assert_not_called()

    def test_uses_single_attempt_probe_not_retrying_heartbeat(self, event_loop, romm_api, logger):
        """The probe drives ``heartbeat_once`` (one shot, short timeout) — never the
        retrying ``heartbeat`` that the version/sync flows use."""
        settings: dict[str, Any] = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        event_loop.run_until_complete(service.probe_reachability())

        assert romm_api.heartbeat_once.call_count == 1
        romm_api.heartbeat.assert_not_called()

    def test_heartbeat_raises_reports_offline(self, event_loop, romm_api, logger):
        """Any heartbeat exception → {"online": False}, never raises."""
        settings: dict[str, Any] = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat_once.side_effect = RommConnectionError("refused")
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        result = event_loop.run_until_complete(service.probe_reachability())

        assert result == {"online": False}
        romm_api.heartbeat_once.assert_called_once_with()

    def test_heartbeat_generic_exception_reports_offline_and_logs(self, event_loop, romm_api, logger, caplog):
        """A non-connection (code/wiring bug) exception still → {"online": False}, logged, never raises."""
        settings: dict[str, Any] = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat_once.side_effect = RuntimeError("heartbeat wiring bug")
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        with caplog.at_level(logging.DEBUG, logger="test_connection"):
            result = event_loop.run_until_complete(service.probe_reachability())

        assert result == {"online": False}
        romm_api.heartbeat_once.assert_called_once_with()
        # The swallow is diagnosable: a genuine bug is not silently lost.
        assert any("probe_reachability heartbeat failed" in r.message for r in caplog.records)
