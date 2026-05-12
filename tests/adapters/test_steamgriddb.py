"""Tests for adapters.steamgriddb — SteamGridDB HTTP adapter."""

import http.client
import json
import logging
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from adapters.steamgriddb import SteamGridDbAdapter
from lib.errors import SgdbApiError


@pytest.fixture
def adapter():
    settings = {"steamgriddb_api_key": "test-key-123"}
    return SteamGridDbAdapter(settings=settings, logger=logging.getLogger("test"))


class TestRequest:
    def test_returns_none_when_no_api_key(self):
        adapter = SteamGridDbAdapter(settings={}, logger=logging.getLogger("test"))
        assert adapter.request("/games/igdb/123") is None

    def test_sends_auth_header(self, adapter):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            adapter.request("/games/igdb/123")
            req = mock_open.call_args[0][0]
            assert req.get_header("Authorization") == "Bearer test-key-123"

    def test_sends_user_agent(self, adapter):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            adapter.request("/test")
            req = mock_open.call_args[0][0]
            assert "decky-romm-sync" in req.get_header("User-agent")

    def test_uses_ssl_context(self, adapter):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            adapter.request("/test")
            # Context should be passed as a kwarg
            assert mock_open.call_args[1].get("context") is not None

    def test_returns_parsed_json(self, adapter):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"success": True, "data": {"id": 42}}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = adapter.request("/games/igdb/123")
            assert result == {"success": True, "data": {"id": 42}}

    def test_constructs_correct_url(self, adapter):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            adapter.request("/heroes/game/123")
            req = mock_open.call_args[0][0]
            assert req.full_url == "https://www.steamgriddb.com/api/v2/heroes/game/123"


class TestDownloadImage:
    def test_downloads_to_dest_path(self, adapter, tmp_path):
        dest = str(tmp_path / "test.png")
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [b"PNG_DATA", b""]
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = adapter.download_image("https://example.com/img.png", dest)
            assert result is True
            with open(dest, "rb") as fh:
                assert fh.read() == b"PNG_DATA"

    def test_atomic_write_cleans_tmp_on_failure(self, adapter, tmp_path):
        dest = str(tmp_path / "test.png")
        with patch("urllib.request.urlopen", side_effect=Exception("network")):
            result = adapter.download_image("https://example.com/img.png", dest)
            assert result is False
            assert not (tmp_path / "test.png").exists()
            assert not (tmp_path / "test.png.tmp").exists()


class TestVerifyApiKey:
    def test_returns_parsed_response(self, adapter):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"success": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = adapter.verify_api_key("my-key")
            assert result == {"success": True}

    def test_sends_provided_key_not_settings_key(self, adapter):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            adapter.verify_api_key("different-key")
            req = mock_open.call_args[0][0]
            assert req.get_header("Authorization") == "Bearer different-key"

    def test_http_error_raises_sgdb_api_error(self, adapter):
        http_error = urllib.error.HTTPError(
            "https://steamgriddb.com", 401, "Unauthorized", http.client.HTTPMessage(), None
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with pytest.raises(SgdbApiError) as exc_info:
                adapter.verify_api_key("bad-key")
            assert exc_info.value.status_code == 401

    def test_http_500_raises_sgdb_api_error(self, adapter):
        http_error = urllib.error.HTTPError(
            "https://steamgriddb.com", 500, "Server Error", http.client.HTTPMessage(), None
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with pytest.raises(SgdbApiError) as exc_info:
                adapter.verify_api_key("any-key")
            assert exc_info.value.status_code == 500


class TestRequestHttpErrorWrapping:
    def test_request_wraps_http_error(self, adapter):
        http_error = urllib.error.HTTPError(
            "https://steamgriddb.com", 403, "Forbidden", http.client.HTTPMessage(), None
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with pytest.raises(SgdbApiError) as exc_info:
                adapter.request("/games/igdb/123")
            assert exc_info.value.status_code == 403
