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


class LaunchGateRomLookup(Protocol):
    """Steam app id → RomM ROM resolution consumed by LaunchGateService.

    The composition root satisfies this with ``LibraryService``'s
    registry-backed lookup. Returns ``None`` when the Steam app id
    does not correspond to a tracked RomM ROM — that's the signal the
    gate uses to allow the launch through unmodified.
    """

    def get_rom_by_steam_app_id(self, app_id: int) -> dict | None: ...


class LaunchGateInstalledChecker(Protocol):
    """ROM-installed lookup consumed by LaunchGateService.

    The composition root satisfies this with ``DownloadService``'s
    ``get_installed_rom``. Returns the installed-ROM metadata dict
    when the ROM has been downloaded, ``None`` otherwise. The gate
    treats any falsy return as "not installed".
    """

    def get_installed_rom(self, rom_id: int) -> dict | None: ...


class LaunchGateSaveStatusReader(Protocol):
    """Save-status read consumed by LaunchGateService.

    The composition root satisfies this with ``SaveService``'s
    ``get_save_status``. The gate only consults the returned
    ``conflicts`` array — any non-empty value blocks the launch with
    a save-conflict verdict.
    """

    async def get_save_status(self, rom_id: int) -> dict: ...
