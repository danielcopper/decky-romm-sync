"""Adapter half of the composition root — the only place adapters are constructed.

Adapter construction lives here so ``main.py`` only deals with the
Decky lifecycle and the callable surface. ``bootstrap()`` also loads
and migrates settings as part of adapter wiring so adapters that bind
a live mutable settings dict (such as ``RommHttpAdapter``) bind the
migrated dict in a single pass; that same dict is returned for the
caller to keep as its source of truth.

The bundles defined here are the typed vocabulary the service half
consumes; nothing outside this module instantiates an adapter.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from adapters.asyncio_sleeper import AsyncioSleeper
from adapters.cover_art_file_store import CoverArtFileStoreAdapter
from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.download_file import DownloadFileAdapter
from adapters.es_de_config import CoreResolver
from adapters.firmware_file import FirmwareFileAdapter
from adapters.game_process import GameProcessAdapter
from adapters.gavel_native import GavelNativeAdapter
from adapters.hostname import HostnameAdapter
from adapters.machine_id import MachineIdAdapter
from adapters.migration_file import MigrationFileAdapter
from adapters.path_probe import PathProbeAdapter
from adapters.persistence import (
    PersistenceAdapter,
    PlatformCoreReaderAdapter,
    SettingsPersisterAdapter,
)
from adapters.plugin_metadata import PluginMetadataAdapter
from adapters.prune_artifacts import PruneArtifactAdapter
from adapters.recovery_bundle import RecoveryBundleAdapter
from adapters.renderer_gc import RendererGcAdapter
from adapters.renderer_rss import RendererRssAdapter
from adapters.repositories.unit_of_work import SqliteUnitOfWork
from adapters.retroarch_config import RetroArchConfigAdapter
from adapters.retroarch_core_info import RetroArchCoreInfoAdapter
from adapters.retrodeck_paths import RetroDeckPathsAdapter
from adapters.rom_files import RomFileAdapter
from adapters.romm.http import RommHttpAdapter
from adapters.romm.romm_api import RommApiAdapter
from adapters.save_file import SaveFileAdapter
from adapters.sgdb_artwork_cache import SgdbArtworkCacheAdapter
from adapters.sqlite_migrations import MIGRATIONS_DIR, apply_migrations
from adapters.steam_config import SteamConfigAdapter
from adapters.steam_recovery import SteamRecoveryAdapter
from adapters.steamgriddb import SteamGridDbAdapter
from adapters.system_clock import SystemClock
from adapters.system_uuid_gen import SystemUuidGen
from domain.state_migrations import fold_legacy_save_sync_settings, migrate_settings

if TYPE_CHECKING:
    import asyncio
    import logging
    from typing import Any

    from services.protocols import (
        Clock,
        ComputeSyncActionFn,
        CoreInfoProvider,
        CoreNameProviderFn,
        CoverArtFileStore,
        DebugLogger,
        DirectoryFileListerFn,
        DownloadFileStore,
        EventEmitter,
        FirmwareFileStore,
        GameProcessControl,
        HostnameReader,
        MachineIdReader,
        MigrationFileStore,
        PathExistsReader,
        PlatformCoreReader,
        PluginMetadataReader,
        PruneArtifactStore,
        RecoveryBundleStore,
        RendererGcFn,
        RendererRssFn,
        ResolveUploadConflictFn,
        RetroArchSaveLayoutProvider,
        RetroDeckPaths,
        RomFileStore,
        RommApi,
        SaveFileStore,
        SettingsPersister,
        SgdbArtworkCache,
        Sleeper,
        SteamConfigStore,
        SteamRecoveryStore,
        SystemM3uSupportFn,
        SystemSupportedExtensionsFn,
        UnitOfWorkFactory,
        UuidGen,
    )

# Filename of the SQLite database inside the plugin runtime dir. Created by the
# migration runner at startup; unused until the service cutover (#784).
_DB_FILENAME = "romm_sync.db"


@dataclass(frozen=True)
class AdapterBundle:
    """Concrete I/O adapters wired into services."""

    http_adapter: RommHttpAdapter
    romm_api: RommApi
    steam_config: SteamConfigStore
    sgdb_adapter: SteamGridDbAdapter
    cover_art_file_store: CoverArtFileStore
    sgdb_artwork_cache: SgdbArtworkCache
    download_file_store: DownloadFileStore
    firmware_file_store: FirmwareFileStore
    migration_file_store: MigrationFileStore
    rom_file_store: RomFileStore
    save_file_store: SaveFileStore
    path_probe: PathExistsReader
    core_info_provider: CoreInfoProvider
    renderer_rss: RendererRssFn
    renderer_gc: RendererGcFn
    game_process: GameProcessControl
    resolve_upload_conflict: ResolveUploadConflictFn
    compute_sync_action: ComputeSyncActionFn
    recovery_store: RecoveryBundleStore
    prune_artifacts: PruneArtifactStore
    steam_recovery: SteamRecoveryStore


@dataclass(frozen=True)
class StateBundle:
    """Live mutable state shared across services."""

    settings: dict[str, Any]


@dataclass(frozen=True)
class RuntimeBundle:
    """Process-level runtime infrastructure (event loop, logger, paths, time/UUID/sleep seams)."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    runtime_dir: str
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    hostname_provider: HostnameReader
    machine_id_provider: MachineIdReader


