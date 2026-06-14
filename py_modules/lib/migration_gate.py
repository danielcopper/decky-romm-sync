"""Decorator that blocks Decky callables while a RetroDECK migration is pending.

The decorated method must be ``async def`` (every Decky callable is). The wrapped
method's owner class **must** expose a ``_migration_service`` attribute with an
``is_retrodeck_migration_pending() -> bool`` method — this gate is a data-safety
guard and refuses to run without it.

A missing ``_migration_service`` is a wiring regression, not a tolerable state:
the wrapper raises ``RuntimeError`` rather than silently skipping the gate. In
correctly-wired production the attribute is always present (``main.py:_main``
binds it; the contract harness binds the same set), so the raise never fires
normally — but a regression that drops the wiring fails loud (and the contract
tests, which drive gated callables over the real bootstrap, catch it in CI)
instead of silently disabling the gate for every gated callable.

Tests can introspect blocked callables via the ``_migration_blocked`` attribute
attached to the wrapper.

Lives in ``lib/`` to stay outside the services/adapters/domain dependency
graph (per import-linter contracts).
"""

from __future__ import annotations

import functools
from typing import Any


def migration_blocked(method):
    """Block this Decky callable when ``is_retrodeck_migration_pending()`` is True.

    Requires the owner to expose ``_migration_service``; raises ``RuntimeError``
    if it is missing (a wiring regression) so the safety gate fails loud rather
    than silently disabling itself for the gated callable.
    """

    @functools.wraps(method)
    async def wrapper(self, *args: Any, **kwargs: Any):
        service = getattr(self, "_migration_service", None)
        if service is None:
            raise RuntimeError(
                f"@migration_blocked on {method.__name__!r}: _migration_service is unwired. "
                "The migration safety gate is a hard requirement; refusing to run the gated "
                "callable without it (this is a wiring regression, not a tolerable state)."
            )
        if service.is_retrodeck_migration_pending():
            return {
                "success": False,
                "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                "blocked_by_migration": True,
            }
        return await method(self, *args, **kwargs)

    wrapper._migration_blocked = True  # type: ignore[attr-defined]
    return wrapper
