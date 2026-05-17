"""Tests for the ListResult typed-subtype union helper."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lib.list_result import ErrorCode, FailedListResult, ListResult, OkListResult


class TestErrorCode:
    """ErrorCode is a str-enum with the documented members."""

    def test_is_str_subclass(self):
        # str-enum members compare equal to their string value, which lets
        # callers serialize/transport the code without an explicit .value.
        assert isinstance(ErrorCode.SERVER_UNREACHABLE, str)
        assert ErrorCode.SERVER_UNREACHABLE == "server_unreachable"

    def test_required_members_present(self):
        assert ErrorCode.SERVER_UNREACHABLE.value == "server_unreachable"
        assert ErrorCode.AUTH_FAILED.value == "auth_failed"
        assert ErrorCode.UNKNOWN.value == "unknown"


class TestOkListResult:
    """OkListResult wraps the fetched list — success branch of the union."""

    def test_with_populated_list(self):
        result = OkListResult(items=[1, 2, 3])
        assert result.items == [1, 2, 3]

    def test_with_empty_list_is_success_not_failure(self):
        """Empty list is "server answered, nothing matched" — still success."""
        result: OkListResult[str] = OkListResult(items=[])
        assert result.items == []

    def test_with_dict_items(self):
        items = [{"id": 1, "name": "Mario"}, {"id": 2, "name": "Luigi"}]
        result = OkListResult(items=items)
        assert result.items == items


class TestFailedListResult:
    """FailedListResult carries an error code and optional message."""

    def test_with_code_and_message(self):
        result = FailedListResult(error=ErrorCode.SERVER_UNREACHABLE, error_message="connection refused")
        assert result.error is ErrorCode.SERVER_UNREACHABLE
        assert result.error_message == "connection refused"

    def test_with_code_only(self):
        result = FailedListResult(error=ErrorCode.AUTH_FAILED)
        assert result.error is ErrorCode.AUTH_FAILED
        assert result.error_message is None

    @pytest.mark.parametrize(
        "code",
        [ErrorCode.SERVER_UNREACHABLE, ErrorCode.AUTH_FAILED, ErrorCode.UNKNOWN],
    )
    def test_each_error_code(self, code):
        result = FailedListResult(error=code, error_message="boom")
        assert result.error is code
        assert result.error_message == "boom"


class TestEquality:
    """Frozen dataclasses get value-based equality for free."""

    def test_two_ok_with_same_items_are_equal(self):
        assert OkListResult(items=[1, 2]) == OkListResult(items=[1, 2])

    def test_two_ok_with_different_items_are_not_equal(self):
        assert OkListResult(items=[1, 2]) != OkListResult(items=[1, 3])

    def test_two_failed_with_same_code_are_equal(self):
        assert FailedListResult(error=ErrorCode.UNKNOWN) == FailedListResult(error=ErrorCode.UNKNOWN)

    def test_two_failed_with_same_code_and_message_are_equal(self):
        left = FailedListResult(error=ErrorCode.AUTH_FAILED, error_message="bad password")
        right = FailedListResult(error=ErrorCode.AUTH_FAILED, error_message="bad password")
        assert left == right

    def test_failed_and_ok_are_never_equal(self):
        assert OkListResult(items=[]) != FailedListResult(error=ErrorCode.UNKNOWN)


class TestImmutability:
    """frozen=True locks fields after construction."""

    def test_cannot_reassign_ok_items(self):
        result = OkListResult(items=[1])
        with pytest.raises(FrozenInstanceError):
            result.items = [2]  # type: ignore[misc]

    def test_cannot_reassign_failed_error(self):
        result = FailedListResult(error=ErrorCode.UNKNOWN)
        with pytest.raises(FrozenInstanceError):
            result.error = ErrorCode.AUTH_FAILED  # type: ignore[misc]

    def test_cannot_reassign_failed_message(self):
        result = FailedListResult(error=ErrorCode.UNKNOWN, error_message="x")
        with pytest.raises(FrozenInstanceError):
            result.error_message = "y"  # type: ignore[misc]


class TestIsinstanceNarrowing:
    """isinstance(r, FailedListResult) narrows BOTH branches under basic mode.

    The success branch must reach ``.items`` without an extra assertion —
    that is the contract that motivates the typed-subtype shape over a
    single dataclass with two Optional fields.
    """

    def test_success_branch_iterates_items_without_assert(self):
        result: ListResult[int] = OkListResult(items=[10, 20, 30])
        # SIM108: deliberately block-style — the point of this test is that
        # the `else` branch narrows to OkListResult and `result.items` is
        # reachable without an assert. A ternary would hide the narrowing.
        if isinstance(result, FailedListResult):  # noqa: SIM108
            total = -1
        else:
            total = sum(result.items)
        assert total == 60

    def test_failure_branch_routes_on_code(self):
        result: ListResult[int] = FailedListResult(error=ErrorCode.AUTH_FAILED, error_message="bad password")
        if isinstance(result, FailedListResult):
            outcome = "reauth" if result.error is ErrorCode.AUTH_FAILED else "other"
        else:
            outcome = "ok"
        assert outcome == "reauth"

    def test_consumer_transforming_items(self):
        """Realistic consumer pattern — read items only on success, no assert."""
        result: ListResult[str] = OkListResult(items=["a", "b", "c"])
        collected: list[str] = []
        if isinstance(result, FailedListResult):
            pass
        else:
            for item in result.items:
                collected.append(item.upper())
        assert collected == ["A", "B", "C"]


class TestMatchNarrowing:
    """``match`` statement narrows the union cleanly too."""

    def test_match_success_branch(self):
        result: ListResult[int] = OkListResult(items=[1, 2, 3])
        match result:
            case OkListResult(items=items):
                total = sum(items)
            case FailedListResult():
                total = -1
        assert total == 6

    def test_match_failure_branch(self):
        result: ListResult[int] = FailedListResult(error=ErrorCode.SERVER_UNREACHABLE)
        match result:
            case OkListResult():
                outcome = "ok"
            case FailedListResult(error=ErrorCode.SERVER_UNREACHABLE):
                outcome = "retry"
            case FailedListResult():
                outcome = "other"
        assert outcome == "retry"

    def test_match_empty_ok(self):
        result: ListResult[str] = OkListResult(items=[])
        match result:
            case OkListResult(items=items):
                count = len(items)
            case FailedListResult():
                count = -1
        assert count == 0
