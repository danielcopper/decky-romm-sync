import asyncio
import http.client
import io
import json
import ssl
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_renderer_gc import FakeRendererGc
from fakes.fake_renderer_rss import FakeRendererRss
from fakes.fake_unit_of_work import FakeUnitOfWorkFactory
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.romm.http import RommHttpAdapter
from adapters.steam_config import SteamConfigAdapter
from lib.errors import (
    RommApiError,
    RommAuthError,
    RommConflictError,
    RommConnectionError,
    RommForbiddenError,
    RommNotFoundError,
    RommServerError,
    RommSSLError,
    RommTimeoutError,
    RommUnprocessableEntityError,
    TokenHostMismatchError,
    classify_error,
)
from lib.list_result import ErrorCode

# conftest.py patches decky before this import
from main import Plugin
from services.connection import ConnectionService, ConnectionServiceConfig
from services.library import LibraryService, LibraryServiceConfig


def _http_error(
    code: int,
    reason: str,
    *,
    content_type: str | None = None,
    body: bytes | None = None,
    url: str = "http://romm.local/api/roms/4375",
) -> urllib.error.HTTPError:
    """Build an ``HTTPError`` the way a real response arrives: headers plus a readable body."""
    hdrs = http.client.HTTPMessage()
    if content_type is not None:
        hdrs["Content-Type"] = content_type
    return urllib.error.HTTPError(url, code, reason, hdrs, io.BytesIO(body) if body is not None else None)


def _entity_404(detail: str = "Rom with id '4375' not found") -> urllib.error.HTTPError:
    """RomM's entity layer answering "this entity does not exist"."""
    return _http_error(404, "Not Found", content_type="application/json", body=json.dumps({"detail": detail}).encode())


@pytest.fixture
def plugin():
    import logging

    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    import decky

    p._http_adapter = RommHttpAdapter(
        p.settings, decky.DECKY_PLUGIN_DIR, logging.getLogger("test"), "decky-romm-sync/9.9.9"
    )
    p._romm_api = MagicMock()
    p._prune_service = MagicMock()
    p._prune_service.is_active.return_value = False

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

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
            settings_persister=MagicMock(),
            log_debug=p._log_debug,
            artwork=FakeArtworkManager(),
            uow_factory=FakeUnitOfWorkFactory(),
            active_core=FakeActiveCoreResolver(default=(None, None)),
            disc_resolver=FakeDiscResolver(),
            renderer_rss=FakeRendererRss(),
            renderer_gc=FakeRendererGc(),
        ),
    )

    p._connection_service = ConnectionService(
        config=ConnectionServiceConfig(
            settings=p.settings,
            romm_api=p._romm_api,
            settings_persister=MagicMock(),
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            min_required_version=Plugin._MIN_REQUIRED_VERSION,
            forget_device=MagicMock(),
            clear_playtime_scope_notice=MagicMock(),
        ),
    )
    return p


class TestResolveSystem:
    def test_exact_slug_match(self, plugin):
        result = plugin._http_adapter.resolve_system("n64")
        assert result == "n64"

    def test_fs_slug_fallback(self, plugin):
        # A slug not in the map but its fs_slug is
        result = plugin._http_adapter.resolve_system("nonexistent-slug", "n64")
        assert result == "n64"

    def test_fallback_returns_slug_as_is(self, plugin):
        result = plugin._http_adapter.resolve_system("totally-unknown-platform")
        assert result == "totally-unknown-platform"


class TestRommDownloadUrlEncoding:
    def test_encodes_spaces_in_cover_path(self, plugin, tmp_path):
        """Cover paths from RomM contain unencoded spaces in timestamps.
        _romm_download must URL-encode them so urllib doesn't reject the URL."""
        import urllib.parse

        # Simulate the path RomM returns
        path = "/assets/romm/resources/roms/53/4375/cover/big.png?ts=2025-07-28 00:05:03"
        encoded = urllib.parse.quote(path, safe="/:?=&@")
        assert " " not in encoded
        assert "%20" in encoded
        assert encoded == "/assets/romm/resources/roms/53/4375/cover/big.png?ts=2025-07-28%2000:05:03"

    def test_preserves_clean_paths(self, plugin):
        """Paths without spaces should pass through unchanged."""
        import urllib.parse

        path = "/assets/romm/resources/roms/53/4375/cover/big.png"
        encoded = urllib.parse.quote(path, safe="/:?=&@")
        assert encoded == path


class TestRommSslContext:
    def test_default_verifies_ssl(self, plugin):
        """Default setting (False) should produce a context that verifies certs."""
        import ssl

        plugin.settings["romm_allow_insecure_ssl"] = False
        ctx = plugin._http_adapter.ssl_context()
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_insecure_disables_verification(self, plugin):
        """When romm_allow_insecure_ssl=True, certs should not be verified."""
        import ssl

        plugin.settings["romm_allow_insecure_ssl"] = True
        ctx = plugin._http_adapter.ssl_context()
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    def test_missing_setting_defaults_secure(self, plugin):
        """Missing setting should default to secure."""
        import ssl

        plugin.settings.pop("romm_allow_insecure_ssl", None)
        ctx = plugin._http_adapter.ssl_context()
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED


class TestRommAuthHeader:
    def test_bearer_format_with_token(self, plugin):
        plugin.settings["romm_api_token"] = "rmm_abc123"
        header = plugin._http_adapter.auth_header()
        assert header == "Bearer rmm_abc123"

    def test_returns_none_when_no_token(self, plugin):
        plugin.settings.pop("romm_api_token", None)
        header = plugin._http_adapter.auth_header()
        assert header is None

    def test_returns_none_when_token_none(self, plugin):
        plugin.settings["romm_api_token"] = None
        header = plugin._http_adapter.auth_header()
        assert header is None

    def test_attaches_token_when_origin_none_legacy(self, plugin):
        """A token minted before host-binding (origin None) is still attached — never blocked."""
        plugin.settings["romm_url"] = "https://romm.local"
        plugin.settings["romm_api_token"] = "rmm_legacy"
        plugin.settings["romm_api_token_origin"] = None
        assert plugin._http_adapter.auth_header() == "Bearer rmm_legacy"

    def test_attaches_token_when_origin_matches(self, plugin):
        plugin.settings["romm_url"] = "https://romm.local"
        plugin.settings["romm_api_token"] = "rmm_bound"
        plugin.settings["romm_api_token_origin"] = "https://romm.local"
        assert plugin._http_adapter.auth_header() == "Bearer rmm_bound"

    def test_origin_match_folds_default_port_and_path(self, plugin):
        """The bound origin equals the current URL even with a default port / path."""
        plugin.settings["romm_url"] = "https://romm.local:443/romm/"
        plugin.settings["romm_api_token"] = "rmm_bound"
        plugin.settings["romm_api_token_origin"] = "https://romm.local"
        assert plugin._http_adapter.auth_header() == "Bearer rmm_bound"

    def test_raises_on_origin_mismatch(self, plugin):
        """#1039: the bearer is never sent to a host the token was not minted for."""
        plugin.settings["romm_url"] = "https://evil.host"
        plugin.settings["romm_api_token"] = "rmm_bound"
        plugin.settings["romm_api_token_origin"] = "https://romm.local"
        with pytest.raises(TokenHostMismatchError):
            plugin._http_adapter.auth_header()

    def test_raises_on_scheme_downgrade(self, plugin):
        """http vs https are different origins — a downgrade is a mismatch."""
        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_bound"
        plugin.settings["romm_api_token_origin"] = "https://romm.local"
        with pytest.raises(TokenHostMismatchError):
            plugin._http_adapter.auth_header()

    def test_no_raise_without_token_even_if_origin_set(self, plugin):
        """No token → no header, no raise (the guard only applies when a token exists)."""
        plugin.settings["romm_url"] = "https://evil.host"
        plugin.settings.pop("romm_api_token", None)
        plugin.settings["romm_api_token_origin"] = "https://romm.local"
        assert plugin._http_adapter.auth_header() is None


class TestTokenHostMismatchRetry:
    def test_token_host_mismatch_is_not_retryable(self):
        """A wrong-origin token can never succeed by retrying — must stay non-retryable."""
        assert RommHttpAdapter.is_retryable(TokenHostMismatchError("mismatch")) is False


class TestWithRetryOnRetryListener:
    """The optional ``on_retry`` listener fires once per retry so the saves UI
    can surface "Connecting to RomM… (attempt N/M)" progress (#1345)."""

    def _adapter(self, on_retry=None):
        import logging

        return RommHttpAdapter(
            {"romm_url": ""},
            "/tmp",
            logging.getLogger("test"),
            "decky-romm-sync/9.9.9",
            on_retry=on_retry,
        )

    def test_fires_per_retry_with_1_based_attempt_numbers(self):
        calls: list[tuple[int, int, float]] = []
        adapter = self._adapter(on_retry=lambda a, m, d: calls.append((a, m, d)))
        tries = {"n": 0}

        def fn():
            tries["n"] += 1
            raise ConnectionError("server down")

        with patch("adapters.romm.http.time.sleep") as sleep_mock, pytest.raises(ConnectionError):
            adapter.with_retry(fn, max_attempts=3)

        # 3 attempts total; the listener fires just before each of the 2 backoff
        # sleeps, naming the retry about to run (attempt 2/3, then 3/3) and its delay.
        assert calls == [(2, 3, 1.0), (3, 3, 3.0)]
        assert sleep_mock.call_count == 2
        assert tries["n"] == 3

    def test_not_fired_when_first_attempt_succeeds(self):
        calls: list[tuple[int, int, float]] = []
        adapter = self._adapter(on_retry=lambda a, m, d: calls.append((a, m, d)))
        assert adapter.with_retry(lambda: "ok") == "ok"
        assert calls == []

    def test_not_fired_on_non_retryable_error(self):
        calls: list[tuple[int, int, float]] = []
        adapter = self._adapter(on_retry=lambda a, m, d: calls.append((a, m, d)))

        def fn():
            raise ValueError("bad request")  # non-retryable — raises immediately

        with pytest.raises(ValueError):
            adapter.with_retry(fn, max_attempts=3)
        assert calls == []

    def test_listener_exception_never_breaks_the_retry(self):
        # A raising listener (e.g. a closed loop at plugin unload) must be
        # swallowed so it can't abort the real HTTP retry underway.
        def boom(*_a):
            raise RuntimeError("loop closed")

        adapter = self._adapter(on_retry=boom)
        tries = {"n": 0}

        def fn():
            tries["n"] += 1
            if tries["n"] < 2:
                raise ConnectionError("server down")
            return "recovered"

        with patch("adapters.romm.http.time.sleep"):
            assert adapter.with_retry(fn, max_attempts=3) == "recovered"

    def test_none_listener_is_a_noop_across_a_retry(self):
        adapter = self._adapter(on_retry=None)
        tries = {"n": 0}

        def fn():
            tries["n"] += 1
            if tries["n"] < 2:
                raise ConnectionError("server down")
            return "ok"

        with patch("adapters.romm.http.time.sleep"):
            assert adapter.with_retry(fn, max_attempts=3) == "ok"


