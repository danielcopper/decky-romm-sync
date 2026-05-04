"""Decorator that blocks Decky callables while a RetroDECK migration is pending.

The decorated method must be ``async def`` (every Decky callable is). The wrapped
method's owner class must expose a ``_migration_service`` attribute with an
``is_retrodeck_migration_pending() -> bool`` method.

Tests can introspect blocked callables via the
``_migration_blocked`` attribute attached to the wrapper.

Lives in ``lib/`` to stay outside the services/adapters/domain dependency
graph (per import-linter contracts).
"""

from __future__ import annotations

import functools
from typing import Any


def migration_blocked(method):
    """Block this Decky callable when ``is_retrodeck_migration_pending()`` is True."""

    @functools.wraps(method)
    async def wrapper(self, *args: Any, **kwargs: Any):
        # Defensive: skip the gate cleanly if _migration_service is missing
        # (e.g. tests that haven't wired it). Prefer no-op over AttributeError.
        service = getattr(self, "_migration_service", None)
        if service is not None and service.is_retrodeck_migration_pending():
            return {
                "success": False,
                "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                "blocked_by_migration": True,
            }
        return await method(self, *args, **kwargs)

    wrapper._migration_blocked = True  # type: ignore[attr-defined]
    return wrapper
