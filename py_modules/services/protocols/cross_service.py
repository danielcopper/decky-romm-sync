"""Multi-method cross-service Protocols.

When one service needs a small handful of methods from another, the
caller depends on a narrowly-typed Protocol instead of the concrete
service class. This keeps the ``services/`` layer independent (no
service-to-service concrete imports) while still letting one service
delegate a chunk of behavior to another. Each Protocol here is the
narrow seam one consuming service sees of another service's surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from models.state import InstalledRomEntry, ShortcutRegistryEntry

    from domain.disc_selection import Disc
    from domain.rom_install import RomInstall
    from domain.save_layout import SaveLayout


class RetryStrategy(Protocol):
    """HTTP retry wrapper pair consumed by SaveService and PlaytimeService."""

    def is_retryable(self, exc: Exception) -> bool: ...

    def with_retry(self, fn: Any, *args: Any, max_attempts: int = 3, base_delay: int = 1, **kwargs: Any) -> Any: ...


class BiosChecker(Protocol):
    """BIOS status checking consumed by GameDetailService and CoreService.

    Both methods take a pre-resolved ``active_core_so`` rather than a ROM
    filename: the per-game active core is resolved upstream (GameDetailService
    runs ``ActiveCoreReader.active_core_for_rom`` where it already holds the
    ``rom_id``) so the BIOS filter never re-derives the core. ``None`` means "use
    the system default" — the standalone platform-level checks (the
    ``check_platform_bios`` callable, the post-system-core-write recheck) pass
    ``None``; the per-game game-detail path passes the resolved ``.so``.
    """

    def check_platform_bios_cached(
        self, platform_slug: str, active_core_so: str | None = None
    ) -> dict[str, Any] | None: ...

    async def check_platform_bios(self, platform_slug: str, active_core_so: str | None = None) -> dict[str, Any]: ...


class ActiveCoreReader(Protocol):
    """Per-ROM active-core resolution consumed by the read-path core consumers.

    The composition root satisfies this with ``ActiveCoreResolver``. Consumers
    (BIOS status, per-core save dir, save-emulator tag, core-change detection,
    and the launch-bake sites) ask "which ``.so`` will this ROM launch with?"
    and operate entirely in ``.so`` space — the resolver runs the stored
    ``emulator_override`` LABEL through ``label_to_core_so`` so no consumer ever
    sees the raw DB label. ``(None, None)`` means the system has no configured
    core; a stale override degrades to the system default rather than raising.
    """

    def active_core_for_rom(self, rom_id: int) -> tuple[str | None, str | None]: ...


class DiscResolver(Protocol):
    """Per-ROM multi-disc launch-path resolution consumed by the bake sites.

    The composition root satisfies this with ``DiscLaunchResolver``. The three
    launch-bake sites (library sync, download-complete, RetroDECK-home migration)
    and the disc-picker callables ask "which file does this installed ROM launch
    with, given its persisted disc pick?" and operate entirely in path space.
    :meth:`enumerate_discs` lists the launchable discs in disc order (empty for a
    single-file ROM); :meth:`resolve_bake_path` resolves the pin over that list;
    :meth:`resolve_for_install` is the bake-site convenience that does both. A
    non-multi-disc ROM resolves to its own ``file_path`` unchanged; a stale pin
    degrades to the default with a WARNING rather than raising.
    """

    def enumerate_discs(self, install: RomInstall) -> list[Disc]: ...

    def resolve_bake_path(self, install: RomInstall, discs: list[Disc], selected_disc: str | None) -> str: ...

    def resolve_for_install(self, install: RomInstall, selected_disc: str | None) -> str: ...


class AchievementsReader(Protocol):
    """Achievement data access consumed by GameDetailService."""

    def get_ra_username(self) -> str: ...

    def get_progress_cache_entry(self, rom_id_str: str) -> dict[str, Any] | None: ...


class ArtworkManager(Protocol):
    """Artwork operations consumed by LibraryService."""

    async def download_artwork(
        self,
        all_roms: list[dict[str, Any]],
        emit_progress: Any,
        is_cancelling: Any,
        progress_step: int = 4,
        progress_total_steps: int = 6,
    ) -> dict[Any, Any]: ...

    def finalize_cover_path(self, grid: str | None, cover_path: str, app_id: int, rom_id_str: str) -> str: ...

    def remove_artwork_files(self, grid: str, rom_id: str | int, entry: ShortcutRegistryEntry) -> None: ...


class ArtworkRemover(Protocol):
    """Delete the on-disk artwork files associated with a registry entry.

    Consumed by ``ShortcutRemovalService`` to clean up grid/banner/cover
    files when a shortcut is removed. The exact set of files and the
    naming scheme are an artwork-layer concern — this Protocol exposes
    only the single-entry deletion seam the removal flow needs.
    """

    def remove_artwork_files(self, grid: str, rom_id: str | int, entry: ShortcutRegistryEntry) -> None: ...


class LaunchGateRomLookup(Protocol):
    """Steam app id → RomM ROM resolution consumed by LaunchGateService.

    The composition root satisfies this with ``LibraryService``'s
    registry-backed lookup. Returns ``None`` when the Steam app id
    does not correspond to a tracked RomM ROM — that's the signal the
    gate uses to allow the launch through unmodified.
    """

    def get_rom_by_steam_app_id(self, app_id: int) -> dict[str, Any] | None: ...


class LaunchGateInstalledChecker(Protocol):
    """ROM-installed lookup consumed by LaunchGateService.

    The composition root satisfies this with ``DownloadService``'s
    ``get_installed_rom``. Returns the installed-ROM metadata entry
    when the ROM has been downloaded, ``None`` otherwise. The gate
    treats any falsy return as "not installed".
    """

    def get_installed_rom(self, rom_id: int) -> InstalledRomEntry | None: ...


class LaunchGateSaveStatusReader(Protocol):
    """Save-status surface consumed by LaunchGateService.

    The composition root satisfies this with ``SaveService``. The gate
    first consults ``is_save_sync_enabled`` — when the feature toggle is
    off there is no conflict state to gate on, so the gate allows the
    launch and skips the ``get_save_status`` round-trip entirely. With
    save-sync on, it calls ``get_save_status`` for the canonical conflict
    signal (a non-empty ``conflicts`` array blocks the launch) and falls
    back to the synchronous ``has_tracked_save`` in-memory check to decide
    whether a ``get_save_status`` failure should be soft-warned (ROM has
    tracked saves — silent allow would risk data loss) or silently
    allowed (no tracked saves — nothing to corrupt).
    """

    def is_save_sync_enabled(self) -> bool: ...

    async def get_save_status(self, rom_id: int) -> dict[str, Any]: ...

    def has_tracked_save(self, rom_id: int) -> bool: ...


class SessionPlaytimeRecorder(Protocol):
    """Playtime end-of-session record consumed by SessionLifecycleService.

    The composition root satisfies this with ``PlaytimeService``'s
    ``record_session_end``. The lifecycle service forwards the
    ``total_seconds`` field to the frontend so the playtime display can
    be updated; a falsy ``success`` value yields ``total_seconds=None``
    on the returned DTO so the frontend leaves the display untouched.
    """

    async def record_session_end(self, rom_id: int) -> dict[str, Any]: ...


class SessionPostExitSync(Protocol):
    """Post-exit save sync consumed by SessionLifecycleService.

    The composition root satisfies this with ``SaveService``'s
    ``post_exit_sync``. Returned shape carries ``offline`` / ``success``
    / ``synced`` / ``conflicts`` which the lifecycle service maps into
    toast strings; any raised exception is collapsed to the "failed"
    toast.
    """

    async def post_exit_sync(self, rom_id: int) -> dict[str, Any]: ...


class SessionAchievementSync(Protocol):
    """Post-session achievement refresh consumed by SessionLifecycleService.

    The composition root satisfies this with ``AchievementsService``'s
    ``sync_achievements_after_session``. The lifecycle service kicks
    this off as a background task — its result and any failure are
    logged backend-side; the frontend never observes the outcome.
    """

    async def sync_achievements_after_session(self, rom_id: int) -> dict[str, Any]: ...


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

    async def refresh_state(self) -> object: ...

    def is_retrodeck_migration_pending(self) -> bool: ...


class SaveSortChangeFn(Protocol):
    """Save-sort-change refresh consumed by SaveService.

    The composition root satisfies this with
    ``MigrationService.detect_save_sort_change``. SaveService invokes
    this at the entry point of ``pre_launch_sync`` and
    ``post_exit_sync`` to refresh save-sort state from the live
    RetroArch config before computing ``saves_dir`` (#238). It returns
    the live ``SaveLayout`` it just observed: the SyncEngine reads this
    to hard-gate save sync when the layout is ``ContentDir`` (#239).
    """

    def __call__(self) -> SaveLayout: ...


class MigrationPendingFn(Protocol):
    """Pending-RetroDECK-migration check consumed by SaveService.

    The composition root satisfies this with
    ``MigrationService.is_retrodeck_migration_pending``. SaveService
    gates destructive operations on this signal.
    """

    def __call__(self) -> bool: ...
