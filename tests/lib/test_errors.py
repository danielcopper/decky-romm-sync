import pytest

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
    RommUnsupportedError,
    classify_error,
    error_response,
)


def _make_exc(cls):
    """Create an instance of the given error class with appropriate constructor args."""
    if cls is RommServerError:
        return cls("test", status_code=500)
    if cls is RommUnsupportedError:
        return cls("test feature", "4.7.0")
    return cls("test")


class TestExceptionHierarchy:
    """All custom exceptions inherit from RommApiError and Exception."""

    def test_romm_api_error_is_exception(self):
        assert issubclass(RommApiError, Exception)

    @pytest.mark.parametrize(
        "cls",
        [
            RommAuthError,
            RommForbiddenError,
            RommNotFoundError,
            RommConflictError,
            RommServerError,
            RommConnectionError,
            RommTimeoutError,
            RommSSLError,
            RommUnsupportedError,
        ],
    )
    def test_subclass_is_romm_api_error(self, cls):
        exc = _make_exc(cls)
        assert isinstance(exc, RommApiError)
        assert isinstance(exc, Exception)

    @pytest.mark.parametrize(
        "cls",
        [
            RommAuthError,
            RommForbiddenError,
            RommNotFoundError,
            RommConflictError,
            RommServerError,
            RommConnectionError,
            RommTimeoutError,
            RommSSLError,
            RommUnsupportedError,
        ],
    )
    def test_subclass_caught_by_romm_api_error(self, cls):
        exc = _make_exc(cls)
        with pytest.raises(RommApiError):
            raise exc


class TestExceptionAttributes:
    """Each exception stores message, url, and method."""

    def test_base_error_attributes(self):
        exc = RommApiError("something failed", url="http://romm/api/test", method="GET")
        assert str(exc) == "something failed"
        assert exc.url == "http://romm/api/test"
        assert exc.method == "GET"
        assert exc.status_code is None

    def test_base_error_defaults(self):
        exc = RommApiError("msg")
        assert exc.url is None
        assert exc.method is None

    def test_auth_error_status(self):
        exc = RommAuthError("unauthorized", url="/api/test", method="GET")
        assert exc.status_code == 401
        assert str(exc) == "unauthorized"
        assert exc.url == "/api/test"
        assert exc.method == "GET"

    def test_forbidden_error_status(self):
        exc = RommForbiddenError("forbidden")
        assert exc.status_code == 403

    def test_not_found_error_status(self):
        exc = RommNotFoundError("missing")
        assert exc.status_code == 404

    def test_conflict_error_status(self):
        exc = RommConflictError("conflict")
        assert exc.status_code == 409

    def test_server_error_default_status(self):
        exc = RommServerError("internal error")
        assert exc.status_code == 500

    def test_server_error_custom_status(self):
        exc = RommServerError("bad gateway", status_code=502)
        assert exc.status_code == 502

    def test_server_error_503(self):
        exc = RommServerError("service unavailable", status_code=503, url="/api/x", method="POST")
        assert exc.status_code == 503
        assert exc.url == "/api/x"
        assert exc.method == "POST"

    def test_connection_error_no_status(self):
        exc = RommConnectionError("refused")
        assert exc.status_code is None

    def test_timeout_error_no_status(self):
        exc = RommTimeoutError("timed out")
        assert exc.status_code is None

    def test_ssl_error_no_status(self):
        exc = RommSSLError("cert invalid")
        assert exc.status_code is None

    def test_unsupported_error_attributes(self):
        exc = RommUnsupportedError("save content download", "4.7.0")
        assert exc.feature == "save content download"
        assert exc.min_version == "4.7.0"
        assert exc.status_code is None
        assert "save content download" in str(exc)
        assert "4.7.0" in str(exc)

    def test_unsupported_error_with_url_and_method(self):
        exc = RommUnsupportedError("notes", "4.7.0", url="/api/notes", method="GET")
        assert exc.url == "/api/notes"
        assert exc.method == "GET"
        assert exc.feature == "notes"


class TestExceptionMessage:
    """Exception messages are accessible via str()."""

    def test_message_preserved(self):
        exc = RommAuthError("HTTP 401: Unauthorized (GET /api/test)")
        assert "401" in str(exc)
        assert "Unauthorized" in str(exc)

    def test_server_error_message(self):
        exc = RommServerError("HTTP 502: Bad Gateway (GET /api/heartbeat)", status_code=502)
        assert "502" in str(exc)
        assert "Bad Gateway" in str(exc)


