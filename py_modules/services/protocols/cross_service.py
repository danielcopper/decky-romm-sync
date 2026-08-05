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
    from contextlib import AbstractAsyncContextManager

    from models.prune import SourceClaim
    from models.state import InstalledRomEntry, ShortcutRegistryEntry
    from models.sync import ClientSaveState

    from domain.disc_selection import Disc
    from domain.rom_install import RomInstall
    from domain.save_layout import SaveLayout
    from domain.shortcut_data import EmulatorInvocation


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

    The composition root satisfies this with ``ActiveCoreResolver``. The
    ``.so``-space read consumers (BIOS status, per-core save dir, save-emulator
    tag, core-change detection, the cores menu) call ``active_core_for_rom`` and
    operate entirely in ``.so`` space — ``(None, None)`` / ``(None, label)`` means
    no libretro core (unconfigured, or a standalone emulator) and they degrade.
    The launch-bake sites call ``active_emulator_for_rom``, which also describes
    **standalone** emulators (PCSX2, RPCS3, …) via a full ES-DE command. Both draw
    from the same resolution, so the read-path core never diverges from the launch.
    """

    def active_core_for_rom(self, rom_id: int) -> tuple[str | None, str | None]: ...

    def active_emulator_for_rom(self, rom_id: int) -> EmulatorInvocation | None: ...


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
    degrades to the default with a WARNING rather than raising; an install the
    system cannot launch (``launchable is False``) resolves to ``""``, which
    every bake site renders as the empty launch command.
    """

    def enumerate_discs(self, install: RomInstall) -> list[Disc]: ...

    def resolve_bake_path(self, install: RomInstall, discs: list[Disc], selected_disc: str | None) -> str: ...

    def resolve_for_install(self, install: RomInstall, selected_disc: str | None) -> str: ...


class RelaunchOptionsReader(Protocol):
    """Installed+bound relaunch-items build consumed by the two relaunch sites.

    The composition root satisfies this with ``RelaunchOptionsResolver``. Both
    the RetroDECK-home migration (re-baking each relocated shortcut to its new
    path) and the startup launch-options reconcile (#1043, healing drift to the
    empty placeholder) ask "what is the current ``launch_options`` for every
    installed and bound ROM?" and forward the returned list to the frontend.
    Each item is a ``{app_id, launch_options}`` dict; uninstalled and unbound
    ROMs are skipped by construction, so an empty list means nothing to relaunch.
    """

    def installed_relaunch_items(self) -> list[dict[str, Any]]: ...


class RomRelaunchItemReader(Protocol):
    """Single installed+bound ROM relaunch-item resolution consumed by VersionSwitchService.

    The composition root satisfies this with ``RelaunchOptionsResolver`` — the
    same seam the batch ``RelaunchOptionsReader`` draws from, narrowed to one
    ROM. After a version switch rebinds a *downloaded* target, the picker needs
    that install's full Steam launch command to write onto the shortcut; this
    resolves it from the freshly-bound rom_id. Returns ``None`` when the ROM has
    no install row or no bound shortcut. A UoW-opening seam — the caller resolves
    it *outside* any open Unit of Work (the nested ``BEGIN IMMEDIATE`` deadlocks).
    """

    def relaunch_item_for_rom(self, rom_id: int) -> dict[str, Any] | None: ...


class RomLaunchPathReader(Protocol):
    """Single installed ROM launch-path resolution consumed by GameProcessService.

    The composition root satisfies this with ``RelaunchOptionsResolver`` — the
    same seam the relaunch items are baked from, narrowed to just the launch
    target inside them. Stop Game matches the live sandbox instances against this
    path to find the one running this ROM, so it MUST come from the bake seam
    rather than be derived some other way: a second derivation would drift from
    the one the shortcut was written with and refuse legitimate stops.

    This is the *same derivation*, not a record of the launch: it re-resolves
    from the current ``rom_installs`` row and disc pick, which a disc switch, a
    version switch, or a reinstall can move between the launch and the stop. A
    stale resolution matches nothing, so the failure direction is a refusal —
    never a wrong instance. Returns ``None`` when the ROM has no install row or
    no bound shortcut: nothing was launched from it. A UoW-opening seam — the
    caller resolves it *outside* any open Unit of Work (the nested
    ``BEGIN IMMEDIATE`` deadlocks).
    """

    def launch_path_for_rom(self, rom_id: int) -> str | None: ...


