"""Direct unit tests for the @migration_blocked decorator.

The decorator wraps Decky callables on Plugin so they short-circuit with a
blocked-dict whenever ``self._migration_service.is_retrodeck_migration_pending()``
is True. Tests use a minimal fake class with a ``_migration_service`` attribute
to keep them independent from the full Plugin.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from lib.migration_gate import migration_blocked


class _FakeMigrationService:
    def __init__(self, pending: bool):
        self._pending = pending

    def is_retrodeck_migration_pending(self) -> bool:
        return self._pending


class _FakeOwner:
    def __init__(self, pending: bool, ret=None, raise_exc: BaseException | None = None):
        self._migration_service = _FakeMigrationService(pending)
        self._ret = ret
        self._raise = raise_exc
        self.calls: list[tuple[tuple, dict]] = []

    @migration_blocked
    async def do_thing(self, *args, **kwargs):
        """Demo docstring used to verify @functools.wraps preservation."""
        self.calls.append((args, kwargs))
        if self._raise is not None:
            raise self._raise
        return self._ret


class TestMigrationBlockedDecorator:
    @pytest.mark.asyncio
    async def test_returns_blocked_dict_when_pending(self):
        owner = _FakeOwner(pending=True, ret={"success": True, "data": "real"})
        result = await owner.do_thing()
        assert result == {
            "success": False,
            "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
            "blocked_by_migration": True,
        }
        assert owner.calls == []  # wrapped method NOT invoked

    @pytest.mark.asyncio
    async def test_passes_args_kwargs_when_not_pending(self):
        owner = _FakeOwner(pending=False, ret={"success": True})
        await owner.do_thing(1, 2, key="value")
        assert owner.calls == [((1, 2), {"key": "value"})]

    @pytest.mark.asyncio
    async def test_preserves_inner_return_value_when_not_pending(self):
        sentinel = {"success": True, "payload": [1, 2, 3]}
        owner = _FakeOwner(pending=False, ret=sentinel)
        result = await owner.do_thing()
        assert result is sentinel  # exact pass-through, no mutation

    def test_marks_wrapper_with_migration_blocked_attribute(self):
        assert getattr(_FakeOwner.do_thing, "_migration_blocked", False) is True

    @pytest.mark.asyncio
    async def test_works_on_async_def_method(self):
        """Ensures the wrapper awaits correctly — would TypeError otherwise."""
        owner = _FakeOwner(pending=False, ret="awaited-value")
        result = await owner.do_thing()
        assert result == "awaited-value"

    @pytest.mark.asyncio
    async def test_blocked_dict_overrides_non_dict_inner_return(self):
        """Contract: when pending, the wrapper ALWAYS returns the blocked-dict
        regardless of the inner method's return type. Documented behavior so
        callers can branch on ``blocked_by_migration`` uniformly."""

        class _OwnerWithTupleReturn:
            def __init__(self):
                self._migration_service = _FakeMigrationService(pending=True)

            @migration_blocked
            async def returns_tuple(self):
                return (1, 2, 3)

        class _OwnerWithNoneReturn:
            def __init__(self):
                self._migration_service = _FakeMigrationService(pending=True)

            @migration_blocked
            async def returns_none(self):
                return None

        class _OwnerWithListReturn:
            def __init__(self):
                self._migration_service = _FakeMigrationService(pending=True)

            @migration_blocked
            async def returns_list(self):
                return [1, 2, 3]

        for owner_cls, attr in (
            (_OwnerWithTupleReturn, "returns_tuple"),
            (_OwnerWithNoneReturn, "returns_none"),
            (_OwnerWithListReturn, "returns_list"),
        ):
            result = await getattr(owner_cls(), attr)()
            assert isinstance(result, dict)
            assert result["blocked_by_migration"] is True
            assert result["success"] is False

    def test_preserves_function_metadata_via_wraps(self):
        """@functools.wraps copies __name__ and __doc__ onto the wrapper so
        introspection (and test reporters) see the original method."""
        assert _FakeOwner.do_thing.__name__ == "do_thing"
        assert _FakeOwner.do_thing.__doc__ == ("Demo docstring used to verify @functools.wraps preservation.")

    @pytest.mark.asyncio
    async def test_decorator_no_op_when_migration_service_missing(self):
        """Defensive path: if _migration_service is absent, the wrapper must
        still call through to the wrapped method instead of raising."""

        class _OwnerWithoutService:
            @migration_blocked
            async def do(self):
                return "ok"

        result = await _OwnerWithoutService().do()
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_returns_blocked_dict_using_mock_service(self):
        """Mock-based variant of the pending check — confirms the wrapper
        uses ``is_retrodeck_migration_pending`` exactly once per call."""

        class _Owner:
            def __init__(self, svc):
                self._migration_service = svc

            @migration_blocked
            async def do(self):
                return "real-call"

        svc = MagicMock()
        svc.is_retrodeck_migration_pending.return_value = True
        owner = _Owner(svc)
        result = cast("dict[str, Any]", await owner.do())
        assert result["blocked_by_migration"] is True
        svc.is_retrodeck_migration_pending.assert_called_once_with()