@dataclass(frozen=True)
class CallbackBundle:
    """Provider callables and persister Protocols injected into services."""

    retrodeck_paths: RetroDeckPaths
    get_save_layout: RetroArchSaveLayoutProvider
    get_core_name: CoreNameProviderFn
    platform_core_reader: PlatformCoreReader
    m3u_support: SystemM3uSupportFn
    system_extensions: SystemSupportedExtensionsFn
    list_rom_dir_files: DirectoryFileListerFn
    settings_persister: SettingsPersister
    log_debug: DebugLogger
    plugin_metadata: PluginMetadataReader
    uow_factory: UnitOfWorkFactory


@dataclass(frozen=True)
class RuntimeAdaptersBundle:
    """Concrete adapters for the Clock/UuidGen/Sleeper/HostnameReader/MachineIdReader seams.

    Bootstrap owns adapter instantiation, but the ``RuntimeBundle``
    handed to ``wire_services`` also needs runtime-only state ``main.py``
    introduces (the ``asyncio`` loop, ``decky.emit``). This sub-bundle
    carries the seams bootstrap builds so ``main.py`` can compose the
    final ``RuntimeBundle`` without instantiating any adapters itself.
    """

    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    hostname_provider: HostnameReader
    machine_id_provider: MachineIdReader


@dataclass(frozen=True)
class BootstrapHandles:
    """Bootstrap outputs ``main.py`` needs that don't fit the wiring bundles.

    Anything ``Plugin`` itself binds (not the services) lives here:
    the debug logger forwarded by ``Plugin._log_debug`` and the
    persistence adapter ``Plugin`` holds for disk-touching callable paths
    that bypass a service. The bundles already cover everything passed to
    ``wire_services``; this struct keeps those Plugin-only handles typed
    instead of returning them via the untyped dict shape of yore.
    """

    debug_logger: DebugLogger
    persistence: PersistenceAdapter


@dataclass(frozen=True)
class BootstrapResult:
    """Typed return shape for :func:`bootstrap`.

    The four bundles carry every Protocol-typed seam and live state
    dict that services need; :attr:`handles` carries the small set of
    raw outputs only ``main.py`` itself binds (debug logger). Together
    they replace the historical untyped ``dict`` return so every
    consumer is caught by basedpyright instead of failing silently at
    runtime on a typo.
    """

    adapters: AdapterBundle
    stores: StateBundle
    callbacks: CallbackBundle
    runtime_adapters: RuntimeAdaptersBundle
    handles: BootstrapHandles


