"""Tests for ConnectionService."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from lib.errors import (
    PairingCodeInvalidError,
    PairingCodeOwnerDisabledError,
    PairingCodeRateLimitedError,
    PairingCodeTokenGoneError,
    RommAuthError,
    RommConnectionError,
    RommForbiddenError,
    RommServerError,
)
from services.connection import ConnectionService, ConnectionServiceConfig

_MIN_VERSION = (4, 9, 0)


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
    api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.0"}}
    api.list_platforms.return_value = [{"id": 1, "slug": "n64"}]
    api.mint_client_token.return_value = {"id": 42, "raw_token": "rmm_minted"}
    api.exchange_pairing_code.return_value = {"id": 99, "raw_token": "rmm_paired"}
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
    forget_device: MagicMock | None = None,
    clear_playtime_scope_notice: MagicMock | None = None,
) -> ConnectionService:
    return ConnectionService(
        config=ConnectionServiceConfig(
            settings=settings,
            romm_api=romm_api,
            settings_persister=settings_persister if settings_persister is not None else MagicMock(),
            loop=loop,
            logger=logger,
            min_required_version=min_required_version,
            forget_device=forget_device if forget_device is not None else MagicMock(),
            clear_playtime_scope_notice=(
                clear_playtime_scope_notice if clear_playtime_scope_notice is not None else MagicMock()
            ),
        ),
    )


class TestTestConnectionHappyPath:
    def test_returns_success_with_version(self, event_loop, romm_api, logger):
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert result["message"] == "Connected to RomM 4.9.0"
        assert result["romm_version"] == "4.9.0"
        romm_api.set_version.assert_called_once_with("4.9.0")

    def test_version_exact_minimum_succeeds(self, event_loop, romm_api, logger):
        """Version equal to minimum tuple is accepted (>= comparison)."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.0"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert result["romm_version"] == "4.9.0"


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
        assert "4.9.0" in result["message"]
        assert "4.5.0" in result["message"]

    def test_former_minimum_now_rejected(self, event_loop, romm_api, logger):
        """4.8.1 (the former minimum) is below 4.9.0 — must now be rejected."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["reason"] == "version_error"

    def test_prerelease_at_exact_floor_rejected(self, event_loop, romm_api, logger):
        """4.9.0-beta ranks below 4.9.0 — pre-release tags at the floor are rejected."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.0-beta.3"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is False
        assert result["reason"] == "version_error"
        assert result["romm_version"] == "4.9.0-beta.3"

    def test_prerelease_above_floor_accepted(self, event_loop, romm_api, logger):
        """4.9.1-beta has a core above 4.9.0 and passes the gate."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.1-beta"}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert result["romm_version"] == "4.9.1-beta"

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

    def test_heartbeat_with_non_string_version_normalized_to_none(self, event_loop, romm_api, logger):
        """A numeric (non-string) SYSTEM.VERSION is normalized to None at the boundary.

        The value is server-controlled: a malformed server sending numeric ``4.9``
        must not reach the version gate as a non-str. It is coerced to None
        (treated as absent) → gate bypassed → success, mirroring the
        SYSTEM-not-a-dict case, so downstream never sees a non-str.
        """
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": 4.9}}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.test_connection())
        assert result["success"] is True
        assert "romm_version" not in result
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
        assert result["romm_version"] == "4.9.0"
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

    def test_successful_sign_in_clears_playtime_scope_notice(self, event_loop, romm_api, logger):
        """A fresh mint carries roms.user.read, so the stale re-sign-in notice is cleared."""
        clear = MagicMock()
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            clear_playtime_scope_notice=clear,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is True
        clear.assert_called_once()

    def test_failed_sign_in_does_not_clear_playtime_scope_notice(self, event_loop, romm_api, logger):
        """A failed mint persists nothing and must not clear the notice."""
        romm_api.mint_client_token.side_effect = RommConnectionError("offline")
        clear = MagicMock()
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            clear_playtime_scope_notice=clear,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is False
        clear.assert_not_called()

    def test_sign_in_succeeds_even_if_scope_notice_clear_raises(self, event_loop, romm_api, logger):
        """The clear is best-effort: a failure never turns a successful sign-in into a failure."""
        clear = MagicMock(side_effect=RuntimeError("db locked"))
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            clear_playtime_scope_notice=clear,
        )
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "alice", "secret"))
        assert result["success"] is True


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


class TestEstablishTokenDeviceForget:
    """0a (#1234): a registered device id is bound to its minting origin, so a
    sign-in that GENUINELY changes origin must forget it (negotiate hard-404s a
    foreign device id). A same-server re-sign-in — including an unstamped
    (`None`) old origin against the unchanged URL — must KEEP the id (#1437), or
    the next post-exit sync flags a spurious conflict. The forget is local-only
    and best-effort on the success path."""

    def test_forgets_device_when_origin_differs(self, event_loop, romm_api, logger):
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "https://old.server"}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["success"] is True
        forget_device.assert_called_once_with()

    def test_keeps_device_when_origin_matches(self, event_loop, romm_api, logger):
        """Same-server re-auth keeps the device id — it is still valid there."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "http://romm.local"}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        forget_device.assert_not_called()

    def test_origin_match_ignores_trailing_slash_and_default_port(self, event_loop, romm_api, logger):
        """Origin folding (path / default port) keeps the device id, like the DELETE path."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "https://romm.local"}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_token("https://romm.local:443/romm/", "u", "p"))
        forget_device.assert_not_called()

    def test_keeps_device_when_old_origin_unknown(self, event_loop, romm_api, logger):
        """#1437: a legacy token with no stored origin is unknown, not different — keep the id.

        An unstamped (`None`) old origin against the unchanged URL must not
        forget; the sign-in then stamps the origin so the next comparison is
        precise.
        """
        settings = {"romm_api_token_id": 99}  # no romm_api_token_origin
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        forget_device.assert_not_called()
        assert settings["romm_api_token_origin"] == "http://romm.local"

    def test_keeps_device_on_first_sign_in(self, event_loop, romm_api, logger):
        """#1437: first-ever sign-in (no prior origin) is not an origin change — never forget."""
        settings: dict[str, Any] = {}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        forget_device.assert_not_called()

    def test_does_not_forget_on_failed_mint(self, event_loop, romm_api, logger):
        """A failed mint restores the snapshot — the still-current device id stays."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "https://old.server"}
        romm_api.mint_client_token.side_effect = RommConnectionError("offline")
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["success"] is False
        forget_device.assert_not_called()

    def test_does_not_forget_on_version_gate_failure(self, event_loop, romm_api, logger):
        """A below-minimum server is rejected before persist — device id untouched."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "https://old.server"}
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.0.0"}}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        assert result["success"] is False
        forget_device.assert_not_called()

    def test_forget_failure_does_not_fail_sign_in(self, event_loop, romm_api, logger):
        """Best-effort: a forget that raises must not turn a good sign-in into a failure."""
        settings = {"romm_api_token_id": 99, "romm_api_token_origin": "https://old.server"}
        forget_device = MagicMock(side_effect=RuntimeError("kv write failed"))
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))
        # The new token is already persisted; the local forget failure is swallowed.
        assert result["success"] is True
        assert settings["romm_api_token"] == "rmm_minted"
        forget_device.assert_called_once_with()


