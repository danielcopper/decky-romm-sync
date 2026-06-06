"""Standalone HTTP client for the RomM API.

No dependency on ``decky`` — all external dependencies (settings, plugin_dir,
logger) are injected via the constructor.
"""

import base64
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, ClassVar

from lib.certifi_bundle import ca_bundle as _ca_bundle
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


class RommHttpAdapter:
    """Low-level HTTP client for RomM API requests.

    Parameters
    ----------
    settings:
        Shared settings dict (held by reference — mutations are visible here).
    plugin_dir:
        Absolute path to the plugin directory (replaces ``decky.DECKY_PLUGIN_DIR``).
    logger:
        Logger instance (replaces ``decky.logger``).
    user_agent:
        Outgoing ``User-Agent`` header value (e.g. ``"decky-romm-sync/0.17.1"``).
        Required because Cloudflare's Bot Fight Mode 403s the default
        ``Python-urllib`` UA before requests reach self-hosted RomM origins.
    """

    _CONNECT_TIMEOUT = 30
    _READ_TIMEOUT = 60
    _DOWNLOAD_BLOCK_SIZE = 65536

    def __init__(self, settings: dict[str, Any], plugin_dir: str, logger: logging.Logger, user_agent: str) -> None:
        self._settings = settings
        self._plugin_dir = plugin_dir
        self._logger = logger
        self._user_agent = user_agent

    # ------------------------------------------------------------------
    # Platform map
    # ------------------------------------------------------------------

    def load_platform_map(self) -> dict[str, str]:
        """Load the platform slug -> RetroDECK system mapping from config.json.

        Degrades to an empty map on a missing or corrupt config.json (matching the
        es_de_config loaders) so ``resolve_system`` falls back to its verbatim
        pass-through (ADR-0010 §5) instead of raising into callers — several of
        which (the synchronous game-detail builder) have no surrounding guard.
        """
        # Check plugin root first (Decky CLI moves defaults/ contents to root),
        # then defaults/ subdirectory (dev deploys via mise run deploy)
        root_path = os.path.join(self._plugin_dir, "config.json")
        dev_path = os.path.join(self._plugin_dir, "defaults", "config.json")
        config_path = root_path if os.path.exists(root_path) else dev_path
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._logger.warning("Failed to load platform_map from config.json: %s", e)
            return {}
        return config.get("platform_map", {})

    def resolve_system(self, platform_slug: str, platform_fs_slug: str | None = None) -> str:
        """Resolve a RomM platform slug to a RetroDECK system name.

        Lazy-loads and caches ``_platform_map`` on first call.
        """
        if not hasattr(self, "_platform_map"):
            self._platform_map = self.load_platform_map()
        platform_map = self._platform_map
        if platform_slug in platform_map:
            return platform_map[platform_slug]
        if platform_fs_slug and platform_fs_slug in platform_map:
            return platform_map[platform_fs_slug]
        return platform_slug

    # ------------------------------------------------------------------
    # SSL / Auth helpers
    # ------------------------------------------------------------------

    def ssl_context(self) -> ssl.SSLContext:
        """SSL context for RomM connections. Respects user insecure toggle."""
        # create_default_context uses secure defaults (TLS 1.2+, cert verification).
        # S4423 is a false positive — Python 3.10+ defaults are safe.
        ctx = ssl.create_default_context(cafile=_ca_bundle())
        if self._settings.get("romm_allow_insecure_ssl", False):
            # Intentionally disabled for self-hosted RomM with self-signed certs.
            # User opts in via settings toggle with UI warning. (S5527, S4830)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def auth_header(self) -> str:
        """Bearer auth header value built from the stored Client API Token."""
        token = self._settings.get("romm_api_token") or ""
        return f"Bearer {token}"

    @staticmethod
    def _basic_auth_header(username: str, password: str) -> str:
        """Base64-encoded Basic Auth header value for *username* / *password*."""
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {credentials}"

    # ------------------------------------------------------------------
    # Error translation & retry logic
    # ------------------------------------------------------------------

    # Maps HTTP status codes to (error_class, custom_message_template_or_None).
    # None means use the default ``msg``.
    _HTTP_STATUS_MAP: ClassVar[dict[int, tuple[type[RommApiError], str | None]]] = {
        400: (RommApiError, "Bad request ({method} {url})"),
        401: (RommAuthError, None),
        403: (RommForbiddenError, None),
        404: (RommNotFoundError, None),
        409: (RommConflictError, None),
        429: (RommServerError, "Rate limited — too many requests ({method} {url})"),
    }

    def _translate_http_status(self, code: int, msg: str, url: str, method: str) -> RommApiError:
        """Map an HTTP status code to a typed error."""
        entry = self._HTTP_STATUS_MAP.get(code)
        if entry:
            cls, tpl = entry
            text = tpl.format(method=method, url=url) if tpl else msg
            kwargs: dict[str, Any] = {"url": url, "method": method}
            if cls is RommServerError:
                kwargs["status_code"] = code
            return cls(text, **kwargs)
        if code >= 500:
            return RommServerError(msg, status_code=code, url=url, method=method)
        return RommApiError(msg, url=url, method=method)

    @staticmethod
    def _translate_unwrapped(exc: Exception, url: str, method: str) -> RommApiError:
        """Translate non-HTTP exceptions (ssl, timeout, connection) to typed errors."""
        if isinstance(exc, ssl.SSLError):
            return RommSSLError(str(exc), url=url, method=method)
        if isinstance(exc, socket.timeout | TimeoutError):
            return RommTimeoutError(str(exc), url=url, method=method)
        if isinstance(exc, ConnectionError | OSError):
            return RommConnectionError(str(exc), url=url, method=method)
        return RommApiError(f"Unexpected error: {exc}", url=url, method=method)

    def translate_http_error(self, exc: Exception, url: str, method: str = "GET") -> RommApiError:
        """Translate urllib/socket exceptions into RommApiError subclasses."""
        if isinstance(exc, urllib.error.HTTPError):
            msg = f"HTTP {exc.code}: {exc.reason} ({method} {url})"
            return self._translate_http_status(exc.code, msg, url, method)
        if isinstance(exc, urllib.error.URLError):
            return (
                self._translate_unwrapped(exc.reason, url, method)
                if isinstance(exc.reason, ssl.SSLError | socket.timeout | TimeoutError)
                else RommConnectionError(str(exc), url=url, method=method)
            )
        return self._translate_unwrapped(exc, url, method)

    @staticmethod
    def is_retryable(exc: Exception) -> bool:
        """Check if an exception is a transient error worth retrying."""
        if isinstance(exc, RommServerError | RommConnectionError | RommTimeoutError):
            return True
        # Backward compat for non-RomM exceptions
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code >= 500
        return isinstance(exc, urllib.error.URLError | ConnectionError | TimeoutError | OSError)

    def with_retry(self, fn, *args, max_attempts: int = 3, base_delay: int = 1, **kwargs):
        """Call fn(*args, **kwargs) with exponential backoff retry.

        Delays: base_delay * 3^attempt (1s, 3s, 9s for defaults).
        Only retries on transient errors (see is_retryable).
        """
        last_exc = None
        for attempt in range(max_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts - 1 and self.is_retryable(exc):
                    delay = base_delay * (3**attempt)
                    self._logger.info(f"Retry {attempt + 1}/{max_attempts} after {delay}s: {exc}")
                    time.sleep(delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]  # pragma: no cover

    # ------------------------------------------------------------------
    # HTTP request methods
    # ------------------------------------------------------------------

    def request(self, path: str):
        """GET a JSON resource from the RomM API."""
        url = self._settings["romm_url"].rstrip("/") + path

        def _do_request():
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", self.auth_header())
            req.add_header("User-Agent", self._user_agent)
            try:
                with urllib.request.urlopen(req, context=self.ssl_context(), timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except RommApiError:
                raise
            except Exception as exc:
                raise self.translate_http_error(exc, url, "GET") from exc

        return self.with_retry(_do_request)

    @staticmethod
    def _stream_to_file(
        resp, dest_path: Path, progress_callback=None, block_size: int = 65536, url: str = ""
    ) -> tuple[int, int]:
        """Read *resp* into *dest_path* and return ``(total, downloaded)``."""
        raw_total = resp.headers.get("Content-Length")
        total = int(raw_total) if raw_total else 0
        downloaded = 0
        with open(dest_path, "wb") as f:
            while True:
                try:
                    chunk = resp.read(block_size)
                except TimeoutError as exc:
                    raise RommTimeoutError(
                        "Download stalled: no data received within read timeout",
                        url=url,
                        method="GET",
                    ) from exc
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total:
                    progress_callback(downloaded, total)
        return total, downloaded

    @staticmethod
    def _validate_download(total: int, downloaded: int) -> None:
        """Raise if the download was incomplete or empty."""
        if total > 0 and downloaded != total:
            raise OSError(f"Download incomplete: got {downloaded} bytes, expected {total}")
        if total == 0 and downloaded == 0:
            raise OSError("Download produced 0 bytes (no Content-Length header and no data received)")

    def download(self, path: str, dest: str, progress_callback=None):
        """Download a file from the RomM API to a local path."""
        encoded_path = urllib.parse.quote(path, safe="/:?=&@")
        url = self._settings["romm_url"].rstrip("/") + encoded_path
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        def _do_download():
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", self.auth_header())
            req.add_header("User-Agent", self._user_agent)
            ctx = self.ssl_context()
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=self._CONNECT_TIMEOUT) as resp:
                    raw_sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                    if raw_sock is not None:
                        raw_sock.settimeout(self._READ_TIMEOUT)
                    total, downloaded = self._stream_to_file(
                        resp, dest_path, progress_callback, block_size=self._DOWNLOAD_BLOCK_SIZE, url=url
                    )
                self._validate_download(total, downloaded)
            except RommApiError:
                raise
            except Exception as exc:
                raise self.translate_http_error(exc, url, "GET") from exc

        return self.with_retry(_do_download)

    def json_request(self, path: str, data, method: str = "POST"):
        """Send a JSON request (POST/PUT) to RomM API, return parsed response."""
        url = self._settings["romm_url"].rstrip("/") + path

        def _do_json_request():
            body = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", self.auth_header())
            req.add_header("User-Agent", self._user_agent)
            try:
                with urllib.request.urlopen(req, context=self.ssl_context(), timeout=30) as resp:
                    return json.loads(resp.read().decode())
            except RommApiError:
                raise
            except Exception as exc:
                raise self.translate_http_error(exc, url, method) from exc

        return self.with_retry(_do_json_request)

    def post_json(self, path: str, data):
        """POST JSON to RomM API, return parsed response."""
        return self.json_request(path, data, method="POST")

    def put_json(self, path: str, data):
        """PUT JSON to RomM API, return parsed response."""
        return self.json_request(path, data, method="PUT")

    # Intentionally skips with_retry: POST uploads may not be idempotent.
    # (RomM saves endpoint upserts by filename, but we err on the side of caution.)
    def upload_multipart(self, path: str, file_path: str, method: str = "POST"):
        """Upload a file via multipart/form-data to RomM API."""
        boundary = uuid.uuid4().hex
        filename = os.path.basename(file_path)
        safe_filename = filename.replace("\r", "").replace("\n", "").replace("\0", "").replace('"', '\\"')

        with open(file_path, "rb") as f:
            file_data = f.read()

        body = b""
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="saveFile"; filename="{safe_filename}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += file_data
        body += f"\r\n--{boundary}--\r\n".encode()

        url = self._settings["romm_url"].rstrip("/") + path
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Authorization", self.auth_header())
        req.add_header("User-Agent", self._user_agent)
        try:
            with urllib.request.urlopen(req, context=self.ssl_context(), timeout=30) as resp:
                return json.loads(resp.read().decode())
        except RommApiError:
            raise
        except Exception as exc:
            raise self.translate_http_error(exc, url, method) from exc

    # Intentionally skips with_retry: token mint/delete are not idempotent
    # and must not be retried (a duplicate mint would orphan a token).
    def basic_auth_request(
        self,
        path: str,
        username: str,
        password: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a one-off Basic-authenticated request from the passed credentials.

        Used for the Client API Token mint/delete flow, where the runtime
        Bearer token deliberately lacks ``me.write`` and so cannot manage
        tokens itself. The ``username`` / ``password`` are taken straight
        from the caller — never from ``self._settings`` — so credentials
        stay transient. Sends a JSON body when ``data`` is given. Returns
        ``{}`` on a 204 No Content, parsed JSON otherwise.
        """
        url = self._settings["romm_url"].rstrip("/") + path
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", self._basic_auth_header(username, password))
        req.add_header("User-Agent", self._user_agent)
        try:
            with urllib.request.urlopen(req, context=self.ssl_context(), timeout=30) as resp:
                if resp.status == 204:
                    return {}
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except RommApiError:
            raise
        except Exception as exc:
            raise self.translate_http_error(exc, url, method) from exc
