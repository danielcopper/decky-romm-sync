"""FirmwareCacheEntry — one item of RomM's TTL-cached firmware inventory.

A snapshot of one firmware entry the server reports, cached with a short TTL so
the BIOS-management UI doesn't re-query RomM on every open. The cache is
replaced wholesale on refresh and the TTL check lives in the firmware service,
so this aggregate stays a thin record.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class FirmwareCacheEntry:
    """One cached firmware-inventory item from RomM."""

    id: int | None
    name: str
    platform_slug: str
    file_size_bytes: int
    cached_at: float

    @classmethod
    def cached(
        cls,
        *,
        id: int | None,
        name: str,
        platform_slug: str,
        file_size_bytes: int,
        cached_at: float,
    ) -> FirmwareCacheEntry:
        """Build a cached firmware entry recorded at Unix time ``cached_at``."""
        return cls(
            id=id,
            name=name,
            platform_slug=platform_slug,
            file_size_bytes=file_size_bytes,
            cached_at=cached_at,
        )