class TestRommBasicAuthRequest:
    """``basic_auth_request`` builds a one-off Basic header from passed creds."""

    def _staged_resp(self, payload: bytes, status: int = 200):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status = status
        resp.read.return_value = payload
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_uses_passed_credentials_not_settings(self, plugin):
        import base64
        import json as _json
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_stored"  # must be ignored
        resp = self._staged_resp(_json.dumps({"id": 7, "raw_token": "rmm_new"}).encode())

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = plugin._http_adapter.basic_auth_request(
                "/api/client-tokens", "alice", "s3cret", method="POST", data={"name": "x"}
            )

        assert result == {"id": 7, "raw_token": "rmm_new"}
        req = mock_open.call_args[0][0]
        auth = req.get_header("Authorization")
        assert auth.startswith("Basic ")
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        assert decoded == "alice:s3cret"
        assert "rmm_stored" not in auth

    def test_posts_json_body_with_content_type(self, plugin):
        import json as _json
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        resp = self._staged_resp(_json.dumps({"id": 1}).encode())

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            plugin._http_adapter.basic_auth_request(
                "/api/client-tokens", "u", "p", method="POST", data={"name": "deck", "scopes": ["me.read"]}
            )

        req = mock_open.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/json"
        assert _json.loads(req.data.decode()) == {"name": "deck", "scopes": ["me.read"]}

    def test_no_body_when_data_none(self, plugin):
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        resp = self._staged_resp(b"", status=204)

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = plugin._http_adapter.basic_auth_request("/api/client-tokens/5", "u", "p", method="DELETE")

        assert result == {}
        req = mock_open.call_args[0][0]
        assert req.data is None
        assert req.get_method() == "DELETE"
        # No JSON content-type header when there is no body.
        assert req.get_header("Content-type") is None

    def test_returns_empty_dict_on_204(self, plugin):
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        resp = self._staged_resp(b"", status=204)

        with patch("urllib.request.urlopen", return_value=resp):
            result = plugin._http_adapter.basic_auth_request("/api/client-tokens/5", "u", "p", method="DELETE")

        assert result == {}

    def test_sends_user_agent(self, plugin):
        import json as _json
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        resp = self._staged_resp(_json.dumps({"id": 1, "raw_token": "rmm_x"}).encode())

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            plugin._http_adapter.basic_auth_request("/api/client-tokens", "u", "p", method="POST", data={"name": "x"})

        req = mock_open.call_args[0][0]
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_does_not_retry_on_server_error(self, plugin):
        """Mint/delete are not retry-safe — a 500 raises immediately, no retry."""
        import http.client
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        exc = urllib.error.HTTPError(
            "http://romm.local/api/client-tokens", 500, "Server Error", http.client.HTTPMessage(), None
        )

        with patch("urllib.request.urlopen", side_effect=exc) as mock_open, pytest.raises(RommServerError):
            plugin._http_adapter.basic_auth_request("/api/client-tokens", "u", "p", method="POST", data={"name": "x"})

        # Single attempt — with_retry would have retried a 500 three times.
        assert mock_open.call_count == 1

    def test_translates_403_to_forbidden(self, plugin):
        import http.client
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        exc = urllib.error.HTTPError(
            "http://romm.local/api/client-tokens", 403, "Forbidden", http.client.HTTPMessage(), None
        )

        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommForbiddenError):
            plugin._http_adapter.basic_auth_request("/api/client-tokens", "u", "p", method="POST", data={"name": "x"})


class TestUnauthenticatedPostJson:
    """``unauthenticated_post_json`` POSTs to a public endpoint with no bearer, no retry."""

    _ENDPOINT = "/api/client-tokens/exchange"

    def _staged_resp(self, payload: bytes, status: int = 200):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = payload
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_omits_authorization_even_with_stored_token(self, plugin):
        # A stored bearer must NEVER go to the public exchange endpoint.
        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_stored"
        plugin.settings["romm_api_token_origin"] = "http://romm.local"
        resp = self._staged_resp(json.dumps({"raw_token": "rmm_paired"}).encode())

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = plugin._http_adapter.unauthenticated_post_json(self._ENDPOINT, {"code": "ABCD2345"})

        assert result == {"raw_token": "rmm_paired"}
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") is None
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_method() == "POST"
        assert json.loads(req.data.decode()) == {"code": "ABCD2345"}

    def test_returns_empty_dict_on_204(self, plugin):
        plugin.settings["romm_url"] = "http://romm.local"
        resp = self._staged_resp(b"", status=204)
        with patch("urllib.request.urlopen", return_value=resp):
            result = plugin._http_adapter.unauthenticated_post_json(self._ENDPOINT, {"code": "X"})
        assert result == {}

    def test_attaches_detail_on_error(self, plugin):
        # The 404 body's ``detail`` must ride along on the typed error so the
        # adapter can distinguish the two 404s the exchange returns.
        plugin.settings["romm_url"] = "http://romm.local"
        exc = _http_error(
            404,
            "Not Found",
            content_type="application/json",
            body=json.dumps({"detail": "Token no longer exists"}).encode(),
            url="http://romm.local/api/client-tokens/exchange",
        )
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommNotFoundError) as exc_info:
            plugin._http_adapter.unauthenticated_post_json(self._ENDPOINT, {"code": "X"})
        assert exc_info.value.detail == "Token no longer exists"

    def test_maps_429_to_server_error(self, plugin):
        plugin.settings["romm_url"] = "http://romm.local"
        exc = urllib.error.HTTPError(
            "http://romm.local/api/client-tokens/exchange", 429, "Too Many Requests", http.client.HTTPMessage(), None
        )
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommServerError) as exc_info:
            plugin._http_adapter.unauthenticated_post_json(self._ENDPOINT, {"code": "X"})
        assert exc_info.value.status_code == 429

    def test_does_not_retry(self, plugin):
        # No with_retry — a single-use credential must not be replayed.
        plugin.settings["romm_url"] = "http://romm.local"
        exc = urllib.error.HTTPError(
            "http://romm.local/api/client-tokens/exchange", 500, "Server Error", http.client.HTTPMessage(), None
        )
        with patch("urllib.request.urlopen", side_effect=exc) as mock_open, pytest.raises(RommServerError):
            plugin._http_adapter.unauthenticated_post_json(self._ENDPOINT, {"code": "X"})
        assert mock_open.call_count == 1

    def test_transport_failure_maps_to_connection_error(self, plugin):
        plugin.settings["romm_url"] = "http://romm.local"
        with (
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")),
            pytest.raises(RommConnectionError),
        ):
            plugin._http_adapter.unauthenticated_post_json(self._ENDPOINT, {"code": "X"})


class TestRommRequest:
    def test_uses_auth_header(self, plugin):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"ok": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            result = plugin._http_adapter.request("/api/test")

        assert result == {"ok": True}
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer rmm_runtime"

    def test_sends_user_agent(self, plugin):
        """GET requests carry the injected ``User-Agent`` so Cloudflare Bot
        Fight Mode does not 403 self-hosted RomM behind a tunnel (#249)."""
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"ok": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            plugin._http_adapter.request("/api/test")

        req = mock_open.call_args[0][0]
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_omits_authorization_when_no_token(self, plugin):
        """A pre-mint probe (no stored token) must not send an empty ``Bearer ``
        header — some RomM versions 500 on it. The User-Agent is still sent."""
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings.pop("romm_api_token", None)
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"ok": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            plugin._http_adapter.request("/api/test")

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") is None
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_sends_authorization_when_token_present(self, plugin):
        """With a stored token, the Bearer header is sent alongside the UA."""
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"ok": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            plugin._http_adapter.request("/api/test")

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer rmm_runtime"
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"


class TestRommRequestOnce:
    def test_single_attempt_short_timeout(self, plugin):
        """``request_once`` fires ONE urlopen with the passed (short) timeout —
        no with_retry wrapper, unlike ``request``."""
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"ok": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            result = plugin._http_adapter.request_once("/api/heartbeat", timeout=3)

        assert result == {"ok": True}
        assert mock_open.call_count == 1
        # The short timeout is threaded through to urlopen, not the 30s default.
        assert mock_open.call_args.kwargs["timeout"] == 3

    def test_does_not_retry_on_transient_error(self, plugin):
        """A retryable transport error from ``request_once`` raises immediately —
        a SINGLE attempt, no exponential backoff (``request`` would retry 3x)."""
        from unittest.mock import patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        with (
            patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as mock_open,
            pytest.raises(RommTimeoutError),
        ):
            plugin._http_adapter.request_once("/api/heartbeat", timeout=3)

        assert mock_open.call_count == 1


