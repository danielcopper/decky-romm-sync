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
from collections.abc import Callable
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
    RommUnprocessableEntityError,
    TokenHostMismatchError,
)
from lib.url_host import same_origin

_CONTENT_TYPE_JSON = "application/json"

# Optional per-retry listener: ``(attempt, max_attempts, delay_s) -> None`` where
# *attempt* is the 1-based OVERALL attempt ordinal about to run (the initial try
# is 1, so the first retry is 2, the second 3 — it reads as "attempt N/M"). Fired
# from :meth:`RommHttpAdapter.with_retry` just before it sleeps ahead of a retry.
# Injected at the composition root (``bootstrap``) so the adapter stays
# service-free: it surfaces retry progress without importing ``decky`` or any
# service (the composition root marshals the call onto the loop and emits the
# ``server_retry_progress`` event).
RetryListener = Callable[[int, int, float], None]


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
    on_retry:
        Optional :data:`RetryListener` invoked once per retry (before the
        backoff sleep) so the UI can surface "connecting… (attempt N/M)". A
        settable attribute, not a hard dependency: ``bootstrap`` wires the
        loop-threadsafe emit after construction (the loop/emit only exist at
        service-wiring time), and tests can pass a spy at construction.
    """

    _CONNECT_TIMEOUT = 30
    _READ_TIMEOUT = 60
    _DOWNLOAD_BLOCK_SIZE = 65536

    def __init__(
        self,
        settings: dict[str, Any],
        plugin_dir: str,
        logger: logging.Logger,
        user_agent: str,
        on_retry: RetryListener | None = None,
    ) -> None:
        self._settings = settings
        self._plugin_dir = plugin_dir
        self._logger = logger
        self._user_agent = user_agent
        self.on_retry = on_retry

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

    def auth_header(self) -> str | None:
        """Bearer auth header value, or ``None`` when no Client API Token is stored.

        The token is host-bound: it is sent only to the server it was minted
        against. When a token is stored together with its minting origin
        (``romm_api_token_origin``) and that origin no longer matches the
        current ``romm_url`` origin, a :class:`TokenHostMismatchError` is raised
        instead of leaking the bearer to a wrong/hostile host (#1039). A legacy
        token minted before origin stamping (origin ``None``) is still attached,
        so existing installs keep working until their next sign-in stamps it.

        Returning ``None`` lets callers omit the ``Authorization`` header on
        unauthenticated probes (fresh setup, before a token is minted). An empty
        ``Bearer `` value is malformed and some RomM versions 500 on it, which
        would deadlock first-time connection setup.
        """
        token = self._settings.get("romm_api_token") or ""
        if not token:
            return None
        origin = self._settings.get("romm_api_token_origin")
        if origin is not None and not same_origin(origin, self._settings.get("romm_url")):
            raise TokenHostMismatchError("Stored token was minted for a different server than the configured URL")
        return f"Bearer {token}"

    def _apply_default_headers(self, req: urllib.request.Request) -> None:
        """Attach the standard outgoing headers: ``User-Agent`` always, and
        ``Authorization`` only when a Client API Token is stored."""
        header = self.auth_header()
        if header is not None:
            req.add_header("Authorization", header)
        req.add_header("User-Agent", self._user_agent)

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
    def _read_error_detail(exc: urllib.error.HTTPError) -> Any:
        """Read the FastAPI ``detail`` field from an error response body, or ``None``.

        Reads the ``HTTPError`` body once (it is otherwise discarded) and returns
        its ``detail`` value, so a caller can distinguish two responses that share
        a status code but carry different ``detail`` text. Any unreadable /
        non-JSON body degrades to ``None``.
        """
        try:
            raw = exc.read().decode()
            body = json.loads(raw) if raw else None
        except Exception:  # unreadable / non-JSON body — degrade to no detail
            return None
        return body.get("detail") if isinstance(body, dict) else None

    def _translate_unprocessable(
        self, exc: urllib.error.HTTPError, url: str, method: str
    ) -> RommUnprocessableEntityError:
        """Build a :class:`RommUnprocessableEntityError` carrying the parsed 422 ``detail``.

        Reads the ``HTTPError`` response body once and extracts its ``detail``
        list (RomM/FastAPI's per-field validation errors). A missing/unreadable
        body degrades to ``detail=None`` — the caller then falls back to a
        whole-request strategy rather than a per-entry one.
        """
        detail = self._read_error_detail(exc)
        msg = f"HTTP 422: {exc.reason} ({method} {url})"
        return RommUnprocessableEntityError(msg, detail=detail, url=url, method=method)

    def _translate_with_detail(self, exc: urllib.error.HTTPError, url: str, method: str) -> RommApiError:
        """Translate an ``HTTPError`` like :meth:`translate_http_error`, with ``detail`` attached.

        Same status → type mapping as :meth:`translate_http_error` (422 keeps its
        own structured path), but the FastAPI ``detail`` string is read once and
        attached to the typed error, so a caller can branch on reasons that share
        a status code (two 404s that differ only by ``detail``).
        """
        if exc.code == 422:
            return self._translate_unprocessable(exc, url, method)
        detail = self._read_error_detail(exc)
        msg = f"HTTP {exc.code}: {exc.reason} ({method} {url})"
        error = self._translate_http_status(exc.code, msg, url, method)
        error.detail = detail
        return error

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
            if exc.code == 422:
                # A 422 carries a structured validation body (FastAPI's
                # ``{"detail": [...]}``). Reading it lets the caller act on WHICH
                # entries a batch endpoint rejected (#1312) instead of collapsing
                # the whole request to a generic error.
                return self._translate_unprocessable(exc, url, method)
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
        # TokenHostMismatchError is intentionally absent: a wrong-origin token
        # can never become right by retrying, so with_retry re-raises it at once.
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
                    self._notify_retry(attempt + 2, max_attempts, float(delay))
                    time.sleep(delay)
                else:
                    raise
        raise last_exc  # type: ignore[misc]  # pragma: no cover

    def _notify_retry(self, attempt: int, max_attempts: int, delay_s: float) -> None:
        """Fire the optional retry listener, swallowing any listener failure.

        The listener surfaces best-effort UI progress; a raise from it (e.g. a
        closed loop during plugin unload) must never abort the real HTTP retry
        underway, so it is caught and logged rather than propagated.
        """
        if self.on_retry is None:
            return
        try:
            self.on_retry(attempt, max_attempts, delay_s)
        except Exception:  # progress emit is best-effort — never break a real retry
            self._logger.debug("on_retry listener raised; ignoring", exc_info=True)

    # ------------------------------------------------------------------
    # HTTP request methods
    # ------------------------------------------------------------------

    def request(self, path: str):
        """GET a JSON resource from the RomM API (with retry)."""
        return self.with_retry(self._build_get(path))

    def request_once(self, path: str, *, timeout: int):
        """GET a JSON resource in a SINGLE attempt with a SHORT *timeout*.

        Deliberately bypasses :meth:`with_retry` and the 30s urlopen timeout used
        by :meth:`request`. The launch-gate reachability probe needs a fast
        offline verdict (~3s, one shot) instead of waiting through 3 retry
        attempts and up to ~90s of accumulated remote timeouts. The real sync
        paths keep the retrying :meth:`request`.
        """
        return self._build_get(path, timeout=timeout)()

    def _build_get(self, path: str, *, timeout: int = 30):
        """Build the GET worker closure shared by :meth:`request` / :meth:`request_once`."""
        url = self._settings["romm_url"].rstrip("/") + path

        def _do_request():
            req = urllib.request.Request(url, method="GET")
            self._apply_default_headers(req)
            try:
                with urllib.request.urlopen(req, context=self.ssl_context(), timeout=timeout) as resp:
                    return json.loads(resp.read().decode())
            except RommApiError:
                raise
            except Exception as exc:
                raise self.translate_http_error(exc, url, "GET") from exc

        return _do_request

    @staticmethod
    def _is_cloudflare(headers) -> bool:
        """True when the response was served through a Cloudflare edge.

        Cloudflare Tunnel strips ``Range`` from the request (the origin then
        returns a plain 200) and stamps ``cf-ray`` / ``server: cloudflare`` on
        the response, so a download routed through it can NEVER be resumed even
        if the origin itself supports ranges. Detecting the edge lets the
        service surface "not resumable" honestly instead of attempting a resume
        that silently restarts from byte 0.
        """
        if headers.get("cf-ray"):
            return True
        return "cloudflare" in (headers.get("server") or "").lower()

    @classmethod
    def _range_supported(cls, status: int, headers) -> bool:
        """Whether the live response proves byte-range resumption is available.

        A ``206 Partial Content`` proves range support even without an
        ``Accept-Ranges`` header (RomM's single-file 206 may omit it); a plain
        ``200`` carrying ``Accept-Ranges: bytes`` advertises it. Either way a
        Cloudflare edge vetoes resumability (it discards ``Range``), so the
        edge check overrides both.
        """
        ranged = status == 206 or (headers.get("Accept-Ranges", "").lower() == "bytes")
        return ranged and not cls._is_cloudflare(headers)

    @staticmethod
    def _stream_to_file(
        resp,
        dest_path: Path,
        progress_callback=None,
        block_size: int = 65536,
        url: str = "",
        *,
        mode: str = "wb",
        downloaded: int = 0,
        total: int | None = None,
    ) -> tuple[int, int]:
        """Read *resp* into *dest_path* and return ``(total, downloaded)``.

        ``mode`` is ``"ab"`` for a resumed transfer (append onto the existing
        ``.tmp``) or ``"wb"`` for a fresh one. ``downloaded`` seeds the running
        byte count with the bytes already on disk so progress + validation count
        against the FULL file. ``total`` overrides the ``Content-Length``-derived
        total — required on a 206, whose ``Content-Length`` is only the REMAINING
        byte count, not the whole file (the full size comes from ``Content-Range``).
        """
        if total is None:
            raw_total = resp.headers.get("Content-Length")
            total = int(raw_total) if raw_total else 0
        with open(dest_path, mode) as f:
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

    @staticmethod
    def _parse_content_range(header: str | None) -> tuple[int, int] | None:
        """Parse ``Content-Range: bytes start-end/total`` → ``(start, total)``.

        Returns ``None`` for a missing/malformed header or an unknown total
        (``*``), so the caller falls back to a fresh transfer rather than
        trusting a bad range.
        """
        if not header:
            return None
        try:
            unit, _, spec = header.strip().partition(" ")
            if unit.lower() != "bytes":
                return None
            range_part, _, total_part = spec.partition("/")
            start_str, _, _end = range_part.partition("-")
            if total_part == "*":
                return None
            return int(start_str), int(total_part)
        except (ValueError, AttributeError):
            return None

    def download(self, path: str, dest: str, progress_callback=None, *, resume=False, on_meta=None):
        """Download a file from the RomM API to a local path.

        When ``resume`` is set and ``dest`` already holds a partial transfer, the
        request carries a ``Range`` header and the server's actual response status
        decides the branch: a ``206 Partial Content`` appends onto the existing
        bytes; a plain ``200`` (Range ignored — Cloudflare, compression, or a
        non-range server) restarts from scratch. ``on_meta``, when given, is
        invoked exactly once with ``range_supported: bool`` the moment the
        response headers arrive (before the body streams), so the caller can
        surface resumability live during the download.
        """
        encoded_path = urllib.parse.quote(path, safe="/:?=&@")
        url = self._settings["romm_url"].rstrip("/") + encoded_path
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        # One-shot guard: ``on_meta`` fires once even across a ``with_retry`` retry.
        meta_sent = [False]

        def _do_download():
            req = urllib.request.Request(url, method="GET")
            self._apply_default_headers(req)
            # Re-evaluated on every retry attempt from the CURRENT .tmp size, so a
            # retried resume picks up wherever the previous attempt left the file.
            existing_size = dest_path.stat().st_size if (resume and dest_path.exists()) else 0
            if existing_size > 0:
                req.add_header("Range", f"bytes={existing_size}-")
            ctx = self.ssl_context()
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=self._CONNECT_TIMEOUT) as resp:
                    raw_sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                    if raw_sock is not None:
                        raw_sock.settimeout(self._READ_TIMEOUT)
                    status = getattr(resp, "status", None) or resp.getcode()
                    if on_meta is not None and not meta_sent[0]:
                        meta_sent[0] = True
                        on_meta(self._range_supported(status, resp.headers))
                    mode, seed, total = self._resume_branch(resp, status, existing_size)
                    total, downloaded = self._stream_to_file(
                        resp,
                        dest_path,
                        progress_callback,
                        block_size=self._DOWNLOAD_BLOCK_SIZE,
                        url=url,
                        mode=mode,
                        downloaded=seed,
                        total=total,
                    )
                self._validate_download(total, downloaded)
            except RommApiError:
                raise
            except Exception as exc:
                raise self.translate_http_error(exc, url, "GET") from exc

        return self.with_retry(_do_download)

    # Schemes an external ``url_cover`` fetch may use. A server-supplied
    # ``url_cover`` is untrusted input, so anything else (``file:``, ``ftp:``,
    # ``data:``, scheme-relative, …) is refused before any request — a
    # ``file:///etc/passwd`` must never reach ``urlopen`` (#1450). Post-DNS
    # private/link-local IP blocking and per-redirect-hop revalidation are the
    # broader bounded-SSRF hardening tracked in #1182 (they also cover the
    # pre-existing RomM download paths); stdlib's ``HTTPRedirectHandler`` already
    # refuses redirects to non-http(s) schemes, so this allowlist closes the
    # ``file:`` primitive on its own.
    _EXTERNAL_URL_SCHEMES = ("http", "https")

    def download_external(self, url: str, dest: str) -> None:
        """Download from an ARBITRARY absolute *url* — ``User-Agent`` only, never the bearer.

        For a ROM's ``url_cover`` (an external metadata-provider CDN such as
        SteamGridDB / IGDB) used as the fallback when the RomM-local cover asset
        404s (#1450): the host-bound RomM bearer must NEVER reach a third-party
        origin, so only the plugin ``User-Agent`` is attached — no
        ``Authorization`` header is ever built here (the CDN behind Cloudflare
        Bot Fight Mode also 403s the default ``Python-urllib`` UA). The *url*
        scheme is validated against :attr:`_EXTERNAL_URL_SCHEMES` first and a
        non-http(s) scheme is rejected with a :class:`RommApiError` before any
        request. Spaces in *url* are URL-encoded (RomM cover URLs carry them
        raw). Single-shot streaming — no ``Range``/resume, covers are small;
        transient errors retry through :meth:`with_retry` like every other
        download, a 404 raises ``RommNotFoundError`` without retry.
        """
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in self._EXTERNAL_URL_SCHEMES:
            raise RommApiError(f"Refusing url_cover with non-http(s) scheme {scheme!r}", url=url, method="GET")
        encoded_url = urllib.parse.quote(url, safe="/:?=&@")
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        def _do_download():
            req = urllib.request.Request(encoded_url, method="GET")
            req.add_header("User-Agent", self._user_agent)
            try:
                with urllib.request.urlopen(req, context=self.ssl_context(), timeout=self._CONNECT_TIMEOUT) as resp:
                    raw_sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                    if raw_sock is not None:
                        raw_sock.settimeout(self._READ_TIMEOUT)
                    total, downloaded = self._stream_to_file(
                        resp,
                        dest_path,
                        block_size=self._DOWNLOAD_BLOCK_SIZE,
                        url=encoded_url,
                    )
                self._validate_download(total, downloaded)
            except RommApiError:
                raise
            except Exception as exc:
                raise self.translate_http_error(exc, encoded_url, "GET") from exc

        return self.with_retry(_do_download)

    def _resume_branch(self, resp, status: int, existing_size: int) -> tuple[str, int, int]:
        """Decide ``(open_mode, seed_bytes, total)`` from the live response.

        On a ``206`` whose ``Content-Range`` start matches the bytes already on
        disk: append (``"ab"``), seed the running count with those bytes, and use
        the Content-Range total (the 206's ``Content-Length`` is only the
        remainder). A 206 whose start does NOT match the local size is treated as
        a fresh transfer (truncate + restart) — a stale ``.tmp`` must never be
        appended onto a mismatched offset. Any other status (a plain ``200`` — the
        server ignored ``Range``, or a non-resume download) truncates and restarts.
        """
        if status == 206:
            parsed = self._parse_content_range(resp.headers.get("Content-Range"))
            if parsed is not None and parsed[0] == existing_size and existing_size > 0:
                return ("ab", existing_size, parsed[1])
            if parsed is not None:
                # A 206 whose range start we did NOT ask for (only a non-compliant
                # server does this — a compliant one honours the Range or returns
                # 200/416). Restart from byte 0, but validate against the FULL
                # Content-Range total so the server's partial body fails the
                # completeness check loudly instead of silently passing as a
                # short/corrupt file.
                return ("wb", 0, parsed[1])
        # 200 (server ignored Range — safe full re-download) → restart from byte 0.
        raw_total = resp.headers.get("Content-Length")
        return ("wb", 0, int(raw_total) if raw_total else 0)

    def json_request(self, path: str, data, method: str = "POST"):
        """Send a JSON request (POST/PUT) to RomM API, return parsed response."""
        url = self._settings["romm_url"].rstrip("/") + path

        def _do_json_request():
            body = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Content-Type", _CONTENT_TYPE_JSON)
            self._apply_default_headers(req)
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
        self._apply_default_headers(req)
        try:
            with urllib.request.urlopen(req, context=self.ssl_context(), timeout=30) as resp:
                return json.loads(resp.read().decode())
        except RommApiError:
            raise
        except Exception as exc:
            raise self.translate_http_error(exc, url, method) from exc

    # Intentionally skips with_retry: a public single-use credential (the pairing
    # code) must not be replayed — a retry burns the one-time code and the
    # server-side rate limit.
    def unauthenticated_post_json(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to a PUBLIC RomM endpoint — no ``Authorization`` header, no retry.

        For endpoints whose credential is the request body itself (the
        pairing-code exchange, whose one-time code IS the credential): a stored
        bearer must never be attached (only the ``User-Agent`` goes out). On an
        HTTP error the response ``detail`` is attached to the raised typed error
        so the caller can branch on the server's specific reason. Returns ``{}``
        on a 204 No Content, parsed JSON otherwise.
        """
        url = self._settings["romm_url"].rstrip("/") + path
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", _CONTENT_TYPE_JSON)
        req.add_header("User-Agent", self._user_agent)
        try:
            with urllib.request.urlopen(req, context=self.ssl_context(), timeout=30) as resp:
                if resp.status == 204:
                    return {}
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise self._translate_with_detail(exc, url, "POST") from exc
        except Exception as exc:
            raise self.translate_http_error(exc, url, "POST") from exc

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
            req.add_header("Content-Type", _CONTENT_TYPE_JSON)
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
