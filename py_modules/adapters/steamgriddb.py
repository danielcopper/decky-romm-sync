"""SteamGridDB HTTP adapter — handles all HTTP I/O for the SteamGridDB API."""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from lib.certifi_bundle import ca_bundle as _ca_bundle
from lib.errors import SgdbApiError

if TYPE_CHECKING:
    import logging

_SGDB_BASE_URL = "https://www.steamgriddb.com/api/v2"
_USER_AGENT = "decky-romm-sync/0.1"


class SteamGridDbAdapter:
    """Concrete SteamGridDB HTTP adapter."""

    def __init__(self, *, settings: dict, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger

    def _ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=_ca_bundle())

    def request(self, path: str) -> dict | None:
        """Authenticated GET to SGDB API v2."""
        api_key = self._settings.get("steamgriddb_api_key", "")
        if not api_key:
            return None
        url = _SGDB_BASE_URL + path
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("User-Agent", _USER_AGENT)
        try:
            with urllib.request.urlopen(req, context=self._ssl_context(), timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SgdbApiError(status_code=e.code, message=str(e)) from e

    def download_image(self, url: str, dest_path: str) -> bool:
        """Download image from URL to dest_path with atomic write."""
        tmp_path = dest_path + ".tmp"
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", _USER_AGENT)
            ctx = self._ssl_context()
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp, open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp_path, dest_path)
            return True
        except Exception as e:
            self._logger.warning(f"SGDB image download failed: {e}")
            if os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.remove(tmp_path)
            return False

    def verify_api_key(self, api_key: str) -> dict:
        """Verify an API key against SGDB.

        Raises ``SgdbApiError`` on non-2xx HTTP responses so callers can
        react to auth failures without importing ``urllib``.
        """
        url = f"{_SGDB_BASE_URL}/search/autocomplete/test"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("User-Agent", _USER_AGENT)
        try:
            with urllib.request.urlopen(req, context=self._ssl_context(), timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SgdbApiError(status_code=e.code, message=str(e)) from e