class TestRommJsonRequest:
    def test_post_json(self, plugin):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"id": 1}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            result = plugin._http_adapter.post_json("/api/saves", {"filename": "test.srm"})

        assert result == {"id": 1}
        req = mock_open.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == "Bearer rmm_runtime"
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_put_json(self, plugin):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"id": 1}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            plugin._http_adapter.put_json("/api/saves/1", {"filename": "test.srm"})

        req = mock_open.call_args[0][0]
        assert req.get_method() == "PUT"

    def test_omits_authorization_when_no_token(self, plugin):
        """A token-less JSON request omits the Authorization header (no empty Bearer)."""
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings.pop("romm_api_token", None)
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"id": 1}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            plugin._http_adapter.post_json("/api/saves", {"filename": "test.srm"})

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") is None
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_post_json_attaches_detail_from_400(self, plugin):
        """A 400 with a JSON ``{"detail": ...}`` body surfaces the detail on the raised error (#1489)."""
        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_allow_insecure_ssl"] = False

        body = json.dumps({"detail": "Sync is disabled for this device"}).encode()
        exc = urllib.error.HTTPError(
            "http://romm.local/api/sync/negotiate", 400, "Bad Request", http.client.HTTPMessage(), io.BytesIO(body)
        )
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommApiError) as exc_info:
            plugin._http_adapter.post_json("/api/sync/negotiate", {"device_id": "d"})
        assert exc_info.value.detail == "Sync is disabled for this device"

    def test_post_json_non_json_body_degrades_detail_to_none(self, plugin):
        """A 400 whose body is not JSON degrades to ``detail=None`` (no crash, #1489)."""
        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_allow_insecure_ssl"] = False

        exc = urllib.error.HTTPError(
            "http://romm.local/api/sync/negotiate",
            400,
            "Bad Request",
            http.client.HTTPMessage(),
            io.BytesIO(b"<html>nope</html>"),
        )
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommApiError) as exc_info:
            plugin._http_adapter.post_json("/api/sync/negotiate", {"device_id": "d"})
        assert exc_info.value.detail is None


class TestRommUploadMultipart:
    def test_upload_sends_multipart(self, plugin, tmp_path):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_api_token"] = "rmm_runtime"
        plugin.settings["romm_allow_insecure_ssl"] = False

        save_file = tmp_path / "test.srm"
        save_file.write_bytes(b"save data here")

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"id": 42}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            result = plugin._http_adapter.upload_multipart("/api/saves", str(save_file))

        assert result == {"id": 42}
        req = mock_open.call_args[0][0]
        assert "multipart/form-data" in req.get_header("Content-type")
        assert b"save data here" in req.data
        assert req.get_header("Authorization") == "Bearer rmm_runtime"
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_upload_strips_control_chars_from_filename(self, plugin, tmp_path):
        """Filenames with CRLF/null bytes must not inject multipart headers."""
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_user"] = "user"
        plugin.settings["romm_pass"] = "pass"
        plugin.settings["romm_allow_insecure_ssl"] = False

        # Create a file whose basename contains injected control chars
        evil_name = "evil\r\nInjected-Header: bad\0.srm"
        safe_dir = tmp_path / "sub"
        safe_dir.mkdir()
        # We can't create a file with \r\n\0 in the name on most FS,
        # so patch os.path.basename to return the evil name.
        save_file = safe_dir / "normal.srm"
        save_file.write_bytes(b"data")

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"id": 1}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=fake_resp) as mock_open,
            patch("os.path.basename", return_value=evil_name),
        ):
            plugin._http_adapter.upload_multipart("/api/saves", str(save_file))

        req = mock_open.call_args[0][0]
        body = req.data
        # Control characters must be stripped from the Content-Disposition header
        assert b"\r\nInjected-Header:" not in body
        assert b"\0" not in body.split(b"\r\n\r\n")[0]  # not in headers
        # The sanitized filename should still appear
        assert b'filename="evilInjected-Header: bad.srm"' in body


class TestPlatformMap:
    def test_loads_config_json(self, plugin):
        pm = plugin._http_adapter.load_platform_map()
        assert isinstance(pm, dict)
        assert "n64" in pm
        assert "snes" in pm
        assert len(pm) > 50  # Should have many entries

    def test_both_romm_3ds_platforms_resolve_to_one_system(self, plugin):
        """RomM carries "3ds" and "new-nintendo-3ds" as separate platforms.

        RetroDECK has one 3DS system for both, so the map must collapse them.
        Without the second key ``resolve_system`` passes it through verbatim
        (ADR-0010 §5) and the download lands in a folder ES-DE never scans
        (#1678).
        """
        adapter = plugin._http_adapter
        assert adapter.resolve_system("3ds") == "n3ds"
        assert adapter.resolve_system("new-nintendo-3ds") == "n3ds"

    def test_short_romm_slugs_resolve_to_their_es_de_system(self, plugin):
        """Three RomM slugs whose ES-DE system is spelled differently.

        Unmapped, ``resolve_system`` passes each through verbatim (ADR-0010 §5)
        and the download lands in a folder ES-DE never scans. RomM's
        ``atari8bit`` covers the whole 8-bit line, which RetroDECK serves from
        ``atari800`` (``atarixe`` is the console variant).
        """
        adapter = plugin._http_adapter
        assert adapter.resolve_system("atari8bit") == "atari800"
        assert adapter.resolve_system("mac") == "macintosh"
        assert adapter.resolve_system("sega32") == "sega32x"

    def test_missing_config_returns_empty_map(self, tmp_path):
        """A plugin_dir with no config.json degrades to an empty map, not an error.

        ``resolve_system`` then falls back to its verbatim pass-through (ADR-0010
        §5) rather than raising into the synchronous game-detail builder.
        """
        import logging

        adapter = RommHttpAdapter({}, str(tmp_path), logging.getLogger("test"), "decky-romm-sync/9.9.9")
        assert adapter.load_platform_map() == {}
        # resolve_system survives the empty map and passes the slug through unchanged.
        assert adapter.resolve_system("dc") == "dc"

    def test_corrupt_config_returns_empty_map(self, tmp_path):
        """A corrupt (non-JSON) config.json degrades to an empty map, not an error."""
        import logging

        (tmp_path / "config.json").write_text("{ this is not valid json")
        adapter = RommHttpAdapter({}, str(tmp_path), logging.getLogger("test"), "decky-romm-sync/9.9.9")
        assert adapter.load_platform_map() == {}
        assert adapter.resolve_system("dc") == "dc"


# ============================================================================
# _translate_http_error
# ============================================================================


def _setup_plugin(plugin):
    """Configure plugin with valid settings for HTTP tests.

    Also rebuilds the connection service on the live event loop so
    executor calls dispatch on the loop the test awaits — pytest-asyncio
    creates a fresh loop per test in auto mode, so the loop the fixture
    captured at setup time is not the loop the test runs on.
    """
    import decky

    plugin.settings["romm_url"] = "http://romm.local"
    plugin.settings["romm_user"] = "user"
    plugin.settings["romm_pass"] = "pass"
    plugin.settings["romm_api_token"] = "rmm_token"
    plugin.settings["romm_allow_insecure_ssl"] = False
    plugin.loop = asyncio.get_event_loop()
    plugin._connection_service = ConnectionService(
        config=ConnectionServiceConfig(
            settings=plugin.settings,
            romm_api=plugin._romm_api,
            settings_persister=MagicMock(),
            loop=plugin.loop,
            logger=decky.logger,
            min_required_version=Plugin._MIN_REQUIRED_VERSION,
            forget_device=MagicMock(),
            clear_playtime_scope_notice=MagicMock(),
        ),
    )


class TestTranslateHttpError:
    """Tests for _translate_http_error method."""

    def test_401_becomes_auth_error(self, plugin):
        exc = urllib.error.HTTPError("http://romm.local/api/test", 401, "Unauthorized", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/test", "GET")
        assert isinstance(result, RommAuthError)
        assert result.status_code == 401
        assert result.url == "http://romm.local/api/test"
        assert result.method == "GET"
        assert "401" in str(result)

    def test_403_becomes_forbidden_error(self, plugin):
        exc = urllib.error.HTTPError("url", 403, "Forbidden", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x", "POST")
        assert isinstance(result, RommForbiddenError)
        assert result.status_code == 403

    def test_404_becomes_not_found_error(self, plugin):
        # RomM's own entity answer — the only 404 shape that is entity authority
        # (TestNotFoundDiscrimination pins every other shape).
        result = plugin._http_adapter.translate_http_error(_entity_404(), "http://romm.local/api/x")
        assert isinstance(result, RommNotFoundError)
        assert result.status_code == 404

    def test_409_becomes_conflict_error(self, plugin):
        exc = urllib.error.HTTPError("url", 409, "Conflict", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x", "PUT")
        assert isinstance(result, RommConflictError)
        assert result.status_code == 409

    def test_422_becomes_unprocessable_with_parsed_detail(self, plugin):
        detail = [{"loc": ["body", "sessions", 2], "msg": "end_time must be after start_time"}]
        body = json.dumps({"detail": detail}).encode()
        exc = urllib.error.HTTPError("url", 422, "Unprocessable Entity", http.client.HTTPMessage(), io.BytesIO(body))
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/play-sessions", "POST")
        assert isinstance(result, RommUnprocessableEntityError)
        assert result.status_code == 422
        assert result.detail == detail
        assert result.method == "POST"

    def test_422_with_unreadable_body_degrades_to_none_detail(self, plugin):
        # fp=None → exc.read() fails → detail degrades to None (whole-request fallback).
        exc = urllib.error.HTTPError("url", 422, "Unprocessable Entity", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/play-sessions", "POST")
        assert isinstance(result, RommUnprocessableEntityError)
        assert result.detail is None

    def test_422_with_non_json_body_degrades_to_none_detail(self, plugin):
        exc = urllib.error.HTTPError(
            "url", 422, "Unprocessable Entity", http.client.HTTPMessage(), io.BytesIO(b"<html>nope</html>")
        )
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/play-sessions", "POST")
        assert isinstance(result, RommUnprocessableEntityError)
        assert result.detail is None

    def test_500_becomes_server_error(self, plugin):
        exc = urllib.error.HTTPError("url", 500, "Internal Server Error", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommServerError)
        assert result.status_code == 500

    def test_502_becomes_server_error(self, plugin):
        exc = urllib.error.HTTPError("url", 502, "Bad Gateway", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommServerError)
        assert result.status_code == 502

    def test_429_becomes_server_error(self, plugin):
        exc = urllib.error.HTTPError("url", 429, "Too Many Requests", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommServerError)
        assert result.status_code == 429
        assert "Rate limited" in str(result)

    def test_other_4xx_becomes_generic_api_error(self, plugin):
        exc = urllib.error.HTTPError("url", 418, "I'm a Teapot", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommApiError)
        assert not isinstance(result, RommServerError)
        assert "418" in str(result)

    def test_url_error_plain_becomes_connection_error(self, plugin):
        exc = urllib.error.URLError("Connection refused")
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommConnectionError)

    def test_url_error_wrapping_ssl_becomes_ssl_error(self, plugin):
        ssl_exc = ssl.SSLError("certificate verify failed")
        exc = urllib.error.URLError(ssl_exc)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommSSLError)

    def test_url_error_wrapping_timeout_becomes_timeout_error(self, plugin):
        timeout_exc = TimeoutError("timed out")
        exc = urllib.error.URLError(timeout_exc)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommTimeoutError)

    def test_url_error_wrapping_timeout_error_becomes_timeout_error(self, plugin):
        timeout_exc = TimeoutError("timed out")
        exc = urllib.error.URLError(timeout_exc)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommTimeoutError)

    def test_direct_ssl_error(self, plugin):
        exc = ssl.SSLError("bad cert")
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommSSLError)

    def test_direct_socket_timeout(self, plugin):
        exc = TimeoutError("timed out")
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommTimeoutError)

    def test_direct_timeout_error(self, plugin):
        exc = TimeoutError("timed out")
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommTimeoutError)

    def test_connection_error(self, plugin):
        exc = ConnectionRefusedError("refused")
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommConnectionError)

    def test_os_error(self, plugin):
        exc = OSError("network unreachable")
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommConnectionError)

    def test_unknown_exception_wrapped_in_romm_api_error(self, plugin):
        exc = ValueError("bad value")
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommApiError)
        assert "Unexpected error: bad value" in str(result)


