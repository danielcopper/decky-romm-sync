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


class SessionPlaytimeRecorder(Protocol):
    """Playtime end-of-session record consumed by SessionLifecycleService.

    The composition root satisfies this with ``PlaytimeService``'s
    ``record_session_end``. The lifecycle service forwards the
    ``total_seconds`` field to the frontend so the playtime display can
    be updated; a falsy ``success`` value yields ``total_seconds=None``
    on the returned DTO so the frontend leaves the display untouched.
    """

    async def record_session_end(self, rom_id: int) -> dict: ...


class SessionPostExitSync(Protocol):
    """Post-exit save sync consumed by SessionLifecycleService.

    The composition root satisfies this with ``SaveService``'s
    ``post_exit_sync``. Returned shape carries ``offline`` / ``success``
    / ``synced`` / ``conflicts`` which the lifecycle service maps into
    toast strings; any raised exception is collapsed to the "failed"
    toast.
    """

    async def post_exit_sync(self, rom_id: int) -> dict: ...


class SessionAchievementSync(Protocol):
    """Post-session achievement refresh consumed by SessionLifecycleService.

    The composition root satisfies this with ``AchievementsService``'s
    ``sync_achievements_after_session``. The lifecycle service kicks
    this off as a background task — its result and any failure are
    logged backend-side; the frontend never observes the outcome.
    """

    async def sync_achievements_after_session(self, rom_id: int) -> dict: ...


class SessionMigrationReader(Protocol):
    """Migration-state refresh + pending check consumed by SessionLifecycleService.

    The composition root satisfies this with ``MigrationService``'s
    ``refresh_state`` and ``is_retrodeck_migration_pending``. The
    refresh result is repacked into the typed DTO the frontend feeds
    into its migration stores; the pending check matches the safety
    net the ``@migration_blocked`` decorator provides for other
    callables, gating the destructive post-exit save sync from inside
    the lifecycle orchestration.
    """

    async def refresh_state(self) -> dict: ...

    def is_retrodeck_migration_pending(self) -> bool: ...