class SaveDriftProbeFn(Protocol):
    """Local-save drift probe consumed by VersionSwitchService.

    The composition root satisfies this with ``LaunchGateService.check_local_drift``.
    Reports whether the ROM's local save files diverge from their persisted sync
    baseline (a purely-local content-hash read) — the signal that switching away
    from a downloaded version would strand un-uploaded save changes. Returns the
    ``{"drifted": bool, "rom_id": int}`` shape and never raises (LaunchGate
    collapses any internal error to not-drifted).
    """

    async def __call__(self, rom_id: int) -> dict[str, Any]: ...


class ReachabilityProbeFn(Protocol):
    """Server-reachability probe consumed by VersionSwitchService.

    The composition root satisfies this with ``ConnectionService.probe_reachability``.
    Fires a single-attempt, short-timeout heartbeat and returns ``{"online": bool}``
    — the version-switch save-stranding refusal reports it as ``server_reachable``
    so the frontend offers "Sync now & switch" only when a sync could actually
    run. Never raises (an offline verdict is ``online: False``).
    """

    async def __call__(self) -> dict[str, Any]: ...


class InstalledRomRemoverFn(Protocol):
    """Installed-ROM removal consumed by DownloadService (sibling supersede, #1298).

    The composition root satisfies this with ``RomRemovalService.remove_rom``.
    Before downloading a version whose sibling group already has another version
    on disk, DownloadService strips that install through this seam — reusing the
    canonical file-deletion + ``rom_installs`` cleanup rather than duplicating it.
    Returns the removal's ``{success, ...}`` shape; ``reason: "not_installed"`` is
    an already-clean no-op, any other failure aborts the download.
    """

    async def __call__(self, rom_id: int) -> dict[str, Any]: ...


class InstalledRomFilesRemoverFn(Protocol):
    """Filesystem-only installed-ROM removal consumed by explicit prune."""

    def __call__(self, rom_id: int, claims: dict[str, SourceClaim] | None = None) -> dict[str, Any]: ...


class VersionSwitcherFn(Protocol):
    """Version-switch authority consumed by explicit prune repointing."""

    async def __call__(self, app_id: int, target_rom_id: int, allow_stranded: bool) -> dict[str, Any]: ...


class RomRemoverProvider(Protocol):
    """Deferred access to the installed-ROM remover consumed by DownloadService.

    DownloadService and RomRemovalService form a construction cycle
    (RomRemovalService needs DownloadService's queue-cleanup seam), so the
    composition root binds the remover after both exist and hands DownloadService
    this getter (a ``LateBinding``). Each call returns the live
    :class:`InstalledRomRemoverFn`; DownloadService awaits it per removal.
    """

    def __call__(self) -> InstalledRomRemoverFn: ...


class ActiveDownloadRomIdsFn(Protocol):
    """Active-download rom-id snapshot consumed by VersionSwitchService (#1298 F1).

    The composition root satisfies this with ``DownloadService.active_download_rom_ids``.
    A version switch is refused while any member of the target's sibling group has
    an active download — the user is asked to cancel it first. Returns a snapshot
    of the rom ids with an in-flight or queued transfer; a *paused* download is not
    in the set (its task's ``finally`` already released the claim), so a switch is
    allowed while a sibling is paused and a stale resume is refused downstream in
    ``resume_download`` instead. Never raises.
    """

    def __call__(self) -> set[int]: ...


class PruneSaveCoordinator(Protocol):
    """Exact-path save inventory, locking, and quarantine consumed by prune."""

    def lock_prune_roms(self, rom_ids: list[int]) -> AbstractAsyncContextManager[None]: ...
    def inventory_prune_saves(self, purge_rom_ids: list[int]) -> dict[str, Any]: ...
    def quarantine_prune_saves(
        self, files: list[dict[str, str]], claims: dict[str, SourceClaim] | None = None
    ) -> dict[str, Any]: ...
    def validate_prune_absences(self, claims: dict[str, SourceClaim]) -> bool: ...


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
        label: str = "",
        applied_sources: dict[int, str] | None = None,
    ) -> dict[Any, Any]: ...

    async def refresh_changed_covers(
        self,
        all_roms: list[dict[str, Any]],
        registry: dict[str, dict[str, Any]],
        emit_progress: Any,
        is_cancelling: Any,
        progress_step: int = 4,
        progress_total_steps: int = 6,
        label: str = "",
    ) -> list[dict[str, int]]: ...

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