class TestEstablishTokenProvenance:
    """#1309: a minted token is stamped ``source="minted"``; a pasted user
    token (``source="user"``) is never DELETE-replayed on re-auth."""

    def test_persists_source_minted(self, event_loop, romm_api, logger, settings_persister):
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
        assert settings["romm_api_token_source"] == "minted"

    def test_skips_delete_when_stored_source_is_user(self, event_loop, romm_api, logger):
        """A previously pasted user token belongs to the user — never DELETE it on re-auth."""
        settings = {
            "romm_api_token_id": 99,
            "romm_api_token_origin": "http://romm.local",
            "romm_api_token_source": "user",
        }
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        assert result["success"] is True
        romm_api.delete_client_token.assert_not_called()
        # The freshly minted token replaces it and is stamped minted provenance.
        assert settings["romm_api_token"] == "rmm_minted"
        assert settings["romm_api_token_source"] == "minted"

    def test_still_deletes_minted_token_on_same_origin(self, event_loop, romm_api, logger):
        """A minted old token on the same origin is still revoked (#1038 path unchanged)."""
        settings = {
            "romm_api_token_id": 99,
            "romm_api_token_origin": "http://romm.local",
            "romm_api_token_source": "minted",
        }
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))
        romm_api.delete_client_token.assert_called_once_with("u", "p", token_id=99)


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


