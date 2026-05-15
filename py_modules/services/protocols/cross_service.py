"""Multi-method cross-service Protocols.

When one service needs a small handful of methods from another, the
caller depends on a narrowly-typed Protocol instead of the concrete
service class. This keeps the ``services/`` layer independent (no
service-to-service concrete imports) while still letting one service
delegate a chunk of behavior to another. Each Protocol here is the
narrow seam one consuming service sees of another service's surface.
"""

from __future__ import annotations

from typing import Any, Protocol


class RetryStrategy(Protocol):
    """HTTP retry wrapper pair consumed by SaveService and PlaytimeService."""

    def is_retryable(self, exc: Exception) -> bool: ...

    def with_retry(self, fn: Any, *args: Any, max_attempts: int = 3, base_delay: int = 1, **kwargs: Any) -> Any: ...


class BiosChecker(Protocol):
    """BIOS status checking consumed by GameDetailService."""

    def check_platform_bios_cached(self, platform_slug: str, rom_filename: str | None = None) -> dict | None: ...

    async def check_platform_bios(self, platform_slug: str, rom_filename: str | None = None) -> dict: ...


class AchievementsReader(Protocol):
    """Achievement data access consumed by GameDetailService."""

    def get_ra_username(self) -> str: ...

    def get_progress_cache_entry(self, rom_id_str: str) -> dict | None: ...


class MetadataExtractor(Protocol):
    """Metadata extraction and cache flushing consumed by LibraryService."""

    def extract_metadata(self, rom: dict) -> dict: ...

    def mark_metadata_dirty(self) -> None: ...

    def flush_metadata_if_dirty(self) -> None: ...


class ArtworkManager(Protocol):
    """Artwork operations consumed by LibraryService."""

    async def download_artwork(
        self,
        all_roms: list[dict],
        emit_progress: Any,
        is_cancelling: Any,
        progress_step: int = 4,
        progress_total_steps: int = 6,
    ) -> dict: ...

    def finalize_cover_path(self, grid: str | None, cover_path: str, app_id: int, rom_id_str: str) -> str: ...

    def remove_artwork_files(self, grid: str, rom_id: str | int, entry: dict) -> None: ...


class ArtworkRemover(Protocol):
    """Delete the on-disk artwork files associated with a registry entry.

    Consumed by ``ShortcutRemovalService`` to clean up grid/banner/cover
    files when a shortcut is removed. The exact set of files and the
    naming scheme are an artwork-layer concern — this Protocol exposes
    only the single-entry deletion seam the removal flow needs.
    """

    def remove_artwork_files(self, grid: str, rom_id: str | int, entry: dict) -> None: ...
