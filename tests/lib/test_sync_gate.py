"""Direct unit tests for the @sync_active_blocked decorator.

The decorator wraps Decky callables on Plugin so they short-circuit with the
canonical failure shape whenever ``self._sync_service.is_sync_in_flight()``
is True. ``_sync_service`` is a hard requirement: a missing/None service is
a wiring regression and the wrapper raises ``RuntimeError`` rather than
silently skipping the safety gate. Tests use a minimal fake class with a
``_sync_service`` attribute to keep them independent from the full Plugin.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from lib.sync_gate import sync_active_blocked

_EXPECTED_BLOCKED_DICT = {
    "success": False,
    "reason": "sync_active",
    "message": "A library sync is in progress — wait for it to finish or cancel it before removing shortcuts or ROMs.",
}


class _FakeSyncService:
    def __init__(self, in_flight: bool):
        self._in_flight = in_flight

    def is_sync_in_flight(self) -> bool:
        return self._in_flight


class _FakeOwner:
    def __init__(self, in_flight: bool, ret=None):
        self._sync_service = _FakeSyncService(in_flight)
        self._ret = ret
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @sync_active_blocked
    async def do_thing(self, *args, **kwargs):
        """Demo docstring used to verify @functools.wraps preservation."""
        self.calls.append((args, kwargs))
        return self._ret


class TestSyncActiveBlockedDecorator:
    @pytest.mark.asyncio
    async def test_returns_canonical_failure_shape_when_in_flight(self):
        """The refusal dict is pinned exactly: success/reason/message, no extras."""
        owner = _FakeOwner(in_flight=True, ret={"success": True, "data": "real"})
        result = await owner.do_thing()
        assert result == _EXPECTED_BLOCKED_DICT
        assert owner.calls == []  # wrapped method NOT invoked

    @pytest.mark.asyncio
    async def test_passes_args_kwargs_when_not_in_flight(self):
        """A gated callable with args (e.g. platform_slug) passes them through."""
        owner = _FakeOwner(in_flight=False, ret={"success": True})
        await owner.do_thing("n64", dry_run=True)
        assert owner.calls == [(("n64",), {"dry_run": True})]

    @pytest.mark.asyncio
    async def test_preserves_inner_return_value_when_not_in_flight(self):
        sentinel = {"success": True, "app_ids": [1001, 1002]}
        owner = _FakeOwner(in_flight=False, ret=sentinel)
        result = await owner.do_thing()
        assert result is sentinel  # exact pass-through, no mutation

    def test_marks_wrapper_with_sync_active_blocked_attribute(self):
        assert getattr(_FakeOwner.do_thing, "_sync_active_blocked", False) is True

    def test_preserves_function_metadata_via_wraps(self):
        """@functools.wraps copies __name__ and __doc__ onto the wrapper so
        introspection (and test reporters) see the original method."""
        assert _FakeOwner.do_thing.__name__ == "do_thing"
        assert _FakeOwner.do_thing.__doc__ == ("Demo docstring used to verify @functools.wraps preservation.")

    @pytest.mark.asyncio
    async def test_raises_when_sync_service_attribute_absent(self):
        """The gate is a hard data-safety requirement: if _sync_service is
        missing entirely (no attribute), the wrapper must RAISE rather than
        silently call through — a wiring regression must fail loud."""

        class _OwnerWithoutService:
            def __init__(self):
                self.called = False

            @sync_active_blocked
            async def do(self):
                self.called = True
                return "ok"

        owner = _OwnerWithoutService()
        with pytest.raises(RuntimeError) as exc_info:
            await owner.do()
        # Message names the gated method and explains the unwired gate.
        assert "do" in str(exc_info.value)
        assert "_sync_service" in str(exc_info.value)
        # The gated callable must NOT have run without the safety gate.
        assert owner.called is False

    @pytest.mark.asyncio
    async def test_raises_when_sync_service_is_none(self):
        """A present-but-None _sync_service is also unwired → raises.

        Mirrors the absent-attribute case for the ``getattr(..., None)`` path:
        an explicitly-None service is just as much a wiring regression.
        """

        class _OwnerWithNoneService:
            def __init__(self):
                self._sync_service = None
                self.called = False

            @sync_active_blocked
            async def do(self):
                self.called = True
                return "ok"

        owner = _OwnerWithNoneService()
        with pytest.raises(RuntimeError) as exc_info:
            await owner.do()
        assert "do" in str(exc_info.value)
        assert "_sync_service" in str(exc_info.value)
        assert owner.called is False

    @pytest.mark.asyncio
    async def test_returns_blocked_dict_using_mock_service(self):
        """Mock-based variant of the in-flight check — confirms the wrapper
        uses ``is_sync_in_flight`` exactly once per call."""

        class _Owner:
            def __init__(self, svc):
                self._sync_service = svc

            @sync_active_blocked
            async def do(self):
                return "real-call"

        svc = MagicMock()
        svc.is_sync_in_flight.return_value = True
        owner = _Owner(svc)
        result = cast("dict[str, Any]", await owner.do())
        assert result == _EXPECTED_BLOCKED_DICT
        svc.is_sync_in_flight.assert_called_once_with()