class TestNotFoundDiscrimination:
    """Only RomM's entity layer may raise ``RommNotFoundError`` (#1570).

    ``RommNotFoundError`` is deletion authority downstream, so a 404 that a
    reverse proxy or FastAPI's route table produced must degrade to the
    transport class instead — every caller then fails OPEN.
    """

    _URL = "http://romm.local/api/roms/4375"

    def _translate(self, plugin, exc, **kwargs):
        return plugin._http_adapter.translate_http_error(exc, self._URL, "GET", **kwargs)

    def test_entity_answer_is_not_found(self, plugin):
        result = self._translate(plugin, _entity_404())
        assert isinstance(result, RommNotFoundError)
        assert classify_error(result)[0] == ErrorCode.NOT_FOUND.value

    def test_content_type_parameters_are_tolerated(self, plugin):
        exc = _http_error(
            404,
            "Not Found",
            content_type="application/json; charset=utf-8",
            body=json.dumps({"detail": "Save with id '9' not found"}).encode(),
        )
        assert isinstance(self._translate(plugin, exc), RommNotFoundError)

    def test_any_non_generic_detail_counts_as_an_entity_answer(self, plugin):
        # The detail wording is RomM-version-dependent and is never parsed for
        # the requested id — only the generic default is blocklisted.
        exc = _entity_404("Firmware file for platform 3 is gone")
        assert isinstance(self._translate(plugin, exc), RommNotFoundError)

    @pytest.mark.parametrize(
        ("case", "exc_kwargs"),
        [
            # FastAPI's route table answering — a misconfigured path prefix.
            ("generic route 404", {"content_type": "application/json", "body": b'{"detail":"Not Found"}'}),
            # A reverse proxy (Cloudflare / Traefik) answering instead of RomM.
            ("html body", {"content_type": "text/html; charset=utf-8", "body": b"<html>404 not found</html>"}),
            ("empty body", {"content_type": "application/json", "body": b""}),
            ("malformed json", {"content_type": "application/json", "body": b'{"detail": '}),
            ("no content type", {"body": b'{"detail":"Rom with id \'1\' not found"}'}),
            ("unreadable body", {"content_type": "application/json"}),
            ("detail is not a string", {"content_type": "application/json", "body": b'{"detail":["a","b"]}'}),
            ("detail is absent", {"content_type": "application/json", "body": b'{"error":"nope"}'}),
            ("detail is blank", {"content_type": "application/json", "body": b'{"detail":"   "}'}),
            # A rephrased default is still the default: a real entity answer
            # NAMES the entity, so it is never the bare phrase in any casing.
            ("generic detail, lowercase", {"content_type": "application/json", "body": b'{"detail":"not found"}'}),
            (
                "generic detail, upper + padded",
                {"content_type": "application/json", "body": b'{"detail":" NOT FOUND "}'},
            ),
            ("body is not an object", {"content_type": "application/json", "body": b'["Not Found"]'}),
        ],
    )
    def test_infrastructure_404_is_not_entity_authority(self, plugin, case, exc_kwargs):
        result = self._translate(plugin, _http_error(404, "Not Found", **exc_kwargs))
        assert not isinstance(result, RommNotFoundError), case
        assert isinstance(result, RommApiError), case
        # Fails open: the catch-all sites read this as an unreachable server,
        # never as "RomM confirmed the entity is gone".
        assert classify_error(result)[0] == ErrorCode.SERVER_UNREACHABLE.value, case

    def test_degraded_404_keeps_the_status_line_and_request_context(self, plugin):
        result = self._translate(plugin, _http_error(404, "Not Found", content_type="text/html", body=b"<html>"))
        assert "HTTP 404" in str(result)
        assert result.url == self._URL
        assert result.method == "GET"

    def test_asset_route_keeps_the_plain_status_mapping(self, plugin):
        # Cover assets come off a static mount whose miss looks exactly like a
        # generic route-404; the #1450 fallback must still see RommNotFoundError.
        exc = _http_error(404, "Not Found", content_type="application/json", body=b'{"detail":"Not Found"}')
        assert isinstance(self._translate(plugin, exc, asset_route=True), RommNotFoundError)


# ============================================================================
# HTTP methods raise structured errors
# ============================================================================


class TestRommRequestErrors:
    """_romm_request translates HTTP errors into structured exceptions."""

    def test_401_raises_auth_error(self, plugin):
        _setup_plugin(plugin)
        exc = urllib.error.HTTPError("http://romm.local/api/test", 401, "Unauthorized", http.client.HTTPMessage(), None)
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommAuthError) as exc_info:
            plugin._http_adapter.request("/api/test")
        assert exc_info.value.status_code == 401

    def test_connection_refused_raises_connection_error(self, plugin):
        _setup_plugin(plugin)
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")),
            pytest.raises(RommConnectionError),
        ):
            plugin._http_adapter.request("/api/test")

    def test_timeout_raises_timeout_error(self, plugin):
        _setup_plugin(plugin)
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")), pytest.raises(RommTimeoutError):
            plugin._http_adapter.request("/api/test")

    def test_500_raises_server_error(self, plugin):
        _setup_plugin(plugin)
        exc = urllib.error.HTTPError(
            "http://romm.local/api/test", 500, "Internal Server Error", http.client.HTTPMessage(), None
        )
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommServerError) as exc_info:
            plugin._http_adapter.request("/api/test")
        assert exc_info.value.status_code == 500

    def test_preserves_cause_chain(self, plugin):
        _setup_plugin(plugin)
        original = ConnectionRefusedError("refused")
        with patch("urllib.request.urlopen", side_effect=original), pytest.raises(RommConnectionError) as exc_info:
            plugin._http_adapter.request("/api/test")
        assert exc_info.value.__cause__ is original

    def test_already_translated_error_not_rewrapped(self, plugin):
        """If a nested call already raised RommApiError, don't re-translate."""
        _setup_plugin(plugin)
        original_err = RommAuthError("already translated")
        with patch("urllib.request.urlopen", side_effect=original_err), pytest.raises(RommAuthError) as exc_info:
            plugin._http_adapter.request("/api/test")
        assert str(exc_info.value) == "already translated"


class TestRommJsonRequestErrors:
    """_romm_json_request translates errors too."""

    def test_404_raises_not_found(self, plugin):
        _setup_plugin(plugin)
        exc = _entity_404("Save with id '7' not found")
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommNotFoundError) as exc_info:
            plugin._http_adapter.post_json("/api/saves", {"data": 1})
        assert exc_info.value.detail == "Save with id '7' not found"

    def test_generic_404_degrades_but_keeps_its_detail(self, plugin):
        # The detail-attaching path discriminates like the plain one, and the
        # body it read still rides along for a caller that wants to log it.
        _setup_plugin(plugin)
        exc = _http_error(404, "Not Found", content_type="application/json", body=b'{"detail":"Not Found"}')
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommApiError) as exc_info:
            plugin._http_adapter.post_json("/api/saves", {"data": 1})
        assert not isinstance(exc_info.value, RommNotFoundError)
        assert exc_info.value.detail == "Not Found"

    def test_timeout_raises_timeout_error(self, plugin):
        _setup_plugin(plugin)
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")), pytest.raises(RommTimeoutError):
            plugin._http_adapter.put_json("/api/saves/1", {"data": 1})


