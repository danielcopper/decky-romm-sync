"""RomM API error types for structured error handling."""


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


def classify_error(exc):
    """Return (error_code, user_friendly_message) for an exception."""
    if isinstance(exc, RommAuthError):
        return "auth_error", "Authentication failed \u2014 check your username and password"
    if isinstance(exc, RommForbiddenError):
        return "forbidden_error", "Access denied \u2014 your account lacks permissions for this action"
    if isinstance(exc, RommSSLError):
        return "ssl_error", "SSL certificate error \u2014 enable 'Allow Insecure SSL' in settings for self-signed certs"
    if isinstance(exc, RommTimeoutError):
        return "timeout_error", "Request timed out \u2014 server may be overloaded or network is slow"
    if isinstance(exc, RommConnectionError):
        return "connection_error", "Server unreachable \u2014 check your URL and ensure RomM is running"
    if isinstance(exc, RommServerError):
        code = exc.status_code or 500
        return "server_error", f"Server error ({code}) \u2014 check your RomM server logs"
    if isinstance(exc, RommNotFoundError):
        return "not_found_error", "Resource not found on server"
    if isinstance(exc, RommUnsupportedError):
        return "unsupported_error", f"This feature requires RomM {exc.min_version} or newer"
    if isinstance(exc, RommApiError):
        return "api_error", str(exc)
    return "unknown_error", str(exc)


def error_response(exc, fallback_message=None):
    """Build a standard {success, message, error_code} dict from an exception."""
    code, msg = classify_error(exc)
    return {"success": False, "message": fallback_message or msg, "error_code": code}