class TestEstablishUserTokenHappyPath:
    def test_stores_pasted_token_with_user_provenance(self, event_loop, romm_api, logger, settings_persister):
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_pasted"))
        assert result["success"] is True
        assert result["romm_version"] == "4.9.0"
        assert settings["romm_api_token"] == "rmm_pasted"
        # A pasted token carries no server-side id.
        assert settings["romm_api_token_id"] is None
        assert settings["romm_api_token_origin"] == "http://romm.local"
        assert settings["romm_api_token_source"] == "user"
        # We never mint or DELETE anything for a user-supplied token.
        romm_api.mint_client_token.assert_not_called()
        romm_api.delete_client_token.assert_not_called()
        # url + ssl + token + id + origin + source commit in a SINGLE atomic save.
        assert settings_persister.save_settings.call_count == 1

    def test_validates_token_with_authenticated_users_me_probe(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_pasted"))
        romm_api.get_current_user.assert_called_once_with()

    def test_trims_token_whitespace_before_use(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "  rmm_pasted  "))
        assert result["success"] is True
        assert settings["romm_api_token"] == "rmm_pasted"

    def test_persists_url_and_ssl_flag(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(
            service.establish_user_token("http://romm.local", "rmm_pasted", allow_insecure_ssl=True)
        )
        assert settings["romm_url"] == "http://romm.local"
        assert settings["romm_allow_insecure_ssl"] is True

    def test_never_logs_the_token_value(self, event_loop, romm_api, logger, caplog):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        with caplog.at_level(logging.DEBUG, logger="test_connection"):
            event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_supersecret"))
        assert all("rmm_supersecret" not in r.getMessage() for r in caplog.records)

    def test_keeps_device_on_first_sign_in(self, event_loop, romm_api, logger):
        """#1437: first-ever paste sign-in (no prior origin) is not a change — never forget."""
        settings: dict[str, Any] = {}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_pasted"))
        forget_device.assert_not_called()

    def test_keeps_device_on_same_server_token_swap(self, event_loop, romm_api, logger):
        """#1437: pasting a new token for the SAME server keeps the device id."""
        settings = {"romm_api_token_origin": "http://romm.local"}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_pasted"))
        forget_device.assert_not_called()

    def test_keeps_device_when_old_origin_unknown(self, event_loop, romm_api, logger):
        """#1437: an unstamped (`None`) old origin against the unchanged URL keeps the id."""
        settings = {"romm_api_token_id": None}  # no romm_api_token_origin
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_pasted"))
        forget_device.assert_not_called()
        assert settings["romm_api_token_origin"] == "http://romm.local"

    def test_forgets_device_when_origin_differs(self, event_loop, romm_api, logger):
        """A genuine server switch on the paste path still forgets the id (ADR-0016 0a)."""
        settings = {"romm_api_token_origin": "https://old.server"}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        result = event_loop.run_until_complete(service.establish_user_token("https://new.server", "rmm_pasted"))
        assert result["success"] is True
        forget_device.assert_called_once_with()

    def test_clears_playtime_scope_notice(self, event_loop, romm_api, logger):
        clear = MagicMock()
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            clear_playtime_scope_notice=clear,
        )
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_pasted"))
        assert result["success"] is True
        clear.assert_called_once()