def bootstrap(
    *,
    settings_dir: str,
    runtime_dir: str,
    plugin_dir: str,
    user_home: str,
    logger: logging.Logger,
) -> BootstrapResult:
    """Build every adapter and bundle the composition root hands to ``main.py``.

    Bootstrap owns adapter instantiation and is the only path that
    constructs ``PersistenceAdapter``. Settings are loaded + migrated
    inside here so the ``SettingsPersisterAdapter`` binds the live dict
    at construction; mutating that dict from the caller side is visible
    to every adapter/service that holds the same reference.

    Parameters
    ----------
    settings_dir:
        ``decky.DECKY_PLUGIN_SETTINGS_DIR``
    runtime_dir:
        ``decky.DECKY_PLUGIN_RUNTIME_DIR``
    plugin_dir:
        ``decky.DECKY_PLUGIN_DIR``
    user_home:
        ``decky.DECKY_USER_HOME`` — base for RetroDECK and Steam path lookups.
    logger:
        ``decky.logger``

    Returns
    -------
    :class:`BootstrapResult`
        Typed bundles consumed by ``wire_services`` (``adapters``,
        ``stores``, ``callbacks``) plus the small set of Plugin-only
        handles ``main.py`` itself binds (``handles.debug_logger``).
    """
    # Bring the on-disk SQLite schema up to date before any service is wired —
    # the composition root owns startup infra. Post-cutover (#784) SQLite is the
    # sole persistence backend: there is no JSON fallback, so a failed or
    # unopenable database is fatal. Log the cause, then re-raise so bootstrap
    # aborts and the plugin stays inert — matching the RomM-minimum-version
    # gate's "inert until the environment is fixed" posture.
    db_path = os.path.join(runtime_dir, _DB_FILENAME)
    try:
        apply_migrations(db_path, MIGRATIONS_DIR, logger=logger)
    except Exception:
        logger.exception("SQLite schema migration failed; plugin cannot start")
        raise

    # The runtime Unit-of-Work factory: each call opens a fresh sync sqlite3
    # connection on db_path (ADR-0004). Wired here but not yet threaded into any
    # service config — the service cutover (#784) consumes it.
    uow_factory: UnitOfWorkFactory = functools.partial(SqliteUnitOfWork, db_path)

    retrodeck_paths = RetroDeckPathsAdapter(user_home=user_home, logger=logger)
    retroarch_config = RetroArchConfigAdapter(user_home=user_home, logger=logger)
    retroarch_core_info = RetroArchCoreInfoAdapter(user_home=user_home, logger=logger)
    core_resolver = CoreResolver(
        plugin_dir=plugin_dir,
        logger=logger,
        user_home=user_home,
    )

    # SystemClock is dependency-free; construct it here so the single shared
    # instance threads into PersistenceAdapter (corrupt-settings backup stamp)
    # and every later seam (uuid_gen/sleeper neighbours, runtime bundle).
    clock = SystemClock()
    persistence = PersistenceAdapter(settings_dir, runtime_dir, logger, clock=clock)
    settings = persistence.load_settings()
    # One-time JSON→JSON lift (ADR-0003): fold the legacy save-sync knobs +
    # device_name out of save_sync_state.json before the schema bump stamps
    # version 4. Idempotent — after the first run save_settings stamps the
    # new version and this branch is skipped.
    if settings.get("version", 0) < 4:
        settings = fold_legacy_save_sync_settings(settings, persistence.load_save_sync_state())
    settings = migrate_settings(settings)
    # If load_settings quarantined a corrupt file this boot, fold the reset into
    # the settings dict as a persistent marker. Set AFTER migration and BEFORE
    # the save so it lands in the fresh settings.json and survives a plugin
    # reload — the frontend surfaces it as a banner (QAM + game detail) until the
    # next successful sign-in clears it (ConnectionService pops it on persist).
    if persistence.corrupt_reset is not None:
        settings["_settings_reset_notice"] = {"backed_up_to": persistence.corrupt_reset["backed_up_to"]}
    persistence.save_settings(settings)
    settings_persister = SettingsPersisterAdapter(persistence, settings)
    # Binds the same live settings dict so the per-platform-core fan-out resolves
    # the freshly-written value, not a snapshot.
    platform_core_reader = PlatformCoreReaderAdapter(settings)
    plugin_metadata = PluginMetadataAdapter()
    # Single source of truth for outgoing User-Agent — read package.json
    # version once at boot and thread the string to every HTTP-talking
    # adapter. Bot Fight Mode on Cloudflare blocks the default
    # ``Python-urllib`` UA before requests reach self-hosted RomM (#249).
    package_name, plugin_version = plugin_metadata.read_metadata(plugin_dir)
    user_agent = f"decky-romm-sync/{plugin_version}"
    recovery_store = RecoveryBundleAdapter(
        user_home=user_home,
        package_name=package_name,
        plugin_version=plugin_version,
    )
    prune_artifacts = PruneArtifactAdapter(runtime_dir=runtime_dir)
    steam_recovery = SteamRecoveryAdapter(user_home=user_home, logger=logger)
    http_adapter = RommHttpAdapter(settings, plugin_dir, logger, user_agent)
    romm_api = RommApiAdapter(http_adapter)
    steam_config = SteamConfigAdapter(user_home=user_home, logger=logger)
    sgdb_adapter = SteamGridDbAdapter(settings=settings, logger=logger, user_agent=user_agent)
    cover_art_file_store = CoverArtFileStoreAdapter()
    sgdb_artwork_cache = SgdbArtworkCacheAdapter(runtime_dir=runtime_dir)
    download_file_store = DownloadFileAdapter()
    firmware_file_store = FirmwareFileAdapter()
    migration_file_store = MigrationFileAdapter()
    rom_file_store = RomFileAdapter()
    save_file_store = SaveFileAdapter(logger=logger)
    path_probe = PathProbeAdapter()
    renderer_rss = RendererRssAdapter()
    renderer_gc = RendererGcAdapter(logger=logger)
    game_process = GameProcessAdapter()
    # The compiled gavel core owns both save-sync decisions — the per-file sync
    # action and the upload-409 resolution. Loaded eagerly so a missing /
    # wrong-architecture artifact is fatal here (like the SQLite migration gate
    # above) rather than surfacing mid-sync — there is no Python fallback
    # (GavelNativeLoadError propagates, plugin stays inert).
    gavel = GavelNativeAdapter()
    uuid_gen = SystemUuidGen()
    sleeper = AsyncioSleeper()
    hostname_provider = HostnameAdapter()
    machine_id_provider = MachineIdAdapter()
    debug_logger = SettingsAwareDebugLogger(settings=settings, logger=logger)

    adapters = AdapterBundle(
        http_adapter=http_adapter,
        romm_api=romm_api,
        steam_config=steam_config,
        sgdb_adapter=sgdb_adapter,
        cover_art_file_store=cover_art_file_store,
        sgdb_artwork_cache=sgdb_artwork_cache,
        download_file_store=download_file_store,
        firmware_file_store=firmware_file_store,
        migration_file_store=migration_file_store,
        rom_file_store=rom_file_store,
        save_file_store=save_file_store,
        path_probe=path_probe,
        core_info_provider=core_resolver,
        renderer_rss=renderer_rss,
        renderer_gc=renderer_gc,
        game_process=game_process,
        resolve_upload_conflict=gavel,
        compute_sync_action=gavel.compute_sync_action,
        recovery_store=recovery_store,
        prune_artifacts=prune_artifacts,
        steam_recovery=steam_recovery,
    )
    stores = StateBundle(
        settings=settings,
    )
    callbacks = CallbackBundle(
        retrodeck_paths=retrodeck_paths,
        get_save_layout=retroarch_config.get_save_layout,
        get_core_name=retroarch_core_info.get_corename,
        platform_core_reader=platform_core_reader,
        m3u_support=core_resolver.system_supports_m3u,
        system_extensions=core_resolver.get_supported_extensions,
        list_rom_dir_files=download_file_store.list_files,
        settings_persister=settings_persister,
        log_debug=debug_logger,
        plugin_metadata=plugin_metadata,
        uow_factory=uow_factory,
    )
    runtime_adapters = RuntimeAdaptersBundle(
        clock=clock,
        uuid_gen=uuid_gen,
        sleeper=sleeper,
        hostname_provider=hostname_provider,
        machine_id_provider=machine_id_provider,
    )
    handles = BootstrapHandles(debug_logger=debug_logger, persistence=persistence)

    return BootstrapResult(
        adapters=adapters,
        stores=stores,
        callbacks=callbacks,
        runtime_adapters=runtime_adapters,
        handles=handles,
    )
