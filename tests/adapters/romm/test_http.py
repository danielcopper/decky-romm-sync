import asyncio
import http.client
import ssl
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from fakes.library_peers import FakeArtworkManager, FakeMetadataExtractor
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen
from models.state import make_default_plugin_state

from adapters.registry_store import RegistryStoreAdapter
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
)

# conftest.py patches decky before this import
from main import Plugin
from services.connection import ConnectionService, ConnectionServiceConfig
from services.library import LibraryService, LibraryServiceConfig


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
    p._state = make_default_plugin_state()
    p._metadata_cache = {}

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
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
            state_persister=MagicMock(),
            settings_persister=MagicMock(),
            registry_store=RegistryStoreAdapter(state=p._state, logger=decky.logger),
            log_debug=p._log_debug,
            metadata_service=FakeMetadataExtractor(),
            artwork=FakeArtworkManager(),
        ),
    )

    p._connection_service = ConnectionService(
        config=ConnectionServiceConfig(
            settings=p.settings,
            romm_api=p._romm_api,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            min_required_version=Plugin._MIN_REQUIRED_VERSION,
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
    def test_basic_auth_format(self, plugin):
        import base64

        plugin.settings["romm_user"] = "admin"
        plugin.settings["romm_pass"] = "secret"
        header = plugin._http_adapter.auth_header()
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        assert decoded == "admin:secret"

    def test_special_characters_in_password(self, plugin):
        import base64

        plugin.settings["romm_user"] = "user"
        plugin.settings["romm_pass"] = "p@ss:w0rd!"
        header = plugin._http_adapter.auth_header()
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        assert decoded == "user:p@ss:w0rd!"


class TestRommRequest:
    def test_uses_auth_header(self, plugin):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_user"] = "user"
        plugin.settings["romm_pass"] = "pass"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"ok": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            result = plugin._http_adapter.request("/api/test")

        assert result == {"ok": True}
        req = mock_open.call_args[0][0]
        assert "Basic " in req.get_header("Authorization")

    def test_sends_user_agent(self, plugin):
        """GET requests carry the injected ``User-Agent`` so Cloudflare Bot
        Fight Mode does not 403 self-hosted RomM behind a tunnel (#249)."""
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_user"] = "user"
        plugin.settings["romm_pass"] = "pass"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"ok": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            plugin._http_adapter.request("/api/test")

        req = mock_open.call_args[0][0]
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"


class TestRommJsonRequest:
    def test_post_json(self, plugin):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_user"] = "user"
        plugin.settings["romm_pass"] = "pass"
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
        assert "Basic " in req.get_header("Authorization")
        assert req.get_header("User-agent") == "decky-romm-sync/9.9.9"

    def test_put_json(self, plugin):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_user"] = "user"
        plugin.settings["romm_pass"] = "pass"
        plugin.settings["romm_allow_insecure_ssl"] = False

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"id": 1}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            plugin._http_adapter.put_json("/api/saves/1", {"filename": "test.srm"})

        req = mock_open.call_args[0][0]
        assert req.get_method() == "PUT"


class TestRommUploadMultipart:
    def test_upload_sends_multipart(self, plugin, tmp_path):
        import json as _json
        from unittest.mock import MagicMock, patch

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["romm_user"] = "user"
        plugin.settings["romm_pass"] = "pass"
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
        assert "Basic " in req.get_header("Authorization")
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
    plugin.settings["romm_allow_insecure_ssl"] = False
    plugin.loop = asyncio.get_event_loop()
    plugin._connection_service = ConnectionService(
        config=ConnectionServiceConfig(
            settings=plugin.settings,
            romm_api=plugin._romm_api,
            loop=plugin.loop,
            logger=decky.logger,
            min_required_version=Plugin._MIN_REQUIRED_VERSION,
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
        exc = urllib.error.HTTPError("url", 404, "Not Found", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x")
        assert isinstance(result, RommNotFoundError)
        assert result.status_code == 404

    def test_409_becomes_conflict_error(self, plugin):
        exc = urllib.error.HTTPError("url", 409, "Conflict", http.client.HTTPMessage(), None)
        result = plugin._http_adapter.translate_http_error(exc, "http://romm.local/api/x", "PUT")
        assert isinstance(result, RommConflictError)
        assert result.status_code == 409

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
        exc = urllib.error.HTTPError("http://romm.local/api/saves", 404, "Not Found", http.client.HTTPMessage(), None)
        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(RommNotFoundError):
            plugin._http_adapter.post_json("/api/saves", {"data": 1})

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
    """test_connection returns structured error_code in responses."""

    @pytest.mark.asyncio
    async def test_config_error_when_url_empty(self, plugin):
        """Returns config_error when no URL is configured."""
        plugin.settings["romm_url"] = ""
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["error_code"] == "config_error"
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
        assert result["error_code"] == "auth_error"
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
        assert result["error_code"] == "connection_error"
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
        assert result["error_code"] == "ssl_error"
        assert "SSL" in result["message"]

    @pytest.mark.asyncio
    async def test_success_on_happy_path(self, plugin):
        """Returns success when both heartbeat and platforms succeed."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}, "status": "ok"}
        plugin._romm_api.list_platforms.return_value = [{"id": 1, "slug": "n64"}]
        result = await plugin.test_connection()
        assert result["success"] is True
        assert "Connected to RomM 4.8.1" in result["message"]
        assert result["romm_version"] == "4.8.1"
        plugin._romm_api.set_version.assert_called_with("4.8.1")

    @pytest.mark.asyncio
    async def test_server_reachable_but_api_failed(self, plugin):
        """When heartbeat succeeds but platforms fails with non-auth error, message is prefixed."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        plugin._romm_api.list_platforms.side_effect = RommServerError("500", status_code=500)
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["error_code"] == "server_error"
        assert "Server reachable but API request failed" in result["message"]


class TestVersionDetection:
    """test_connection detects and reports RomM server version."""

    @pytest.mark.asyncio
    async def test_version_extracted_from_heartbeat(self, plugin):
        """Extracts version from SYSTEM.VERSION in heartbeat response."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["romm_version"] == "4.8.1"
        plugin._romm_api.set_version.assert_called_with("4.8.1")

    @pytest.mark.asyncio
    async def test_old_version_rejected(self, plugin):
        """Versions below 4.8.1 are rejected with version_error."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.5.0"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["error_code"] == "version_error"
        assert result["romm_version"] == "4.5.0"

    @pytest.mark.asyncio
    async def test_46_version_rejected(self, plugin):
        """RomM 4.6.x is below the 4.8.1 minimum and is rejected."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.6.1"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["error_code"] == "version_error"

    @pytest.mark.asyncio
    async def test_47_version_rejected(self, plugin):
        """RomM 4.7.x is below the 4.8.1 minimum and is rejected."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.7.0"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["error_code"] == "version_error"

    @pytest.mark.asyncio
    async def test_48_minimum_version_accepted(self, plugin):
        """RomM 4.8.1 meets the minimum version requirement."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.1"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_480_patch_below_minimum_rejected(self, plugin):
        """RomM 4.8.0 is below the 4.8.1 patch minimum and is rejected."""
        import asyncio

        _setup_plugin(plugin)
        plugin.loop = asyncio.get_event_loop()
        plugin._romm_api.heartbeat.return_value = {"SYSTEM": {"VERSION": "4.8.0"}}
        plugin._romm_api.list_platforms.return_value = []
        result = await plugin.test_connection()
        assert result["success"] is False
        assert result["error_code"] == "version_error"

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
        plugin._romm_api.get_version.return_value = "4.8.1"  # previously detected
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
        assert result["error_code"] == "timeout_error"


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