class TestEstablishUserTokenBadPath:
    def test_empty_url_returns_config_error(self, event_loop, romm_api, logger):
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token("", "rmm_x"))
        assert result == {"success": False, "reason": "config_error", "message": "No server URL configured"}
        romm_api.heartbeat.assert_not_called()

    @pytest.mark.parametrize("bad_url", ["romm.local", "ftp://romm.local", "   ", "https://"])
    def test_invalid_url_returns_config_error_without_probing(self, event_loop, romm_api, logger, bad_url):
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token(bad_url, "rmm_x"))
        assert result == {"success": False, "reason": "config_error", "message": "Enter a valid http(s):// server URL"}
        romm_api.heartbeat.assert_not_called()
        romm_api.get_current_user.assert_not_called()

    @pytest.mark.parametrize("blank_token", ["", "   ", "\t\n"])
    def test_blank_token_returns_config_error_without_probing(self, event_loop, romm_api, logger, blank_token):
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", blank_token))
        assert result == {"success": False, "reason": "config_error", "message": "Enter your RomM API token"}
        romm_api.heartbeat.assert_not_called()
        romm_api.get_current_user.assert_not_called()

    def test_unreachable_server_returns_error_no_validation(self, event_loop, romm_api, logger):
        romm_api.heartbeat.side_effect = RommConnectionError("refused")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_x"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        romm_api.get_current_user.assert_not_called()

    def test_version_gate_failure_returns_version_error_no_validation(self, event_loop, romm_api, logger):
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_x"))
        assert result["success"] is False
        assert result["reason"] == "version_error"
        romm_api.get_current_user.assert_not_called()

    def test_invalid_token_401_returns_auth_failed(self, event_loop, romm_api, logger, settings_persister):
        romm_api.get_current_user.side_effect = RommAuthError("401")
        service = _make_service(
            settings={},
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_bad"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "invalid or has been revoked" in result["message"]
        settings_persister.save_settings.assert_not_called()

    def test_scope_403_returns_actionable_scope_message(self, event_loop, romm_api, logger):
        romm_api.get_current_user.side_effect = RommForbiddenError("403")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_readonly"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "scopes" in result["message"]

    def test_generic_validation_error_returns_error_response(self, event_loop, romm_api, logger):
        romm_api.get_current_user.side_effect = RommServerError("boom", status_code=500)
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_x"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

    def test_persist_failure_returns_error(self, event_loop, romm_api, logger, settings_persister):
        settings_persister.save_settings.side_effect = OSError("disk full")
        service = _make_service(
            settings={},
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_x"))
        assert result["success"] is False
        assert result["reason"] == "unknown"
        assert "disk full" in result["message"]


class TestEstablishUserTokenSnapshotRestore:
    """A failed pasted-token sign-in must not clobber the previous working state."""

    def _assert_old_state_intact(self, settings: dict[str, Any]) -> None:
        assert settings["romm_url"] == "https://old.server"
        assert settings["romm_api_token"] == "rmm_old"
        assert settings["romm_api_token_id"] == 7
        assert settings["romm_api_token_origin"] == "https://old.server"

    def test_invalid_token_restores_old_state_and_never_saves(self, event_loop, romm_api, logger, settings_persister):
        romm_api.get_current_user.side_effect = RommAuthError("401")
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_user_token("https://new.server", "rmm_bad"))
        assert result["success"] is False
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

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
        result = event_loop.run_until_complete(service.establish_user_token("https://new.server", "rmm_bad"))
        assert result["success"] is False
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

    def test_binds_pasted_token_to_candidate_origin_during_probe(self, event_loop, romm_api, logger):
        """The validation probe runs with the PASTED token bound to the candidate origin.

        The auth-header guard only attaches a token whose stored origin matches the
        current URL, so ``establish_user_token`` must stamp both before validating.
        """
        seen: dict[str, Any] = {}

        def _capture_get_current_user():
            seen["token"] = settings.get("romm_api_token")
            seen["origin"] = settings.get("romm_api_token_origin")
            return {"id": 1, "username": "tester"}

        romm_api.get_current_user.side_effect = _capture_get_current_user
        settings = _working_settings()
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_user_token("https://new.server", "rmm_pasted"))
        assert seen["token"] == "rmm_pasted"
        assert seen["origin"] == "https://new.server"


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
            return {"SYSTEM": {"VERSION": "4.9.0"}}

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
        # A credential-minted token carries the "minted" provenance (#1309).
        assert settings["romm_api_token_source"] == "minted"
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


class TestEstablishPairedTokenHappyPath:
    def test_exchanges_and_stores_token_with_user_provenance(self, event_loop, romm_api, logger, settings_persister):
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is True
        assert result["romm_version"] == "4.9.0"
        # A paired token is persisted exactly like a pasted one: user provenance,
        # no server-side id, and never minted or DELETEd.
        assert settings["romm_api_token"] == "rmm_paired"
        assert settings["romm_api_token_id"] is None
        assert settings["romm_api_token_origin"] == "http://romm.local"
        assert settings["romm_api_token_source"] == "user"
        romm_api.mint_client_token.assert_not_called()
        romm_api.delete_client_token.assert_not_called()
        assert settings_persister.save_settings.call_count == 1

    def test_validates_token_with_authenticated_users_me_probe(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        romm_api.get_current_user.assert_called_once_with()

    def test_normalizes_code_before_exchange(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ab-cd ef23"))
        romm_api.exchange_pairing_code.assert_called_once_with("ABCDEF23")

    def test_persists_url_and_ssl_flag(self, event_loop, romm_api, logger):
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(
            service.establish_paired_token("http://romm.local", "ABCD2345", allow_insecure_ssl=True)
        )
        assert settings["romm_url"] == "http://romm.local"
        assert settings["romm_allow_insecure_ssl"] is True

    def test_never_logs_the_code_or_token(self, event_loop, romm_api, logger, caplog):
        settings: dict[str, Any] = {}
        romm_api.exchange_pairing_code.return_value = {"id": 1, "raw_token": "rmm_supersecret"}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        with caplog.at_level(logging.DEBUG, logger="test_connection"):
            event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "SECRETCODE23"))
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "rmm_supersecret" not in messages
        assert "SECRETCODE23" not in messages

    def test_keeps_device_on_first_sign_in(self, event_loop, romm_api, logger):
        """#1437: first-ever paired sign-in (no prior origin) is not a change — never forget."""
        settings: dict[str, Any] = {}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        forget_device.assert_not_called()

    def test_keeps_device_on_same_server_re_pair(self, event_loop, romm_api, logger):
        """#1437: re-pairing against the SAME server keeps the device id."""
        settings = {"romm_api_token_origin": "http://romm.local"}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        forget_device.assert_not_called()

    def test_forgets_device_when_origin_differs(self, event_loop, romm_api, logger):
        """A genuine server switch on the pairing path still forgets the id (ADR-0016 0a)."""
        settings = {"romm_api_token_origin": "https://old.server"}
        forget_device = MagicMock()
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )
        result = event_loop.run_until_complete(service.establish_paired_token("https://new.server", "ABCD2345"))
        assert result["success"] is True
        forget_device.assert_called_once_with()

    def test_clears_playtime_scope_notice(self, event_loop, romm_api, logger):
        clear = MagicMock()
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            clear_playtime_scope_notice=clear,
        )
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is True
        clear.assert_called_once()