class LaunchGateDriftReader(Protocol):
    """Local save-file enumeration + baseline lookup consumed by LaunchGateService.

    The composition root satisfies this with a thin shim over the same
    ``RomInfoService.find_save_files`` discovery the sync/status path uses
    and the ``rom_save_sync_states`` aggregate's per-file ``last_sync_hash``
    baselines — so the launch-gate drift check sees exactly the files a
    real sync would, never a divergent file-discovery path.

    :meth:`find_local_save_files` returns the on-disk save files for an
    installed ROM (``[{"path", "filename"}]``); an empty list means the ROM
    is not installed or has no save files present.
    :meth:`last_sync_hashes` returns the persisted ``last_sync_hash``
    baseline per filename (``None`` for a file with no baseline yet, and
    missing keys for files never tracked).
    """

    def find_local_save_files(self, rom_id: int) -> list[dict[str, str]]: ...

    def last_sync_hashes(self, rom_id: int) -> dict[str, str | None]: ...


class SessionPlaytimeRecorder(Protocol):
    """Playtime end-of-session record consumed by SessionLifecycleService.

    The composition root satisfies this with ``PlaytimeService``'s
    ``record_session_end``. The lifecycle service forwards the
    ``total_seconds`` field to the frontend so the playtime display can
    be updated; a falsy ``success`` value yields ``total_seconds=None``
    on the returned DTO so the frontend leaves the display untouched.
    Device-suspend time is excluded by the recorder itself via the
    monotonic clock (#1148); the caller passes no suspend duration.
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


class SaveInventoryBuilderFn(Protocol):
    """Scoped negotiate-inventory build consumed by SyncEngine.

    The composition root satisfies this with ``SaveService.build_save_inventory``.
    SyncEngine calls it to gather this device's local-save inventory for the
    negotiate POST: ``rom_id=None`` builds the whole-device inventory (the bulk
    ``sync_all_saves`` pre-negotiate), a concrete ``rom_id`` scopes it to that
    one ROM (the single-ROM negotiate trigger). Only confirmed, non-legacy-slot
    ROMs with local save files contribute entries — the wizard gate stays
    upstream of negotiate (ADR-0016).
    """

    def __call__(self, rom_id: int | None = None) -> list[ClientSaveState]: ...


class DeviceIdProvider(Protocol):
    """Server device-id read consumed by PlaytimeService.

    The composition root satisfies this with ``SaveService.get_device_id``
    (which delegates to the ``DeviceRegistry``, the single owner of
    ``kv_config["device_id"]``). PlaytimeService reads the id to attribute
    native play-session ingests and to gate the offline outbox: a ``None``
    return means this device is not registered yet, so the session is folded
    locally and never enqueued (never wire an empty device id).
    """

    def get_device_id(self) -> str | None: ...


class DeviceForgetFn(Protocol):
    """Server-device-id reset consumed by ConnectionService.

    The composition root satisfies this with ``SaveService.forget_device``.
    ConnectionService invokes it on a successful sign-in whose origin differs
    from the previous token's origin: the registered device id is bound to the
    origin it was minted against, so a server switch must drop it (negotiate
    hard-404s a foreign device id) and let the next sync re-register.
    """

    def __call__(self) -> None: ...


class PlaytimeScopeNoticeClearFn(Protocol):
    """Playtime read-scope re-sign-in notice reset consumed by ConnectionService.

    The composition root satisfies this with ``PlaytimeService.clear_scope_notice``.
    ConnectionService invokes it on a successful sign-in: the freshly minted token
    carries the ``roms.user.read`` scope (#1280), so any durable "sign in again to
    enable cross-device playtime" notice a prior 403 raised is now stale and must
    be cleared. Best-effort + local-only — a clear failure never fails the sign-in.
    """

    def __call__(self) -> None: ...
