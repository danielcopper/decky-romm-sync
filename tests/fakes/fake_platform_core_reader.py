"""In-memory ``PlatformCoreReader`` implementation for service tests.

Mirrors the ``settings.json`` ``platform_cores`` map (RomM platform slug → core
label) so tests can exercise the per-platform layer of the active-core resolver
without wiring a real ``PlatformCoreReaderAdapter`` over a settings dict.
"""

from __future__ import annotations


class FakePlatformCoreReader:
    """Maps a platform slug to its configured core label, or ``None`` when absent.

    ``mapping`` is mutable so a test can seed a per-platform core after
    construction; ``calls`` records each queried slug.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping: dict[str, str] = mapping if mapping is not None else {}
        self.calls: list[str] = []

    def get_platform_core(self, platform_slug: str) -> str | None:
        self.calls.append(platform_slug)
        return self.mapping.get(platform_slug)