class TestEstablishPairedTokenBadPath:
    def test_empty_url_returns_config_error(self, event_loop, romm_api, logger):
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("", "ABCD2345"))
        assert result == {"success": False, "reason": "config_error", "message": "No server URL configured"}
        romm_api.heartbeat.assert_not_called()

    @pytest.mark.parametrize("bad_url", ["romm.local", "ftp://romm.local", "   ", "https://"])
    def test_invalid_url_returns_config_error_without_probing(self, event_loop, romm_api, logger, bad_url):
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token(bad_url, "ABCD2345"))
        assert result == {"success": False, "reason": "config_error", "message": "Enter a valid http(s):// server URL"}
        romm_api.heartbeat.assert_not_called()
        romm_api.exchange_pairing_code.assert_not_called()

    @pytest.mark.parametrize("blank_code", ["", "   ", "\t\n", "-", "- -"])
    def test_blank_code_returns_config_error_without_probing(self, event_loop, romm_api, logger, blank_code):
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", blank_code))
        assert result == {"success": False, "reason": "config_error", "message": "Enter the pairing code from RomM"}
        romm_api.heartbeat.assert_not_called()
        romm_api.exchange_pairing_code.assert_not_called()

    def test_unreachable_server_returns_error_no_exchange(self, event_loop, romm_api, logger):
        romm_api.heartbeat.side_effect = RommConnectionError("refused")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        romm_api.exchange_pairing_code.assert_not_called()

    def test_version_gate_failure_returns_version_error_no_exchange(self, event_loop, romm_api, logger):
        romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "version_error"
        romm_api.exchange_pairing_code.assert_not_called()

    def test_invalid_code_returns_auth_failed(self, event_loop, romm_api, logger, settings_persister):
        romm_api.exchange_pairing_code.side_effect = PairingCodeInvalidError("404")
        service = _make_service(
            settings={},
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "BADCODE1"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "invalid or has expired" in result["message"]
        # No validation probe runs after a failed exchange, and nothing persists.
        romm_api.get_current_user.assert_not_called()
        settings_persister.save_settings.assert_not_called()

    def test_token_gone_returns_auth_failed_with_own_message(self, event_loop, romm_api, logger):
        romm_api.exchange_pairing_code.side_effect = PairingCodeTokenGoneError("404")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "no longer exists" in result["message"]

    def test_owner_disabled_returns_auth_failed_with_own_message(self, event_loop, romm_api, logger):
        romm_api.exchange_pairing_code.side_effect = PairingCodeOwnerDisabledError("403")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "disabled" in result["message"]

    def test_rate_limited_returns_rate_limited_reason(self, event_loop, romm_api, logger):
        romm_api.exchange_pairing_code.side_effect = PairingCodeRateLimitedError("429")
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "rate_limited"
        assert "Too many attempts" in result["message"]

    def test_transport_failure_during_exchange_returns_error_response(self, event_loop, romm_api, logger):
        romm_api.exchange_pairing_code.side_effect = RommServerError("boom", status_code=500)
        service = _make_service(settings={}, romm_api=romm_api, loop=event_loop, logger=logger)
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

    def test_missing_raw_token_returns_error(self, event_loop, romm_api, logger, settings_persister):
        romm_api.exchange_pairing_code.return_value = {"id": 1}  # no raw_token
        service = _make_service(
            settings={},
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert "did not return a usable token" in result["message"]
        settings_persister.save_settings.assert_not_called()

    def test_users_me_401_after_exchange_returns_auth_failed(self, event_loop, romm_api, logger, settings_persister):
        # The exchanged token still runs through the shared /users/me validation:
        # a 401 there rejects it just like a pasted token.
        romm_api.get_current_user.side_effect = RommAuthError("401")
        service = _make_service(
            settings={},
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "invalid or has been revoked" in result["message"]
        settings_persister.save_settings.assert_not_called()


class TestEstablishPairedTokenSnapshotRestore:
    """A failed pairing-code sign-in must not clobber the previous working state."""

    def _assert_old_state_intact(self, settings: dict[str, Any]) -> None:
        assert settings["romm_url"] == "https://old.server"
        assert settings["romm_api_token"] == "rmm_old"
        assert settings["romm_api_token_id"] == 7
        assert settings["romm_api_token_origin"] == "https://old.server"

    def test_invalid_code_restores_old_state_and_never_saves(self, event_loop, romm_api, logger, settings_persister):
        romm_api.exchange_pairing_code.side_effect = PairingCodeInvalidError("404")
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_paired_token("https://new.server", "BADCODE1"))
        assert result["success"] is False
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

    def test_rate_limit_restores_old_state_and_never_saves(self, event_loop, romm_api, logger, settings_persister):
        romm_api.exchange_pairing_code.side_effect = PairingCodeRateLimitedError("429")
        settings = _working_settings()
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )
        result = event_loop.run_until_complete(service.establish_paired_token("https://new.server", "ABCD2345"))
        assert result["success"] is False
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

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
        result = event_loop.run_until_complete(service.establish_paired_token("https://new.server", "ABCD2345"))
        assert result["success"] is False
        self._assert_old_state_intact(settings)
        settings_persister.save_settings.assert_not_called()

    def test_binds_paired_token_to_candidate_origin_during_validation(self, event_loop, romm_api, logger):
        """The /users/me validation runs with the EXCHANGED token bound to the candidate origin."""
        seen: dict[str, Any] = {}

        def _capture_get_current_user():
            seen["token"] = settings.get("romm_api_token")
            seen["origin"] = settings.get("romm_api_token_origin")
            return {"id": 1, "username": "tester"}

        romm_api.exchange_pairing_code.return_value = {"id": 1, "raw_token": "rmm_paired"}
        romm_api.get_current_user.side_effect = _capture_get_current_user
        settings = _working_settings()
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)
        event_loop.run_until_complete(service.establish_paired_token("https://new.server", "ABCD2345"))
        assert seen["token"] == "rmm_paired"
        assert seen["origin"] == "https://new.server"


