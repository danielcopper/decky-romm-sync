"""In-memory ``CoreInfoProvider`` implementation for service tests."""

from __future__ import annotations

from typing import Any


class FakeCoreInfoProvider:
    """In-memory CoreInfoProvider for tests.

    Returns the configured active_core and available_cores for any system.
    Both attributes are mutable so tests can set them directly on the
    instance before exercising the code under test. ``reset_cache``
    increments ``reset_cache_count`` so writers can assert the cache
    was invalidated after a write. ``active_core_calls`` and
    ``available_cores_calls`` record the ``system_name`` each seam was
    invoked with so callers can assert a normalized system (not the raw
    platform slug) reached the read seam.
    """

    def __init__(
        self,
        *,
        active_core: tuple[str | None, str | None] = (None, None),
        available_cores: list[dict[str, Any]] | None = None,
    ) -> None:
        self.active_core = active_core
        self.available_cores: list[dict[str, Any]] = available_cores if available_cores is not None else []
        self.reset_cache_count = 0
        self.active_core_calls: list[str] = []
        self.available_cores_calls: list[str] = []

    def get_active_core(self, system_name: str) -> tuple[str | None, str | None]:
        self.active_core_calls.append(system_name)
        return self.active_core

    def get_available_cores(self, system_name: str) -> list[dict[str, Any]]:
        self.available_cores_calls.append(system_name)
        return self.available_cores

    def reset_cache(self) -> None:
        self.reset_cache_count += 1