class TestClassifyError:
    """classify_error returns (error_code, user_friendly_message) for each exception type."""

    def test_auth_error(self):
        code, msg = classify_error(RommAuthError("401"))
        assert code == "auth_failed"
        assert "Authentication failed" in msg

    def test_forbidden_error(self):
        code, msg = classify_error(RommForbiddenError("403"))
        assert code == "auth_failed"
        assert "Access denied" in msg

    def test_ssl_error(self):
        code, msg = classify_error(RommSSLError("cert fail"))
        assert code == "server_unreachable"
        assert "SSL certificate error" in msg

    def test_timeout_error(self):
        code, msg = classify_error(RommTimeoutError("timed out"))
        assert code == "server_unreachable"
        assert "timed out" in msg.lower()

    def test_connection_error(self):
        code, msg = classify_error(RommConnectionError("refused"))
        assert code == "server_unreachable"
        assert "unreachable" in msg.lower()

    def test_server_error(self):
        code, msg = classify_error(RommServerError("500", status_code=500))
        assert code == "server_unreachable"
        assert "500" in msg

    def test_server_error_502(self):
        code, msg = classify_error(RommServerError("bad gateway", status_code=502))
        assert code == "server_unreachable"
        assert "502" in msg

    def test_not_found_error(self):
        code, msg = classify_error(RommNotFoundError("missing"))
        assert code == "not_found"
        assert "not found" in msg.lower()

    def test_unsupported_error(self):
        code, msg = classify_error(RommUnsupportedError("save content download", "4.7.0"))
        assert code == "unsupported"
        assert "4.7.0" in msg
        assert "requires RomM" in msg

    def test_generic_api_error(self):
        code, msg = classify_error(RommApiError("some API issue"))
        assert code == "server_unreachable"
        assert msg == "some API issue"

    def test_conflict_error_is_api_error(self):
        """RommConflictError is a subclass of RommApiError, not specifically handled."""
        code, msg = classify_error(RommConflictError("conflict"))
        assert code == "server_unreachable"
        assert msg == "conflict"

    def test_unknown_exception_value_error(self):
        code, msg = classify_error(ValueError("bad value"))
        assert code == "unknown"
        assert msg == "bad value"

    def test_unknown_exception_runtime_error(self):
        code, msg = classify_error(RuntimeError("something broke"))
        assert code == "unknown"
        assert msg == "something broke"

    def test_messages_are_user_friendly_not_tracebacks(self):
        """User-friendly messages should not contain traceback-like text."""
        for exc in [
            RommAuthError("HTTP 401: Unauthorized"),
            RommConnectionError("Connection refused"),
            RommSSLError("certificate verify failed"),
            RommTimeoutError("timed out"),
            RommServerError("Internal Server Error", status_code=500),
        ]:
            _code, msg = classify_error(exc)
            assert "Traceback" not in msg
            assert "File " not in msg

    def test_subclass_ordering_auth_before_api(self):
        """RommAuthError (subclass of RommApiError) should be classified as auth_failed, not server_unreachable."""
        code, _ = classify_error(RommAuthError("auth fail"))
        assert code == "auth_failed"

    def test_subclass_ordering_ssl_before_connection(self):
        """RommSSLError should be classified as server_unreachable even though it's a RommApiError."""
        code, _ = classify_error(RommSSLError("cert fail"))
        assert code == "server_unreachable"


class TestErrorResponse:
    """error_response returns a proper {success, reason, message} dict."""

    def test_structure(self):
        resp = error_response(RommAuthError("401"))
        assert resp["success"] is False
        assert "message" in resp
        assert "reason" in resp
        assert "error_code" not in resp
        assert "error" not in resp

    def test_auth_error_code(self):
        resp = error_response(RommAuthError("unauthorized"))
        assert resp["reason"] == "auth_failed"
        assert resp["success"] is False

    def test_connection_error_code(self):
        resp = error_response(RommConnectionError("refused"))
        assert resp["reason"] == "server_unreachable"

    def test_ssl_error_code(self):
        resp = error_response(RommSSLError("cert fail"))
        assert resp["reason"] == "server_unreachable"

    def test_timeout_error_code(self):
        resp = error_response(RommTimeoutError("timed out"))
        assert resp["reason"] == "server_unreachable"

    def test_server_error_code(self):
        resp = error_response(RommServerError("500", status_code=500))
        assert resp["reason"] == "server_unreachable"

    def test_unknown_error_code(self):
        resp = error_response(ValueError("bad"))
        assert resp["reason"] == "unknown"

    def test_fallback_message_override(self):
        resp = error_response(RommAuthError("401"), fallback_message="Custom message")
        assert resp["message"] == "Custom message"
        assert resp["reason"] == "auth_failed"

    def test_fallback_message_none_uses_default(self):
        resp = error_response(RommAuthError("401"), fallback_message=None)
        assert "Authentication failed" in resp["message"]

    def test_not_found_error_response(self):
        resp = error_response(RommNotFoundError("missing"))
        assert resp["reason"] == "not_found"
        assert resp["success"] is False

    def test_forbidden_error_response(self):
        resp = error_response(RommForbiddenError("forbidden"))
        assert resp["reason"] == "auth_failed"
        assert "Access denied" in resp["message"]

    def test_unsupported_error_response(self):
        resp = error_response(RommUnsupportedError("save content download", "4.7.0"))
        assert resp["reason"] == "unsupported"
        assert resp["success"] is False
        assert "4.7.0" in resp["message"]
