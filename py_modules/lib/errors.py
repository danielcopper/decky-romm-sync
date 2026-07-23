"""RomM API error types for structured error handling."""

import socket
from typing import Any

from lib.list_result import ErrorCode


class SgdbApiError(Exception):
    """Raised by SteamGridDb adapter for non-2xx HTTP responses.

    Wraps urllib.error.HTTPError so callers never import urllib.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class SteamGridDirMissingError(Exception):
    """Raised when the Steam grid directory cannot be located.

    Distinguishes the "expected, user-recoverable" missing-grid-dir
    condition from generic write failures so callers can route the
    two cases through different log levels.
    """


class RommApiError(Exception):
    """Base exception for all RomM HTTP API errors."""

    status_code = None
    # Parsed FastAPI ``detail`` body, populated only by the request paths that
    # read it (422 validation, the pairing-code exchange). ``None`` otherwise, so
    # a caller can branch on two responses that share a status code but differ in
    # ``detail`` without every error path paying to read the body.
    detail: Any = None

    def __init__(self, message, url=None, method=None):
        self.url = url
        self.method = method
        super().__init__(message)


class RommAuthError(RommApiError):
    """401 Unauthorized — bad credentials."""

    status_code = 401


class RommForbiddenError(RommApiError):
    """403 Forbidden — valid credentials but insufficient permissions."""

    status_code = 403


class RommNotFoundError(RommApiError):
    """404 Not Found — resource does not exist."""

    status_code = 404


class RommConflictError(RommApiError):
    """409 Conflict."""

    status_code = 409


class RommUnprocessableEntityError(RommApiError):
    """422 Unprocessable Entity — the request body failed server-side validation.

    Carries the parsed validation ``detail`` body (RomM/FastAPI's
    ``{"detail": [{"loc": [...], "msg": ...}, ...]}``) so a caller can identify
    which entries of a batch endpoint the server rejected. Non-retryable: the
    same body draws the same validation verdict, so replaying it cannot succeed.
    """

    status_code = 422

    def __init__(self, message, *, detail=None, url=None, method=None):
        self.detail = detail
        super().__init__(message, url=url, method=method)


class RommServerError(RommApiError):
    """5xx server errors (500, 502, 503, etc.)."""

    def __init__(self, message, status_code=500, url=None, method=None):
        self.status_code = status_code
        super().__init__(message, url=url, method=method)


class RommConnectionError(RommApiError):
    """Network-level failures: connection refused, DNS failure, reset, etc."""


class RommTimeoutError(RommApiError):
    """Request timed out."""


class RommSSLError(RommApiError):
    """SSL certificate verification failure."""


class RommUnsupportedError(RommApiError):
    """Feature not available in the connected RomM server version."""

    def __init__(self, feature, min_version, url=None, method=None):
        self.feature = feature
        self.min_version = min_version
        super().__init__(
            f"{feature} requires RomM {min_version} or newer",
            url=url,
            method=method,
        )


class TokenHostMismatchError(RommApiError):
    """Stored token's minting origin does not match the current ``romm_url``.

    Raised before any request carries the bearer token to a host the token was
    not minted for, so the credential never leaks to a wrong/hostile server.
    Non-retryable: replaying it cannot succeed, only re-signing-in can.
    """


class RommSyncDisabledError(RommApiError):
    """RomM has save sync disabled for this device (the negotiate 400 policy signal).

    RomM's per-device ``sync_enabled`` switch is enforced only at the
    ``negotiate`` endpoint, which 400s with detail "Sync is disabled for this
    device" when the user turned sync off for this device server-side. Distinct
    from a transport failure so the engine can stop the run with a visible
    policy reason instead of degrading to a sessionless sync. Non-retryable:
    replaying it cannot succeed, only re-enabling sync in RomM can.
    """


class PairingCodeInvalidError(RommApiError):
    """Pairing-code exchange rejected: the code is invalid, expired, or already used (404)."""


class PairingCodeTokenGoneError(RommApiError):
    """Pairing-code exchange rejected: the token the code was minted for no longer exists (404)."""


class PairingCodeOwnerDisabledError(RommApiError):
    """Pairing-code exchange rejected: the token owner account is disabled (403)."""


class PairingCodeRateLimitedError(RommApiError):
    """Pairing-code exchange rejected: too many exchange attempts from this client (429)."""


class DeviceNotRegisteredError(Exception):
    """A save upload was attempted with no registered device id.

    A client-side precondition failure, not a RomM HTTP error (hence a plain
    ``Exception``, outside the :class:`RommApiError` hierarchy). The RomM >= 4.9
    floor makes device registration the norm, so a missing device id must refuse
    the upload rather than fall back to a slot-less (legacy) POST that would
    misfile a named-slot save into the ``slot:null`` bucket (#1478). Save-sync
    funnels catch it and surface the ``device_not_registered`` reason slug —
    they own the slug/message (a save-sync concern) since ``lib`` cannot import
    the service-layer message constants.
    """


def classify_error(exc):
    """Return ``(reason, user_friendly_message)`` for an exception.

    ``reason`` is a canonical :class:`lib.list_result.ErrorCode` slug
    (returned as its string value). Several exception types fold onto one
    slug \u2014 connection/timeout/SSL/5xx/generic-API plus raw socket errors
    all map to ``server_unreachable``; 401/403 map to ``auth_failed`` \u2014 but
    each branch keeps a distinct human ``message``. In particular the 403
    branch stays distinguishable from the 401 branch: a Cloudflare
    bot-fight 403 at the tunnel edge is not a wrong-credentials failure, so
    the two share the ``auth_failed`` slug but explain different remedies.
    """
    if isinstance(exc, RommAuthError):
        return ErrorCode.AUTH_FAILED.value, "Authentication failed \u2014 check your username and password"
    if isinstance(exc, RommForbiddenError):
        return ErrorCode.AUTH_FAILED.value, "Access denied \u2014 your account lacks permissions for this action"
    if isinstance(exc, RommSSLError):
        return (
            ErrorCode.SERVER_UNREACHABLE.value,
            "SSL certificate error \u2014 enable 'Allow Insecure SSL' in settings for self-signed certs",
        )
    if isinstance(exc, RommTimeoutError):
        return (
            ErrorCode.SERVER_UNREACHABLE.value,
            "Request timed out \u2014 server may be overloaded or network is slow",
        )
    if isinstance(exc, RommConnectionError):
        return (
            ErrorCode.SERVER_UNREACHABLE.value,
            "Server unreachable \u2014 check your URL and ensure RomM is running",
        )
    if isinstance(exc, RommServerError):
        code = exc.status_code or 500
        return ErrorCode.SERVER_UNREACHABLE.value, f"Server error ({code}) \u2014 check your RomM server logs"
    if isinstance(exc, RommNotFoundError):
        return ErrorCode.NOT_FOUND.value, "Resource not found on server"
    if isinstance(exc, RommUnsupportedError):
        return ErrorCode.UNSUPPORTED.value, f"This feature requires RomM {exc.min_version} or newer"
    if isinstance(exc, TokenHostMismatchError):
        return "config_error", "Your saved RomM login is for a different server. Sign in again to continue."
    if isinstance(exc, RommSyncDisabledError):
        return (
            "device_sync_disabled",
            "Save sync is disabled for this device on the RomM server — enable it in RomM's device settings",
        )
    if isinstance(exc, RommApiError):
        return ErrorCode.SERVER_UNREACHABLE.value, str(exc)
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror)):
        # A socket-level failure that never passed through the adapter's
        # ``translate_http_error`` (a caller reaching the network outside the
        # RomM client). Same reachability verdict as ``RommConnectionError``.
        # Deliberately NOT bare ``OSError``: that also covers local disk I/O,
        # so a failed settings write would report the server as unreachable —
        # the same class of lie as reporting a 404 that way.
        return ErrorCode.SERVER_UNREACHABLE.value, f"Server unreachable — {exc}"
    return ErrorCode.UNKNOWN.value, str(exc)


def error_response(exc, fallback_message=None):
    """Build a canonical ``{success, reason, message}`` dict from an exception.

    ``reason`` is the :func:`classify_error` slug; ``message`` is the
    human-readable detail (overridable via *fallback_message*). The legacy
    ``error_code`` key is gone \u2014 this is the single failure shape the
    frontend reads (``scripts/check_failure_shape.py`` enforces it).
    """
    reason, msg = classify_error(exc)
    return {"success": False, "reason": reason, "message": fallback_message or msg}
