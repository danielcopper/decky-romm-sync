"""Decorator that blocks destructive Decky callables while a library sync is in flight.

The decorated method must be ``async def`` (every Decky callable is). The wrapped
method's owner class **must** expose a ``_sync_service`` attribute with an
``is_sync_in_flight() -> bool`` method — this gate is a data-safety guard and
refuses to run without it. In flight means the live sync state is RUNNING or
CANCELLING; IDLE (which paused and completed runs reset to) passes through.

A missing ``_sync_service`` is a wiring regression, not a tolerable state: the
wrapper raises ``RuntimeError`` rather than silently skipping the gate. In
correctly-wired production the attribute is always present (``main.py:_main``
binds it; the contract harness binds the same set), so the raise never fires
normally — but a regression that drops the wiring fails loud (and the contract
tests, which drive gated callables over the real bootstrap, catch it in CI)
instead of silently disabling the gate for every gated callable.

Tests can introspect blocked callables via the ``_sync_active_blocked``
attribute attached to the wrapper.

Lives in ``lib/`` to stay outside the services/adapters/domain dependency
graph (per import-linter contracts).
"""

from __future__ import annotations

import functools
from typing import Any

_BLOCKED_MESSAGE = (
    "A library sync is in progress — wait for it to finish or cancel it before removing shortcuts or ROMs."
)


def sync_active_blocked(method):
    """Block this Decky callable when ``is_sync_in_flight()`` is True.

    Returns the canonical failure shape ``{success: False, reason:
    "sync_active", message}`` instead of running the gated callable while a
    library-sync run is in flight. Requires the owner to expose
    ``_sync_service``; raises ``RuntimeError`` if it is missing (a wiring
    regression) so the safety gate fails loud rather than silently disabling
    itself for the gated callable.
    """

    @functools.wraps(method)
    async def wrapper(self, *args: Any, **kwargs: Any):
        service = getattr(self, "_sync_service", None)
        if service is None:
            raise RuntimeError(
                f"@sync_active_blocked on {method.__name__!r}: _sync_service is unwired. "
                "The sync safety gate is a hard requirement; refusing to run the gated "
                "callable without it (this is a wiring regression, not a tolerable state)."
            )
        if service.is_sync_in_flight():
            return {
                "success": False,
                "reason": "sync_active",
                "message": _BLOCKED_MESSAGE,
            }
        return await method(self, *args, **kwargs)

    wrapper._sync_active_blocked = True  # type: ignore[attr-defined]
    return wrapper