class TestRommDownloadErrors:
    """_romm_download translates errors."""

    def test_403_raises_forbidden(self, plugin, tmp_path):
        _setup_plugin(plugin)
        exc = urllib.error.HTTPError(
            "http://romm.local/assets/rom.zip", 403, "Forbidden", http.client.HTTPMessage(), None
        )
        dest = str(tmp_path / "rom.zip")
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommForbiddenError):
            plugin._http_adapter.download("/assets/rom.zip", dest)

    def test_generic_route_404_still_raises_not_found(self, plugin, tmp_path):
        # A byte fetch answers about a file, not an entity, so its 404 keeps the
        # plain mapping and never has to prove an entity answer. Unifying this
        # with the API routes must fail here rather than pass silently.
        _setup_plugin(plugin)
        exc = _http_error(
            404, "Not Found", content_type="application/json", body=b'{"detail":"Not Found"}', url="http://romm.local/x"
        )
        dest = str(tmp_path / "rom.zip")
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommNotFoundError):
            plugin._http_adapter.download("/assets/rom.zip", dest)


class TestRommUploadMultipartErrors:
    """_romm_upload_multipart translates errors."""

    def test_409_raises_conflict(self, plugin, tmp_path):
        _setup_plugin(plugin)
        save_file = tmp_path / "test.srm"
        save_file.write_bytes(b"data")
        exc = urllib.error.HTTPError("http://romm.local/api/saves", 409, "Conflict", http.client.HTTPMessage(), None)
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommConflictError):
            plugin._http_adapter.upload_multipart("/api/saves", str(save_file))


# ============================================================================
# Retry Logic (moved from test_save_sync.py)
# ============================================================================


class TestRetryLogic:
    """Tests for with_retry and is_retryable on RommHttpAdapter."""

    def test_is_retryable_5xx(self, plugin):
        """HTTP 500/502/503 are retryable."""
        for code in (500, 502, 503):
            exc = urllib.error.HTTPError("url", code, "err", http.client.HTTPMessage(), None)
            assert RommHttpAdapter.is_retryable(exc) is True

    def test_is_not_retryable_4xx(self, plugin):
        """HTTP 400/401/404/409 are NOT retryable."""
        for code in (400, 401, 403, 404, 409):
            exc = urllib.error.HTTPError("url", code, "err", http.client.HTTPMessage(), None)
            assert RommHttpAdapter.is_retryable(exc) is False

    def test_is_retryable_connection_errors(self, plugin):
        """ConnectionError, TimeoutError, URLError are retryable."""
        assert RommHttpAdapter.is_retryable(ConnectionError("refused")) is True
        assert RommHttpAdapter.is_retryable(TimeoutError("timed out")) is True
        assert RommHttpAdapter.is_retryable(urllib.error.URLError("unreachable")) is True
        assert RommHttpAdapter.is_retryable(OSError("network down")) is True

    def test_is_not_retryable_other(self, plugin):
        """ValueError, KeyError etc. are NOT retryable."""
        assert RommHttpAdapter.is_retryable(ValueError("bad")) is False
        assert RommHttpAdapter.is_retryable(KeyError("missing")) is False

    def test_is_retryable_romm_server_error(self, plugin):
        """RommServerError is retryable."""
        assert RommHttpAdapter.is_retryable(RommServerError("500")) is True

    def test_is_retryable_romm_connection_error(self, plugin):
        """RommConnectionError is retryable."""
        assert RommHttpAdapter.is_retryable(RommConnectionError("refused")) is True

    def test_is_retryable_romm_timeout_error(self, plugin):
        """RommTimeoutError is retryable."""
        assert RommHttpAdapter.is_retryable(RommTimeoutError("timed out")) is True

    def test_is_not_retryable_romm_auth_error(self, plugin):
        """RommAuthError is NOT retryable."""
        assert RommHttpAdapter.is_retryable(RommAuthError("401")) is False

    def test_is_not_retryable_romm_not_found_error(self, plugin):
        """RommNotFoundError is NOT retryable."""
        assert RommHttpAdapter.is_retryable(RommNotFoundError("404")) is False

    def test_is_not_retryable_romm_conflict_error(self, plugin):
        """RommConflictError is NOT retryable."""
        assert RommHttpAdapter.is_retryable(RommConflictError("409")) is False

    def test_is_not_retryable_romm_ssl_error(self, plugin):
        """RommSSLError is NOT retryable."""
        assert RommHttpAdapter.is_retryable(RommSSLError("cert bad")) is False

    def test_is_not_retryable_romm_forbidden_error(self, plugin):
        """RommForbiddenError is NOT retryable."""
        assert RommHttpAdapter.is_retryable(RommForbiddenError("403")) is False

    def test_retry_succeeds_on_first_try(self, plugin):
        """No retries needed when call succeeds."""
        fn = MagicMock(return_value="ok")
        result = plugin._http_adapter.with_retry(fn, "arg1", key="val")
        assert result == "ok"
        fn.assert_called_once_with("arg1", key="val")

    def test_retry_succeeds_after_transient_failure(self, plugin):
        """Retries on transient error, succeeds on second attempt."""
        fn = MagicMock(side_effect=[ConnectionError("refused"), "ok"])
        with patch("time.sleep"):
            result = plugin._http_adapter.with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert fn.call_count == 2

    def test_retry_exhausted_raises(self, plugin):
        """All attempts fail -> raises last exception."""
        fn = MagicMock(side_effect=ConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(ConnectionError):
            plugin._http_adapter.with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    def test_retry_no_retry_on_4xx(self, plugin):
        """4xx errors raise immediately without retry."""
        err = urllib.error.HTTPError("url", 404, "not found", http.client.HTTPMessage(), None)
        fn = MagicMock(side_effect=err)
        with pytest.raises(urllib.error.HTTPError):
            plugin._http_adapter.with_retry(fn, max_attempts=3, base_delay=0)
        fn.assert_called_once()

    def test_retry_delays_exponential(self, plugin):
        """Delays follow base_delay * 3^attempt pattern."""
        fn = MagicMock(side_effect=[ConnectionError("1"), ConnectionError("2"), "ok"])
        with patch("time.sleep") as mock_sleep:
            plugin._http_adapter.with_retry(fn, max_attempts=3, base_delay=1)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)  # 1 * 3^0
        mock_sleep.assert_any_call(3)  # 1 * 3^1

    def test_retry_no_retry_on_romm_auth_error(self, plugin):
        """RommAuthError raises immediately without retry."""
        fn = MagicMock(side_effect=RommAuthError("401"))
        with pytest.raises(RommAuthError):
            plugin._http_adapter.with_retry(fn, max_attempts=3, base_delay=0)
        fn.assert_called_once()

    def test_retry_retries_romm_server_error(self, plugin):
        """RommServerError is retried."""
        fn = MagicMock(side_effect=[RommServerError("500"), "ok"])
        with patch("time.sleep"):
            result = plugin._http_adapter.with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert fn.call_count == 2

    def test_retry_retries_romm_connection_error(self, plugin):
        """RommConnectionError is retried."""
        fn = MagicMock(side_effect=[RommConnectionError("refused"), "ok"])
        with patch("time.sleep"):
            result = plugin._http_adapter.with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert fn.call_count == 2


# ============================================================================
# test_connection structured errors
# ============================================================================