class TestSignOut:
    """``sign_out`` forgets the token locally — one atomic save, no server call."""

    def test_clears_token_keys_keeps_url_and_ssl_and_persists_once(
        self, event_loop, romm_api, logger, settings_persister
    ):
        settings = _working_settings()
        settings["romm_api_token_source"] = "minted"
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = service.sign_out()

        assert result["success"] is True
        assert "Signed out" in result["message"]
        # The four token keys are cleared to None.
        assert settings["romm_api_token"] is None
        assert settings["romm_api_token_id"] is None
        assert settings["romm_api_token_origin"] is None
        assert settings["romm_api_token_source"] is None
        # URL + SSL flag are kept for convenience.
        assert settings["romm_url"] == "https://old.server"
        assert settings["romm_allow_insecure_ssl"] is False
        # Persisted in exactly one atomic save; version cache cleared.
        settings_persister.save_settings.assert_called_once_with()
        romm_api.set_version.assert_called_once_with(None)

    def test_never_deletes_token_on_server(self, event_loop, romm_api, logger):
        settings = _working_settings()
        settings["romm_api_token_source"] = "minted"
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        service.sign_out()

        romm_api.delete_client_token.assert_not_called()

    def test_idempotent_when_already_signed_out(self, event_loop, romm_api, logger, settings_persister):
        """Signing out with no stored token still succeeds and persists cleanly."""
        settings: dict[str, Any] = {"romm_url": "https://old.server", "romm_allow_insecure_ssl": True}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = service.sign_out()

        assert result["success"] is True
        assert settings["romm_api_token"] is None
        assert settings["romm_url"] == "https://old.server"
        assert settings["romm_allow_insecure_ssl"] is True
        settings_persister.save_settings.assert_called_once_with()

    def test_mutates_settings_dict_in_place(self, event_loop, romm_api, logger):
        """The live settings dict identity is preserved (mutated, not rebound)."""
        settings = _working_settings()
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        service.sign_out()

        assert service._settings is settings

    def test_persist_failure_returns_canonical_failure_and_restores_keys(
        self, event_loop, romm_api, logger, settings_persister
    ):
        """A failed atomic save rolls the token quad back so the still-valid token survives."""
        settings = _working_settings()
        settings["romm_api_token_source"] = "minted"
        settings_persister.save_settings.side_effect = OSError("disk full")
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = service.sign_out()

        # Canonical failure shape (reason + message both present).
        assert result["success"] is False
        assert result["reason"] == "unknown"
        assert "disk full" in result["message"]
        # All four token keys restored in-memory — the token is still stored.
        assert settings["romm_api_token"] == "rmm_old"
        assert settings["romm_api_token_id"] == 7
        assert settings["romm_api_token_origin"] == "https://old.server"
        assert settings["romm_api_token_source"] == "minted"
        # The version cache is cleared only after a successful save.
        romm_api.set_version.assert_not_called()


