"""RomM API error types for structured error handling."""

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


def classify_error(exc):
    """Return ``(reason, user_friendly_message)`` for an exception.

    ``reason`` is a canonical :class:`lib.list_result.ErrorCode` slug
    (returned as its string value). Several exception types fold onto one
    slug \u2014 connection/timeout/SSL/5xx/generic-API all map to
    ``server_unreachable``; 401 and 403 both map to ``auth_failed`` \u2014 but
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
    if isinstance(exc, RommApiError):
        return ErrorCode.SERVER_UNREACHABLE.value, str(exc)
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