class TestTestConnectionErrors:
    """test_connection returns a canonical ``reason`` slug in failure responses."""

    @pytest.mark.asyncio
    async def test_config_error_when_url_empty(self, plugin):
        """Returns config_error when no URL is configured."""
        plugin.settings["romm_url"] = ""
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "config_error"
        assert "No server URL" in result["message"]

    @pytest.mark.asyncio
    async def test_auth_error_on_401(self, plugin):
        """Returns auth_error when platforms endpoint returns 401."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        # Heartbeat succeeds, platforms raises auth error
        plugin._romm_api.heartbeat.return_value = {"status": "ok"}
        plugin._romm_api.list_platforms.side_effect = RommAuthError("401")
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "auth_failed"
        assert "Authentication failed" in result["message"]

    @pytest.mark.asyncio
    async def test_connection_error_on_refused(self, plugin):
        """Returns connection_error when server is unreachable."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.side_effect = RommConnectionError("refused")
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert "unreachable" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_ssl_error(self, plugin):
        """Returns ssl_error on SSL certificate failure."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.side_effect = RommSSLError("cert fail")
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert "SSL" in result["message"]

    @pytest.mark.asyncio
    async def test_success_on_happy_path(self, plugin):
        """Returns success when both heartbeat and platforms succeed."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.0"}, "status": "ok"}
        plugin._romm_api.list_platforms.return_value = [{"id": 1, "slug": "n64"}]
        result = await plugin.test_connection()
        assert result["success"] is True
        assert "Connected to RomM 4.9.0" in result["message"]
        assert result["romm_version"] == "4.9.0"
        plugin._romm_api.set_version.assert_called_with("4.9.0")

    @pytest.mark.asyncio
    async def test_server_reachable_but_api_failed(self, plugin):
        """When heartbeat succeeds but platforms fails with non-auth error, message is prefixed."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.0"}}
        plugin._romm_api.list_platforms.side_effect = RommServerError("500", status_code=500)
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert "Server reachable but API request failed" in result["message"]


class TestVersionDetection:
    """test_connection detects and reports RomM server version."""

    @pytest.mark.asyncio
    async def test_version_extracted_from_heartbeat(self, plugin):
        """Extracts version from SYSTEM.VERSION in heartbeat response."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.0"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["romm_version"] == "4.9.0"
        plugin._romm_api.set_version.assert_called_with("4.9.0")

    @pytest.mark.asyncio
    async def test_old_version_rejected(self, plugin):
        """Versions below 4.9.0 are rejected with version_error."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "version_error"
        assert result["romm_version"] == "4.5.0"

    @pytest.mark.asyncio
    async def test_46_version_rejected(self, plugin):
        """RomM 4.6.x is below the 4.9.0 minimum and is rejected."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.6.1"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "version_error"

    @pytest.mark.asyncio
    async def test_47_version_rejected(self, plugin):
        """RomM 4.7.x is below the 4.9.0 minimum and is rejected."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.7.0"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "version_error"

    @pytest.mark.asyncio
    async def test_minimum_version_accepted(self, plugin):
        """RomM 4.9.0 meets the minimum version requirement."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.9.0"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_former_minimum_now_rejected(self, plugin):
        """RomM 4.8.1 (the former minimum) is below 4.9.0 and is now rejected."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "version_error"

    @pytest.mark.asyncio
    async def test_development_version_accepted(self, plugin):
        """Development builds pass through without version check."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "development"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is True
        assert result.get("romm_version") == "development"

    @pytest.mark.asyncio
    async def test_missing_version_in_heartbeat(self, plugin):
        """Handles heartbeat without SYSTEM.VERSION gracefully."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"status": "ok"}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is True
        plugin._romm_api.set_version.assert_called_with(None)

    @pytest.mark.asyncio
    async def test_version_cleared_on_connection_failure(self, plugin):
        """Version is cleared when heartbeat fails."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.get_version.return_value = "4.9.0"  # previously detected
        plugin._romm_api.heartbeat.side_effect = RommConnectionError("refused")
        result = await plugin.test_connection()
        assert result["success"] is False
        plugin._romm_api.set_version.assert_called_with(None)

    @pytest.mark.asyncio
    async def test_timeout_error(self, plugin):
        """Returns timeout_error on request timeout."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.side_effect = RommTimeoutError("timed out")
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"


# ── Tests for uncovered HTTP adapter methods ──────────


class TestTranslateHttpStatus:
    """Tests for _translate_http_status() — covers lines 122-134."""

    def _make_client(self):
        import logging

        return RommHttpAdapter(
            {"romm_url": "http://test", "romm_user": "u", "romm_pass": "p"},
            "/tmp",
            logging.getLogger("test"),
            "decky-romm-sync/9.9.9",
        )

    def test_400_bad_request(self):
        client = self._make_client()
        err = client._translate_http_status(400, "Bad request", "/api/test", "GET")
        assert isinstance(err, RommApiError)
        assert "Bad request" in str(err)

    def test_401_auth_error(self):
        client = self._make_client()
        err = client._translate_http_status(401, "Unauthorized", "/api/test", "GET")
        assert isinstance(err, RommAuthError)

    def test_403_forbidden(self):
        client = self._make_client()
        err = client._translate_http_status(403, "Forbidden", "/api/test", "GET")
        assert isinstance(err, RommForbiddenError)

    def test_404_not_found(self):
        client = self._make_client()
        err = client._translate_http_status(404, "Not Found", "/api/test", "GET")
        assert isinstance(err, RommNotFoundError)

    def test_409_conflict(self):
        client = self._make_client()
        err = client._translate_http_status(409, "Conflict", "/api/test", "POST")
        assert isinstance(err, RommConflictError)

    def test_429_rate_limited(self):
        client = self._make_client()
        err = client._translate_http_status(429, "Too Many", "/api/test", "GET")
        assert isinstance(err, RommServerError)
        assert "Rate limited" in str(err)

    def test_500_server_error(self):
        client = self._make_client()
        err = client._translate_http_status(500, "Internal Server Error", "/api/test", "GET")
        assert isinstance(err, RommServerError)

    def test_502_server_error(self):
        client = self._make_client()
        err = client._translate_http_status(502, "Bad Gateway", "/api/test", "GET")
        assert isinstance(err, RommServerError)

    def test_unknown_4xx(self):
        client = self._make_client()
        err = client._translate_http_status(418, "I'm a teapot", "/api/test", "GET")
        assert isinstance(err, RommApiError)
        assert not isinstance(err, RommServerError)


class TestTranslateUnwrapped:
    """Tests for _translate_unwrapped() — covers lines 137-145."""

    def test_ssl_error(self):
        err = RommHttpAdapter._translate_unwrapped(ssl.SSLError("cert error"), "/api", "GET")
        assert isinstance(err, RommSSLError)

    def test_socket_timeout(self):
        err = RommHttpAdapter._translate_unwrapped(TimeoutError("timed out"), "/api", "GET")
        assert isinstance(err, RommTimeoutError)

    def test_timeout_error(self):
        err = RommHttpAdapter._translate_unwrapped(TimeoutError("timed out"), "/api", "GET")
        assert isinstance(err, RommTimeoutError)

    def test_connection_error(self):
        err = RommHttpAdapter._translate_unwrapped(ConnectionError("refused"), "/api", "GET")
        assert isinstance(err, RommConnectionError)

    def test_os_error(self):
        err = RommHttpAdapter._translate_unwrapped(OSError("disk full"), "/api", "GET")
        assert isinstance(err, RommConnectionError)

    def test_unexpected_error(self):
        err = RommHttpAdapter._translate_unwrapped(ValueError("weird"), "/api", "GET")
        assert isinstance(err, RommApiError)
        assert "Unexpected" in str(err)


class TestStreamToFile:
    """Tests for _stream_to_file() — covers lines 214-229."""

    def test_writes_data_to_file(self, tmp_path):
        from io import BytesIO

        data = b"hello world" * 100
        resp = MagicMock()
        resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        resp.read = stream.read

        dest = tmp_path / "output.bin"
        total, downloaded = RommHttpAdapter._stream_to_file(resp, dest)
        assert total == len(data)
        assert downloaded == len(data)
        assert dest.read_bytes() == data

    def test_no_content_length(self, tmp_path):
        from io import BytesIO

        data = b"some data"
        resp = MagicMock()
        resp.headers = {}
        stream = BytesIO(data)
        resp.read = stream.read

        dest = tmp_path / "output.bin"
        total, downloaded = RommHttpAdapter._stream_to_file(resp, dest)
        assert total == 0
        assert downloaded == len(data)

    def test_progress_callback(self, tmp_path):
        from io import BytesIO

        data = b"x" * 16384  # 2 blocks
        resp = MagicMock()
        resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        resp.read = stream.read

        progress_calls = []
        dest = tmp_path / "output.bin"
        RommHttpAdapter._stream_to_file(resp, dest, progress_callback=lambda d, t: progress_calls.append((d, t)))
        assert len(progress_calls) >= 1
        assert progress_calls[-1][0] == len(data)


class TestValidateDownload:
    """Tests for _validate_download() — covers lines 232-237."""

    def test_valid_download(self):
        RommHttpAdapter._validate_download(1000, 1000)  # should not raise

    def test_incomplete_download(self):
        with pytest.raises(IOError, match="incomplete"):
            RommHttpAdapter._validate_download(1000, 500)

    def test_zero_bytes_no_content_length(self):
        with pytest.raises(IOError, match="0 bytes"):
            RommHttpAdapter._validate_download(0, 0)

    def test_no_content_length_but_data_received(self):
        RommHttpAdapter._validate_download(0, 500)  # should not raise


class TestDownloadTimeout:
    """Tests for progressive read timeout in download() and _stream_to_file()."""

    def _make_adapter(self):
        import logging

        settings = {"romm_url": "http://romm.local", "romm_user": "user", "romm_pass": "pass"}
        return RommHttpAdapter(settings, "/fake/plugin_dir", logging.getLogger("test"), "decky-romm-sync/9.9.9")

    # ------------------------------------------------------------------
    # _stream_to_file direct tests
    # ------------------------------------------------------------------

    def test_stream_to_file_socket_timeout_mid_transfer_raises_timeout_error(self, tmp_path):
        """socket.timeout during resp.read() raises RommTimeoutError with 'stalled' in message."""
        resp = MagicMock()
        resp.headers = {"Content-Length": "65536"}
        # First read returns data, second raises socket.timeout
        resp.read.side_effect = [b"x" * 256, TimeoutError("timed out")]

        dest = tmp_path / "rom.zip"
        with pytest.raises(RommTimeoutError, match="stalled"):
            RommHttpAdapter._stream_to_file(resp, dest)

    def test_stream_to_file_timeout_error_mid_transfer_raises_timeout_error(self, tmp_path):
        """TimeoutError during resp.read() is also caught and re-raised as RommTimeoutError."""
        resp = MagicMock()
        resp.headers = {"Content-Length": "65536"}
        resp.read.side_effect = [b"y" * 512, TimeoutError("read timed out")]

        dest = tmp_path / "rom.zip"
        with pytest.raises(RommTimeoutError, match="stalled"):
            RommHttpAdapter._stream_to_file(resp, dest)

    def test_stream_to_file_uses_larger_block_size(self, tmp_path):
        """resp.read is called with block_size=65536 (the class default)."""
        from io import BytesIO

        data = b"a" * 65536
        resp = MagicMock()
        resp.headers = {"Content-Length": str(len(data))}
        # Use a real BytesIO so read() terminates naturally, but spy via side_effect wrapper
        stream = BytesIO(data)
        calls = []

        def read_spy(n):
            calls.append(n)
            return stream.read(n)

        resp.read = read_spy

        dest = tmp_path / "output.bin"
        RommHttpAdapter._stream_to_file(resp, dest, block_size=65536)
        # Every call to read should have used block_size=65536
        assert all(n == 65536 for n in calls)

    def test_stream_to_file_custom_block_size(self, tmp_path):
        """block_size parameter is forwarded to resp.read correctly."""
        from io import BytesIO

        data = b"b" * 1024
        resp = MagicMock()
        resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        calls = []

        def read_spy(n):
            calls.append(n)
            return stream.read(n)

        resp.read = read_spy

        dest = tmp_path / "output.bin"
        RommHttpAdapter._stream_to_file(resp, dest, block_size=512)
        assert all(n == 512 for n in calls)

    # ------------------------------------------------------------------
    # download() integration tests
    # ------------------------------------------------------------------

    def test_download_stall_raises_timeout_error(self, tmp_path):
        """Mock urlopen returns a response whose read() stalls — RommTimeoutError raised."""
        adapter = self._make_adapter()
        dest = str(tmp_path / "rom.zip")

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": "131072"}
        call_count = 0

        def _read(n):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return b"x" * 65536
            raise TimeoutError("no data")

        mock_resp.read = _read
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=mock_resp),
            pytest.raises(RommTimeoutError, match="stalled") as exc_info,
        ):
            adapter.download("/roms/big.zip", dest)
        assert exc_info.value.url is not None
        assert "romm.local" in exc_info.value.url

    def test_large_download_succeeds_with_slow_chunks(self, tmp_path):
        """download() completes successfully when chunks arrive steadily."""
        from io import BytesIO

        adapter = self._make_adapter()
        dest = str(tmp_path / "rom.zip")
        data = b"chunk" * 13107  # ~64KB

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        mock_resp.read = stream.read
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            adapter.download("/roms/game.zip", dest)

        with open(dest, "rb") as f:
            assert f.read() == data

    def test_download_sends_user_agent(self, tmp_path):
        """download() requests carry the injected ``User-Agent`` (#249)."""
        from io import BytesIO

        adapter = self._make_adapter()
        dest = str(tmp_path / "rom.zip")
        data = b"hello"

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        mock_resp.read = stream.read
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            adapter.download("/roms/game.zip", dest)

        req = mock_open.call_args[0][0]
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_download_omits_authorization_when_no_token(self, tmp_path):
        """A token-less download omits the Authorization header (no empty Bearer)."""
        from io import BytesIO

        adapter = self._make_adapter()
        adapter._settings.pop("romm_api_token", None)
        dest = str(tmp_path / "rom.zip")
        data = b"hello"

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        mock_resp.read = stream.read
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            adapter.download("/roms/game.zip", dest)

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") is None
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_connection_timeout_still_works(self, tmp_path):
        """socket.timeout raised by urlopen (connection phase) -> RommTimeoutError."""
        adapter = self._make_adapter()
        dest = str(tmp_path / "rom.zip")

        with (
            patch("urllib.request.urlopen", side_effect=TimeoutError("connection timed out")),
            pytest.raises(RommTimeoutError),
        ):
            adapter.download("/roms/game.zip", dest)

    def test_connection_timeout_via_urlerror(self, tmp_path):
        """URLError-wrapped socket.timeout (real urllib path) -> RommTimeoutError."""
        adapter = self._make_adapter()
        dest = str(tmp_path / "rom.zip")
        with (
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError("connection timed out"))),
            pytest.raises(RommTimeoutError),
        ):
            adapter.download("/roms/game.zip", dest)

    def test_download_sets_read_timeout_on_socket(self, tmp_path):
        """After urlopen succeeds, settimeout(_READ_TIMEOUT) is called on the raw socket."""
        from io import BytesIO

        adapter = self._make_adapter()
        dest = str(tmp_path / "rom.zip")
        data = b"hello"

        mock_sock = MagicMock()
        mock_raw = MagicMock()
        mock_raw._sock = mock_sock
        mock_fp = MagicMock()
        mock_fp.raw = mock_raw

        mock_resp = MagicMock()
        mock_resp.fp = mock_fp
        mock_resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        mock_resp.read = stream.read
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            adapter.download("/roms/game.zip", dest)

        mock_sock.settimeout.assert_called_once_with(RommHttpAdapter._READ_TIMEOUT)

    def test_download_no_socket_attribute_does_not_crash(self, tmp_path):
        """When fp/raw/_sock chain is absent, download proceeds without crashing."""
        from io import BytesIO

        adapter = self._make_adapter()
        dest = str(tmp_path / "rom.zip")
        data = b"hello"

        mock_resp = MagicMock()
        mock_resp.fp = None  # breaks the getattr chain: getattr(None, 'raw', None) -> None
        mock_resp.headers = {"Content-Length": str(len(data))}
        stream = BytesIO(data)
        mock_resp.read = stream.read
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            adapter.download("/roms/game.zip", dest)  # should not raise

        with open(dest, "rb") as f:
            assert f.read() == data