class TestUserIdentityStamping:
    """romm_user_id lifecycle: stamped at sign-in, backfilled lazily, cleared on sign-out.

    The signed-in user's own id drives the collection owner-scope filter (#1532).
    It is bound to the token — re-derived on every sign-in, restored on a failed
    one, and forgotten on sign-out — so "Own" is never computed against a stale
    server's or a different user's identity, and degrades to "All" when unknown.
    """

    # ── Fresh sign-in stamps identity (AC1) ──────────────────────────────────

    def test_mint_sign_in_stamps_user_id(self, event_loop, romm_api, logger, settings_persister):
        romm_api.get_current_user.return_value = {"id": 3, "username": "alice"}
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
        assert settings["romm_user_id"] == 3
        # The identity rides the SINGLE atomic token-persist save (#1015 shape held).
        assert settings_persister.save_settings.call_count == 1

    def test_mint_sign_in_identity_probe_failure_leaves_id_none_but_succeeds(
        self, event_loop, romm_api, logger, settings_persister
    ):
        """A failed /api/users/me probe never fails the sign-in — id stays None (→ "Own" acts as "All")."""
        romm_api.get_current_user.side_effect = RommConnectionError("offline")
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
        assert settings["romm_user_id"] is None
        assert settings_persister.save_settings.call_count == 1

    def test_user_token_sign_in_stamps_user_id_from_validation_probe(
        self, event_loop, romm_api, logger, settings_persister
    ):
        romm_api.get_current_user.return_value = {"id": 9, "username": "bob"}
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = event_loop.run_until_complete(service.establish_user_token("http://romm.local", "rmm_pasted"))

        assert result["success"] is True
        assert settings["romm_user_id"] == 9
        # No SECOND /api/users/me call — the validation probe's result is reused.
        romm_api.get_current_user.assert_called_once_with()
        assert settings_persister.save_settings.call_count == 1

    def test_paired_token_sign_in_stamps_user_id(self, event_loop, romm_api, logger, settings_persister):
        romm_api.get_current_user.return_value = {"id": 11, "username": "carol"}
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = event_loop.run_until_complete(service.establish_paired_token("http://romm.local", "ABCD2345"))

        assert result["success"] is True
        assert settings["romm_user_id"] == 11
        assert settings_persister.save_settings.call_count == 1

    def test_malformed_identity_payload_leaves_id_none(self, event_loop, romm_api, logger):
        """A payload with no int id (or a bool) never becomes a wrong owner id."""
        romm_api.get_current_user.return_value = {"username": "no-id"}
        settings: dict[str, Any] = {}
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        event_loop.run_until_complete(service.establish_token("http://romm.local", "u", "p"))

        assert settings["romm_user_id"] is None

    # ── Origin change re-derives / clears identity (AC5) ─────────────────────

    def test_origin_change_refetches_user_id_from_new_server(self, event_loop, romm_api, logger):
        """Signing into a different server re-derives identity from THAT server."""
        romm_api.get_current_user.return_value = {"id": 2, "username": "new-server-user"}
        settings = _working_settings()
        settings["romm_user_id"] = 1  # the old server's identity
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))

        assert result["success"] is True
        assert settings["romm_user_id"] == 2

    def test_origin_change_with_probe_failure_clears_stale_id(self, event_loop, romm_api, logger):
        """On a server switch whose identity probe fails, the old id is cleared — never stale."""
        romm_api.get_current_user.side_effect = RommConnectionError("offline")
        settings = _working_settings()
        settings["romm_user_id"] = 1
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))

        assert result["success"] is True
        assert settings["romm_user_id"] is None

    def test_same_origin_different_user_rederives_identity(self, event_loop, romm_api, logger):
        """A DIFFERENT user signing in on the SAME server re-derives identity to THEM.

        The shared-server scenario the token-binding design exists for: user A
        signs out, user B signs in against the same origin. Here
        ``is_origin_change`` is False (the ``forget_device`` seam never fires,
        asserted below), so identity is bound to the TOKEN, not the origin — it
        re-derives on every sign-in. An origin-GATED re-derivation
        (``if is_origin_change(...)``) would leave A's id in place and pass every
        other identity test; this asserts B's id after the same-origin re-sign-in,
        so it fails the moment the re-derivation is wrongly origin-gated.
        """
        forget_device = MagicMock()
        romm_api.get_current_user.side_effect = [
            {"id": 101, "username": "user-a"},
            {"id": 202, "username": "user-b"},
        ]
        settings: dict[str, Any] = {}
        service = _make_service(
            settings=settings, romm_api=romm_api, loop=event_loop, logger=logger, forget_device=forget_device
        )

        # User A signs in on server X.
        result_a = event_loop.run_until_complete(service.establish_token("http://server.x", "user-a", "pw"))
        assert result_a["success"] is True
        assert settings["romm_user_id"] == 101

        # User B signs in on the SAME server X — same origin, no server switch.
        result_b = event_loop.run_until_complete(service.establish_token("http://server.x", "user-b", "pw"))
        assert result_b["success"] is True
        # Identity re-derived to B — NOT left at A's 101 — even though the origin
        # never changed (proven by the untouched device-forget seam).
        assert settings["romm_user_id"] == 202
        forget_device.assert_not_called()

    def test_failed_sign_in_restores_previous_user_id(self, event_loop, romm_api, logger):
        """A failed sign-in must not wipe the still-valid current identity."""
        romm_api.mint_client_token.side_effect = RommConnectionError("offline")
        settings = _working_settings()
        settings["romm_user_id"] = 5
        service = _make_service(settings=settings, romm_api=romm_api, loop=event_loop, logger=logger)

        result = event_loop.run_until_complete(service.establish_token("https://new.server", "u", "p"))

        assert result["success"] is False
        assert settings["romm_user_id"] == 5

    # ── Sign-out forgets identity ────────────────────────────────────────────

    def test_sign_out_clears_user_id(self, event_loop, romm_api, logger, settings_persister):
        settings = _working_settings()
        settings["romm_user_id"] = 5
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        service.sign_out()

        assert settings["romm_user_id"] is None
        # Still exactly one atomic save — identity clears alongside the token quad.
        settings_persister.save_settings.assert_called_once_with()

    # ── Lazy backfill on a connection check (AC1) ────────────────────────────

    def test_test_connection_backfills_missing_user_id(self, event_loop, romm_api, logger, settings_persister):
        """An existing session with a token but no stored id backfills it — no re-login."""
        romm_api.get_current_user.return_value = {"id": 4, "username": "existing"}
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = event_loop.run_until_complete(service.test_connection())

        assert result["success"] is True
        assert settings["romm_user_id"] == 4
        settings_persister.save_settings.assert_called_once_with()

    def test_test_connection_skips_backfill_when_user_id_known(self, event_loop, romm_api, logger, settings_persister):
        """A known id needs no network and no save on every connection check."""
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token", "romm_user_id": 4}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = event_loop.run_until_complete(service.test_connection())

        assert result["success"] is True
        assert settings["romm_user_id"] == 4
        romm_api.get_current_user.assert_not_called()
        settings_persister.save_settings.assert_not_called()

    def test_test_connection_backfill_failure_is_best_effort(self, event_loop, romm_api, logger, settings_persister):
        """A backfill probe failure leaves the id unknown and never fails the connection check."""
        romm_api.get_current_user.side_effect = RommConnectionError("offline")
        settings = {"romm_url": "http://romm.local", "romm_api_token": "rmm_token"}
        service = _make_service(
            settings=settings,
            romm_api=romm_api,
            loop=event_loop,
            logger=logger,
            settings_persister=settings_persister,
        )

        result = event_loop.run_until_complete(service.test_connection())

        assert result["success"] is True
        assert settings.get("romm_user_id") is None
        settings_persister.save_settings.assert_not_called()
