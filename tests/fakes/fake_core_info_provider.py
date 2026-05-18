"""In-memory ``CoreInfoProvider`` implementation for service tests."""

from __future__ import annotations


class FakeCoreInfoProvider:
    """In-memory CoreInfoProvider for tests.

    Returns the configured active_core and available_cores for any system.
    Both attributes are mutable so tests can set them directly on the
    instance before exercising the code under test. ``reset_cache``
    increments ``reset_cache_count`` so writers can assert the cache
    was invalidated after a write.
    """

    def __init__(
        self,
        *,
        active_core: tuple[str | None, str | None] = (None, None),
        available_cores: list[dict] | None = None,
    ) -> None:
        self.active_core = active_core
        self.available_cores: list[dict] = available_cores if available_cores is not None else []
        self.reset_cache_count = 0

    def get_active_core(
        self,
        system_name: str,
        rom_filename: str | None = None,
    ) -> tuple[str | None, str | None]:
        return self.active_core

    def get_available_cores(self, system_name: str) -> list[dict]:
        return self.available_cores

    def reset_cache(self) -> None:
        self.reset_cache_count += 1