def _make_resp(status, headers, body):
    """Build a context-manager urlopen-style response mock.

    ``status`` drives the resume branch; ``headers`` is a plain dict (so
    ``.get`` works for Content-Range / Accept-Ranges / cf-ray); ``body`` is the
    bytes the stream yields.
    """
    from io import BytesIO

    resp = MagicMock()
    resp.status = status
    resp.headers = headers
    resp.fp = None  # short-circuit the raw-socket settimeout chain
    stream = BytesIO(body)
    resp.read = stream.read
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _resume_adapter():
    import logging

    settings = {"romm_url": "http://romm.local", "romm_user": "u", "romm_pass": "p"}
    return RommHttpAdapter(settings, "/fake/plugin_dir", logging.getLogger("test"), "decky-romm-sync/9.9.9")


class TestIsCloudflare:
    """``_is_cloudflare`` — Cloudflare edge detection from response headers."""

    def test_cf_ray_present_is_cloudflare(self):
        assert RommHttpAdapter._is_cloudflare({"cf-ray": "abc123-FRA"}) is True

    def test_server_cloudflare_is_cloudflare(self):
        assert RommHttpAdapter._is_cloudflare({"server": "cloudflare"}) is True

    def test_server_cloudflare_mixed_case(self):
        assert RommHttpAdapter._is_cloudflare({"server": "Cloudflare"}) is True

    def test_no_cloudflare_markers(self):
        assert RommHttpAdapter._is_cloudflare({"server": "nginx"}) is False

    def test_empty_headers(self):
        assert RommHttpAdapter._is_cloudflare({}) is False


class TestRangeSupported:
    """``_range_supported`` — resumability verdict from status + headers."""

    def test_206_proves_range_even_without_accept_ranges(self):
        # RomM's single-file 206 may omit Accept-Ranges; the 206 itself proves it.
        assert RommHttpAdapter._range_supported(206, {}) is True

    def test_200_with_accept_ranges_bytes(self):
        assert RommHttpAdapter._range_supported(200, {"Accept-Ranges": "bytes"}) is True

    def test_200_mod_zip_no_accept_ranges(self):
        # Multi-file ROM mod_zip: 200, no Accept-Ranges → not resumable.
        assert RommHttpAdapter._range_supported(200, {}) is False

    def test_206_through_cloudflare_is_not_resumable(self):
        # The edge strips Range, so even a 206 can't be trusted for resume.
        assert RommHttpAdapter._range_supported(206, {"cf-ray": "x"}) is False

    def test_200_accept_ranges_through_cloudflare_is_not_resumable(self):
        assert RommHttpAdapter._range_supported(200, {"Accept-Ranges": "bytes", "server": "cloudflare"}) is False


class TestParseContentRange:
    """``_parse_content_range`` — extract (start, total) from Content-Range."""

    def test_parses_well_formed_range(self):
        assert RommHttpAdapter._parse_content_range("bytes 100-199/500") == (100, 500)

    def test_none_for_missing_header(self):
        assert RommHttpAdapter._parse_content_range(None) is None

    def test_none_for_unknown_total(self):
        assert RommHttpAdapter._parse_content_range("bytes 100-199/*") is None

    def test_none_for_wrong_unit(self):
        assert RommHttpAdapter._parse_content_range("items 0-9/100") is None

    def test_none_for_malformed(self):
        assert RommHttpAdapter._parse_content_range("garbage") is None


class TestDownloadResume:
    """``download(resume=True)`` — 206 appends, 200 truncates + restarts."""

    def test_206_appends_onto_existing_tmp(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "rom.tmp")
        with open(dest, "wb") as f:
            f.write(b"AAAA")  # 4 bytes already on disk

        # 206: remaining 6 bytes, Content-Range start matches the 4 on disk.
        resp = _make_resp(
            206,
            {"Content-Length": "6", "Content-Range": "bytes 4-9/10"},
            b"BBBBBB",
        )

        with patch("urllib.request.urlopen", return_value=resp):
            adapter.download("/api/roms/1/content/rom", dest, resume=True)

        with open(dest, "rb") as f:
            assert f.read() == b"AAAABBBBBB"  # appended, full 10 bytes

    def test_200_fallback_truncates_and_restarts(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "rom.tmp")
        with open(dest, "wb") as f:
            f.write(b"STALE")  # stale partial that must be discarded

        # Server ignored Range (Cloudflare/compression): plain 200, full body.
        resp = _make_resp(200, {"Content-Length": "5"}, b"FRESH")

        with patch("urllib.request.urlopen", return_value=resp):
            adapter.download("/api/roms/1/content/rom", dest, resume=True)

        with open(dest, "rb") as f:
            assert f.read() == b"FRESH"  # truncated + rewritten, no STALE prefix

    def test_206_start_mismatch_restarts_from_zero(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "rom.tmp")
        with open(dest, "wb") as f:
            f.write(b"AAAA")  # 4 bytes locally

        # 206 whose start (2) does NOT match the 4 local bytes → treat as restart.
        resp = _make_resp(
            206,
            {"Content-Length": "8", "Content-Range": "bytes 2-9/8"},
            b"WHOLEDAT",
        )

        with patch("urllib.request.urlopen", return_value=resp):
            adapter.download("/api/roms/1/content/rom", dest, resume=True)

        with open(dest, "rb") as f:
            assert f.read() == b"WHOLEDAT"  # truncated, not appended

    def test_206_mismatch_validates_against_full_total(self):
        """L2 fail-safe: a 206 whose start we did NOT ask for restarts (wb, 0) but
        keeps the FULL Content-Range total, not the partial-remainder
        Content-Length — so a non-compliant server's short body fails the
        completeness check instead of silently passing as a truncated file."""
        adapter = _resume_adapter()
        # start=2 (≠ the 4 local bytes), full total=10, but only 6 remainder bytes.
        resp = _make_resp(206, {"Content-Length": "6", "Content-Range": "bytes 2-9/10"}, b"PARTAL")
        mode, seed, total = adapter._resume_branch(resp, 206, existing_size=4)
        assert (mode, seed) == ("wb", 0)
        assert total == 10  # full file size, NOT the 6-byte remainder

    def test_on_meta_fires_once_with_range_supported(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "rom.tmp")

        resp = _make_resp(206, {"Content-Length": "5", "Content-Range": "bytes 0-4/5"}, b"hello")
        meta_calls: list[bool] = []

        with patch("urllib.request.urlopen", return_value=resp):
            adapter.download("/api/roms/1/content/rom", dest, on_meta=meta_calls.append)

        assert meta_calls == [True]  # 206 → range_supported True, exactly once

    def test_on_meta_false_for_mod_zip_200(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "rom.tmp")

        resp = _make_resp(200, {"Content-Length": "5"}, b"hello")
        meta_calls: list[bool] = []

        with patch("urllib.request.urlopen", return_value=resp):
            adapter.download("/api/roms/1/content/rom", dest, on_meta=meta_calls.append)

        assert meta_calls == [False]  # 200, no Accept-Ranges → not resumable

    def test_resume_sends_range_header_when_tmp_exists(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "rom.tmp")
        with open(dest, "wb") as f:
            f.write(b"AAAA")

        resp = _make_resp(206, {"Content-Length": "6", "Content-Range": "bytes 4-9/10"}, b"BBBBBB")

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download("/api/roms/1/content/rom", dest, resume=True)

        req = mock_open.call_args[0][0]
        assert req.get_header("Range") == "bytes=4-"

    def test_no_range_header_without_resume(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "rom.tmp")
        with open(dest, "wb") as f:
            f.write(b"AAAA")  # exists, but resume not requested

        resp = _make_resp(200, {"Content-Length": "5"}, b"FRESH")

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download("/api/roms/1/content/rom", dest, resume=False)

        req = mock_open.call_args[0][0]
        assert req.get_header("Range") is None


