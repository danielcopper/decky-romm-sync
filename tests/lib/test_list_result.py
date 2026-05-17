"""Tests for the ListResult discriminated helper."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lib.list_result import ErrorCode, ListResult


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


class TestListResultOk:
    """ListResult.ok wraps a list and signals success."""

    def test_with_populated_list(self):
        result: ListResult[int] = ListResult.ok([1, 2, 3])
        assert result.items == [1, 2, 3]
        assert result.error is None
        assert result.error_message is None

    def test_with_empty_list_is_success_not_failure(self):
        """Empty list is "server answered, nothing matched" — still success."""
        result: ListResult[str] = ListResult.ok([])
        assert result.items == []
        assert result.error is None
        assert result.error_message is None

    def test_with_dict_items(self):
        items = [{"id": 1, "name": "Mario"}, {"id": 2, "name": "Luigi"}]
        result: ListResult[dict[str, object]] = ListResult.ok(items)
        assert result.items == items
        assert result.error is None


class TestListResultFailed:
    """ListResult.failed wraps an error code and optional message."""

    def test_with_code_and_message(self):
        result: ListResult[int] = ListResult.failed(ErrorCode.SERVER_UNREACHABLE, "connection refused")
        assert result.items is None
        assert result.error is ErrorCode.SERVER_UNREACHABLE
        assert result.error_message == "connection refused"

    def test_with_code_only(self):
        result: ListResult[int] = ListResult.failed(ErrorCode.AUTH_FAILED)
        assert result.items is None
        assert result.error is ErrorCode.AUTH_FAILED
        assert result.error_message is None

    @pytest.mark.parametrize(
        "code",
        [ErrorCode.SERVER_UNREACHABLE, ErrorCode.AUTH_FAILED, ErrorCode.UNKNOWN],
    )
    def test_each_error_code(self, code):
        result: ListResult[int] = ListResult.failed(code, "boom")
        assert result.error is code
        assert result.error_message == "boom"


class TestDiscriminatorInvariant:
    """Exactly one of items/error must be set — both or neither is a bug."""

    def test_both_none_raises(self):
        with pytest.raises(ValueError, match="exactly one of"):
            ListResult[int]()

    def test_both_set_raises(self):
        with pytest.raises(ValueError, match="exactly one of"):
            ListResult[int](items=[1], error=ErrorCode.UNKNOWN)

    def test_both_set_with_empty_list_still_raises(self):
        """Empty list is still "items is set"; it should not be treated as missing."""
        with pytest.raises(ValueError, match="exactly one of"):
            ListResult[int](items=[], error=ErrorCode.UNKNOWN)

    def test_error_message_without_error_raises(self):
        with pytest.raises(ValueError, match="error_message must be None"):
            ListResult[int](items=[1], error=None, error_message="should not be set")


class TestImmutability:
    """frozen=True locks fields after construction."""

    def test_cannot_reassign_items(self):
        result: ListResult[int] = ListResult.ok([1])
        with pytest.raises(FrozenInstanceError):
            result.items = [2]  # type: ignore[misc]

    def test_cannot_reassign_error(self):
        result: ListResult[int] = ListResult.failed(ErrorCode.UNKNOWN)
        with pytest.raises(FrozenInstanceError):
            result.error = ErrorCode.AUTH_FAILED  # type: ignore[misc]


class TestConsumerPattern:
    """The documented `error is None` + `assert items is not None` flow.

    basedpyright in basic mode does not co-narrow two independent
    ``Optional`` fields, so the project consumer pattern pairs the
    discriminator check with one explicit assertion before reading items.
    These tests exercise that exact shape so any future refactor that
    breaks it (e.g. splitting into typed subtypes) is forced to update the
    consumer expectations here too.
    """

    def test_success_branch_can_iterate_items(self):
        result: ListResult[int] = ListResult.ok([10, 20, 30])
        if result.error is None:
            assert result.items is not None
            total = sum(result.items)
        else:
            total = -1
        assert total == 60

    def test_failure_branch_routes_on_code(self):
        result: ListResult[int] = ListResult.failed(ErrorCode.AUTH_FAILED, "bad password")
        if result.error is None:
            outcome = "ok"
        elif result.error is ErrorCode.AUTH_FAILED:
            outcome = "reauth"
        else:
            outcome = "other"
        assert outcome == "reauth"

    def test_consumer_transforming_items(self):
        """Realistic consumer pattern — read items only on success."""
        result: ListResult[str] = ListResult.ok(["a", "b", "c"])
        collected: list[str] = []
        if result.error is None:
            assert result.items is not None
            for item in result.items:
                collected.append(item.upper())
        assert collected == ["A", "B", "C"]