class TestDownloadExternal:
    """``download_external`` — the bearer-free fetch for the url_cover fallback (#1450)."""

    def _adapter_with_token(self):
        import logging

        settings = {
            "romm_url": "http://romm.local",
            "romm_api_token": "rmm_secret",
            "romm_api_token_origin": "http://romm.local",
        }
        return RommHttpAdapter(settings, "/fake/plugin_dir", logging.getLogger("test"), "decky-romm-sync/9.9.9")

    def test_omits_authorization_even_with_stored_token(self, tmp_path):
        """The host-bound RomM bearer must NEVER reach the external url_cover host."""
        adapter = self._adapter_with_token()
        dest = str(tmp_path / "cover.png")
        data = b"cdn art"
        resp = _make_resp(200, {"Content-Length": str(len(data))}, data)

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download_external("https://cdn.example.com/x.png", dest)

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") is None
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"
        with open(dest, "rb") as f:
            assert f.read() == data

    def test_uses_absolute_url_verbatim_not_romm_prefixed(self, tmp_path):
        """The url is absolute — it must NOT be prefixed with romm_url."""
        adapter = self._adapter_with_token()
        dest = str(tmp_path / "cover.png")
        resp = _make_resp(200, {"Content-Length": "1"}, b"x")

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download_external("https://cdn.example.com/x.png", dest)

        req = mock_open.call_args[0][0]
        assert req.full_url == "https://cdn.example.com/x.png"

    def test_encodes_spaces_in_url(self, tmp_path):
        """RomM cover URLs carry raw spaces — encoded before the request."""
        adapter = self._adapter_with_token()
        dest = str(tmp_path / "cover.png")
        resp = _make_resp(200, {"Content-Length": "1"}, b"x")

        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download_external("https://cdn.example.com/a b.png", dest)

        req = mock_open.call_args[0][0]
        assert req.full_url == "https://cdn.example.com/a%20b.png"

    def test_404_raises_not_found_without_retry(self, tmp_path):
        adapter = self._adapter_with_token()
        dest = str(tmp_path / "cover.png")
        exc = urllib.error.HTTPError("https://cdn.example.com/x.png", 404, "Not Found", http.client.HTTPMessage(), None)

        with (
            patch("urllib.request.urlopen", side_effect=exc) as mock_open,
            pytest.raises(RommNotFoundError),
        ):
            adapter.download_external("https://cdn.example.com/x.png", dest)

        # A definitive 404 is not retryable — a single attempt.
        assert mock_open.call_count == 1

    def test_generic_route_404_still_raises_not_found(self, tmp_path):
        # The CDN has no entity layer at all, so this byte fetch's 404 keeps the
        # plain mapping instead of proving an entity answer it could never give.
        adapter = self._adapter_with_token()
        dest = str(tmp_path / "cover.png")
        exc = _http_error(
            404,
            "Not Found",
            content_type="application/json",
            body=b'{"detail":"Not Found"}',
            url="https://cdn.example.com/x.png",
        )
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommNotFoundError):
            adapter.download_external("https://cdn.example.com/x.png", dest)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "FILE:///etc/passwd",
            "ftp://internal.host/secret",
            "data:text/html,<script>x</script>",
            "gopher://127.0.0.1:70/",
            "//cdn.example.com/x.png",  # scheme-relative — no scheme
            "/relative/path.png",  # not absolute
        ],
    )
    def test_rejects_non_http_scheme_without_requesting(self, tmp_path, url):
        """A server-supplied non-http(s) url_cover must never reach urlopen (#1450).

        The RomM ``url_cover`` is untrusted input; a ``file:///etc/passwd`` (or
        any other scheme) is refused with the adapter's normal error type before
        any request, and nothing is written to disk.
        """
        adapter = self._adapter_with_token()
        dest = tmp_path / "cover.png"

        with (
            patch("urllib.request.urlopen") as mock_open,
            pytest.raises(RommApiError),
        ):
            adapter.download_external(url, str(dest))

        mock_open.assert_not_called()
        assert not dest.exists()


class TestDownloadConditional:
    """download_conditional — the #1454 revalidation GET (304 vs 200 + validators)."""

    def test_plain_get_returns_validators_and_streams(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "c.png")
        resp = _make_resp(
            200,
            {"Content-Length": "5", "ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
            b"BYTES",
        )
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = adapter.download_conditional("/cover/big.png?ts=1", dest)
        # No validator supplied → no conditional header sent.
        req = mock_open.call_args[0][0]
        assert req.get_header("If-none-match") is None
        assert req.get_header("If-modified-since") is None
        assert result.not_modified is False
        assert result.etag == '"v1"'
        assert result.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"
        with open(dest, "rb") as f:
            assert f.read() == b"BYTES"

    def test_if_none_match_304_keeps_dest(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "c.png")
        with open(dest, "wb") as f:
            f.write(b"CACHED")
        hdrs = http.client.HTTPMessage()
        hdrs["ETag"] = '"v1"'
        exc = urllib.error.HTTPError("http://romm.local/cover/big.png?ts=2", 304, "Not Modified", hdrs, None)
        with patch("urllib.request.urlopen", side_effect=exc) as mock_open:
            result = adapter.download_conditional("/cover/big.png?ts=2", dest, etag='"v1"')
        req = mock_open.call_args[0][0]
        assert req.get_header("If-none-match") == '"v1"'
        assert result.not_modified is True
        assert result.etag == '"v1"'
        # The cached bytes survive — the 304 never touched dest.
        with open(dest, "rb") as f:
            assert f.read() == b"CACHED"

    def test_if_none_match_200_replaces_dest(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "c.png")
        with open(dest, "wb") as f:
            f.write(b"OLD")
        resp = _make_resp(200, {"Content-Length": "3", "ETag": '"v2"'}, b"NEW")
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = adapter.download_conditional("/cover/big.png?ts=2", dest, etag='"v1"')
        req = mock_open.call_args[0][0]
        assert req.get_header("If-none-match") == '"v1"'
        assert result.not_modified is False
        assert result.etag == '"v2"'
        with open(dest, "rb") as f:
            assert f.read() == b"NEW"

    def test_if_modified_since_fallback_when_only_last_modified(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "c.png")
        resp = _make_resp(200, {"Content-Length": "1"}, b"x")
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download_conditional("/c.png?ts=2", dest, last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
        req = mock_open.call_args[0][0]
        assert req.get_header("If-modified-since") == "Wed, 01 Jan 2025 00:00:00 GMT"
        assert req.get_header("If-none-match") is None

    def test_etag_preferred_over_last_modified(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "c.png")
        resp = _make_resp(200, {"Content-Length": "1"}, b"x")
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download_conditional(
                "/c.png?ts=2", dest, etag='"v1"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT"
            )
        req = mock_open.call_args[0][0]
        assert req.get_header("If-none-match") == '"v1"'
        assert req.get_header("If-modified-since") is None

    def test_authenticated_get_carries_bearer(self, tmp_path):
        """A RomM-origin cover GET keeps the bearer (unlike the external url_cover fetch)."""
        import logging

        settings = {
            "romm_url": "http://romm.local",
            "romm_api_token": "rmm_secret",
            "romm_api_token_origin": "http://romm.local",
        }
        adapter = RommHttpAdapter(settings, "/fake/plugin_dir", logging.getLogger("test"), "decky-romm-sync/9.9.9")
        dest = str(tmp_path / "c.png")
        resp = _make_resp(200, {"Content-Length": "1"}, b"x")
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            adapter.download_conditional("/c.png?ts=1", dest, etag='"v1"')
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer rmm_secret"
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_404_raises_not_found(self, tmp_path):
        adapter = _resume_adapter()
        dest = str(tmp_path / "c.png")
        exc = urllib.error.HTTPError("http://romm.local/c.png", 404, "Not Found", http.client.HTTPMessage(), None)
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommNotFoundError):
            adapter.download_conditional("/c.png?ts=1", dest, etag='"v1"')

    def test_generic_route_404_still_raises_not_found(self, tmp_path):
        # RomM's cover resources come off a static mount, so a missing cover
        # answers with FastAPI's generic route-404 body. The asset routes keep
        # the plain mapping precisely so the #1450 url_cover fallback still fires
        # on it — the entity-answer proof applies to the API routes only.
        adapter = _resume_adapter()
        dest = str(tmp_path / "c.png")
        exc = _http_error(
            404,
            "Not Found",
            content_type="application/json",
            body=b'{"detail":"Not Found"}',
            url="http://romm.local/c.png",
        )
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommNotFoundError):
            adapter.download_conditional("/c.png?ts=1", dest, etag='"v1"')
