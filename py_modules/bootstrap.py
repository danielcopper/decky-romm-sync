"""Composition root — instantiates adapters and wires services.

Adapter construction lives here so ``main.py`` only deals with the
Decky lifecycle and the callable surface. ``bootstrap()`` also loads
and migrates settings as part of adapter wiring so adapters that bind
a live mutable settings dict (such as ``RommHttpAdapter``) bind the
migrated dict in a single pass; that same dict is returned for the
caller to keep as its source of truth.
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
from adapters.steamgriddb import SteamGridDbAdapter
from adapters.system_clock import SystemClock
from adapters.system_uuid_gen import SystemUuidGen
from domain.state_migrations import fold_legacy_save_sync_settings, migrate_settings
from lib.late_binding import LateBinding
from services.achievements import AchievementsService, AchievementsServiceConfig
from services.active_core_resolver import ActiveCoreResolver, ActiveCoreResolverConfig
from services.artwork import ArtworkService, ArtworkServiceConfig
from services.connection import ConnectionService, ConnectionServiceConfig
from services.cores import CoreService, CoreServiceConfig
from services.disc import DiscService, DiscServiceConfig
from services.disc_launch_resolver import DiscLaunchResolver, DiscLaunchResolverConfig
from services.downloads import DownloadService, DownloadServiceConfig
from services.firmware import FirmwareService, FirmwareServiceConfig
from services.game_detail import GameDetailService, GameDetailServiceConfig
from services.launch_gate import LaunchGateService, LaunchGateServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService, MetadataServiceConfig
from services.migration import MigrationService, MigrationServiceConfig
from services.playtime import PlaytimeService, PlaytimeServiceConfig
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig
from services.saves import SaveService, SaveServiceConfig
from services.session_lifecycle import SessionLifecycleService, SessionLifecycleServiceConfig
from services.settings import SettingsService, SettingsServiceConfig
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig
from services.startup_healing import StartupHealingService, StartupHealingServiceConfig
from services.steamgrid import SteamGridService, SteamGridServiceConfig

if TYPE_CHECKING:
    import asyncio
    import logging
    from typing import Any

    from services.protocols import (
        Clock,
        CoreInfoProvider,
        CoreNameProviderFn,
        CoverArtFileStore,
        DebugLogger,
        DirectoryFileListerFn,
        DownloadFileStore,
        EventEmitter,
        FirmwareFileStore,
        HostnameReader,
        MachineIdReader,
        MigrationFileStore,
        PathExistsReader,
        PlatformCoreReader,
        PluginMetadataReader,
        RetroArchSaveLayoutProvider,
        RetroDeckPaths,
        RomFileStore,
        RommApi,
        SaveFileStore,
        SettingsPersister,
        SgdbArtworkCache,
        Sleeper,
        SteamConfigStore,
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


@dataclass(frozen=True)
class WiringConfig:
    """Composition-root inputs for ``wire_services``.

    Four bundles carry the wiring; ``min_required_version`` sits at the
    top level — it's plugin metadata, not a runtime seam, and only
    ConnectionService consumes it.
    """

    adapters: AdapterBundle
    stores: StateBundle
    runtime: RuntimeBundle
    callbacks: CallbackBundle
    min_required_version: tuple[int, ...]


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
    user_agent = f"decky-romm-sync/{plugin_metadata.read_version(plugin_dir)}"
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
    save_file_store = SaveFileAdapter()
    path_probe = PathProbeAdapter()
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


def wire_services(cfg: WiringConfig) -> dict[str, Any]:
    """Create service instances after plugin state is initialised.

    Called from ``Plugin._main()`` after save-sync state is populated
    so that services receive live references to the fully-populated
    state dicts.

    Returns
    -------
    dict with keys ``save_sync_service``, ``playtime_service``,
    ``sync_service``, ``download_service``, and ``firmware_service``.
    """
    # Forward-reference bindings for producers constructed later in this
    # function. Consumers receive ``binding.get`` (a bound method); the
    # binding is populated via ``.set(...)`` once the producer exists.
    # Accessing ``.get()`` before ``.set()`` raises RuntimeError instead of
    # the NameError a bare forward-ref lambda would produce.
    bios_files_index_binding: LateBinding[dict[str, dict[str, Any]]] = LateBinding("bios_files_index")
    pending_sync_binding: LateBinding[dict[int, dict[str, Any]]] = LateBinding("pending_sync")

    # The single read-path core resolver (B1): folds the per-game
    # emulator_override pin over the system-layer ES-DE resolution. Built first
    # (no service deps) so every per-game-core read consumer — migration, saves,
    # game-detail, cores — draws from the SAME seam and the read-path core never
    # diverges from the launched core.
    active_core_resolver = ActiveCoreResolver(
        config=ActiveCoreResolverConfig(
            uow_factory=cfg.callbacks.uow_factory,
            core_info=cfg.adapters.core_info_provider,
            platform_core_reader=cfg.callbacks.platform_core_reader,
            resolve_system=cfg.adapters.http_adapter.resolve_system,
            logger=cfg.runtime.logger,
        ),
    )

    # The single read-path disc resolver (#865): folds the per-game
    # selected_disc pick over the live disc-image enumeration of an installed
    # ROM's directory. Built alongside active_core_resolver (no service deps) so
    # every launch-bake site and the picker callables draw the bake path from the
    # SAME seam and the baked launch_options never diverge from the selection.
    disc_launch_resolver = DiscLaunchResolver(
        config=DiscLaunchResolverConfig(
            list_files=cfg.callbacks.list_rom_dir_files,
            system_extensions=cfg.callbacks.system_extensions,
            logger=cfg.runtime.logger,
        ),
    )

    # MigrationService is constructed before SaveService so that
    # save_sync_service can receive a bound reference to
    # ``migration_service.detect_save_sort_change``. SaveService must observe
    # fresh sort state before computing saves_dir (#238).
    migration_service = MigrationService(
        config=MigrationServiceConfig(
            migration_file_store=cfg.adapters.migration_file_store,
            settings=cfg.stores.settings,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            settings_persister=cfg.callbacks.settings_persister,
            emit=cfg.runtime.emit,
            get_bios_files_index=bios_files_index_binding.get,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            get_save_layout=cfg.callbacks.get_save_layout,
            active_core=active_core_resolver,
            disc_resolver=disc_launch_resolver,
            get_core_name=cfg.callbacks.get_core_name,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    save_service_config = SaveServiceConfig(
        romm_api=cfg.adapters.romm_api,
        retry=cfg.adapters.http_adapter,
        settings=cfg.stores.settings,
        settings_persister=cfg.callbacks.settings_persister,
        save_file_store=cfg.adapters.save_file_store,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        retrodeck_paths=cfg.callbacks.retrodeck_paths,
        active_core=active_core_resolver,
        hostname_provider=cfg.runtime.hostname_provider,
        machine_id_provider=cfg.runtime.machine_id_provider,
        log_debug=cfg.callbacks.log_debug,
        get_core_name=cfg.callbacks.get_core_name,
        plugin_metadata=cfg.callbacks.plugin_metadata,
        plugin_dir=cfg.runtime.plugin_dir,
        emit=cfg.runtime.emit,
        # StatusService reports the live layout so the SAVES tab can warn when
        # saves go to the content dir (#239).
        get_save_layout=cfg.callbacks.get_save_layout,
        # SaveService must observe fresh sort state before computing saves_dir (#238).
        detect_sort_change=migration_service.detect_save_sort_change,
        is_retrodeck_migration_pending=migration_service.is_retrodeck_migration_pending,
        uow_factory=cfg.callbacks.uow_factory,
    )
    save_sync_service = SaveService(config=save_service_config)

    playtime_service = PlaytimeService(
        config=PlaytimeServiceConfig(
            romm_api=cfg.adapters.romm_api,
            retry=cfg.adapters.http_adapter,
            settings=cfg.stores.settings,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            log_debug=cfg.callbacks.log_debug,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    metadata_service = MetadataService(
        config=MetadataServiceConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            log_debug=cfg.callbacks.log_debug,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    artwork_service = ArtworkService(
        config=ArtworkServiceConfig(
            romm_api=cfg.adapters.romm_api,
            steam_config=cfg.adapters.steam_config,
            cover_art_file_store=cfg.adapters.cover_art_file_store,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            get_pending_sync=pending_sync_binding.get,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    shortcut_removal_service = ShortcutRemovalService(
        config=ShortcutRemovalServiceConfig(
            steam_config=cfg.adapters.steam_config,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            artwork_remover=artwork_service,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=cfg.adapters.romm_api,
            steam_config=cfg.adapters.steam_config,
            settings=cfg.stores.settings,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            plugin_dir=cfg.runtime.plugin_dir,
            emit=cfg.runtime.emit,
            clock=cfg.runtime.clock,
            uuid_gen=cfg.runtime.uuid_gen,
            sleeper=cfg.runtime.sleeper,
            settings_persister=cfg.callbacks.settings_persister,
            log_debug=cfg.callbacks.log_debug,
            artwork=artwork_service,
            uow_factory=cfg.callbacks.uow_factory,
            active_core=active_core_resolver,
            disc_resolver=disc_launch_resolver,
        ),
    )
    pending_sync_binding.set(lambda: sync_service.pending_sync)

    download_service = DownloadService(
        config=DownloadServiceConfig(
            romm_api=cfg.adapters.romm_api,
            download_file_store=cfg.adapters.download_file_store,
            resolve_system=cfg.adapters.http_adapter.resolve_system,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            emit=cfg.runtime.emit,
            clock=cfg.runtime.clock,
            sleeper=cfg.runtime.sleeper,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            active_core=active_core_resolver,
            disc_resolver=disc_launch_resolver,
            m3u_support=cfg.callbacks.m3u_support,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    rom_removal_service = RomRemovalService(
        config=RomRemovalServiceConfig(
            logger=cfg.runtime.logger,
            loop=cfg.runtime.loop,
            rom_file_store=cfg.adapters.rom_file_store,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            download_queue_cleanup=download_service,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    firmware_service = FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=cfg.adapters.romm_api,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            plugin_dir=cfg.runtime.plugin_dir,
            clock=cfg.runtime.clock,
            firmware_file_store=cfg.adapters.firmware_file_store,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            core_info=cfg.adapters.core_info_provider,
            resolve_system=cfg.adapters.http_adapter.resolve_system,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )
    # Load the BIOS registry from disk now so the property does not raise
    # the pre-load RuntimeError when the binding's reader is later invoked.
    firmware_service.load_bios_registry()
    bios_files_index_binding.set(lambda: firmware_service.bios_files_index)

    sgdb_service = SteamGridService(
        config=SteamGridServiceConfig(
            sgdb_api=cfg.adapters.sgdb_adapter,
            romm_api=cfg.adapters.romm_api,
            steam_config=cfg.adapters.steam_config,
            sgdb_artwork_cache=cfg.adapters.sgdb_artwork_cache,
            settings=cfg.stores.settings,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            settings_persister=cfg.callbacks.settings_persister,
            get_pending_sync=pending_sync_binding.get,
            log_debug=cfg.callbacks.log_debug,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    achievements_service = AchievementsService(
        config=AchievementsServiceConfig(
            romm_api=cfg.adapters.romm_api,
            uow_factory=cfg.callbacks.uow_factory,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            log_debug=cfg.callbacks.log_debug,
        ),
    )

    game_detail_service = GameDetailService(
        config=GameDetailServiceConfig(
            settings=cfg.stores.settings,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            uow_factory=cfg.callbacks.uow_factory,
            bios_checker=firmware_service,
            achievements=achievements_service,
            active_core=active_core_resolver,
        ),
    )

    settings_service = SettingsService(
        config=SettingsServiceConfig(
            settings=cfg.stores.settings,
            uow_factory=cfg.callbacks.uow_factory,
            logger=cfg.runtime.logger,
            settings_persister=cfg.callbacks.settings_persister,
            steam_config=cfg.adapters.steam_config,
        ),
    )

    core_service = CoreService(
        config=CoreServiceConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            core_info=cfg.adapters.core_info_provider,
            resolve_system=cfg.adapters.http_adapter.resolve_system,
            settings=cfg.stores.settings,
            settings_persister=cfg.callbacks.settings_persister,
            bios_checker=firmware_service,
            uow_factory=cfg.callbacks.uow_factory,
            active_core=active_core_resolver,
            disc_resolver=disc_launch_resolver,
        ),
    )

    disc_service = DiscService(
        config=DiscServiceConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            uow_factory=cfg.callbacks.uow_factory,
            disc_resolver=disc_launch_resolver,
            active_core=active_core_resolver,
        ),
    )

    connection_service = ConnectionService(
        config=ConnectionServiceConfig(
            settings=cfg.stores.settings,
            romm_api=cfg.adapters.romm_api,
            settings_persister=cfg.callbacks.settings_persister,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            min_required_version=cfg.min_required_version,
        ),
    )

    startup_healing_service = StartupHealingService(
        config=StartupHealingServiceConfig(
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            path_probe=cfg.adapters.path_probe,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    launch_gate_service = LaunchGateService(
        config=LaunchGateServiceConfig(
            rom_lookup=sync_service,
            installed_checker=download_service,
            save_status_reader=save_sync_service,
            logger=cfg.runtime.logger,
        ),
    )

    session_lifecycle_service = SessionLifecycleService(
        config=SessionLifecycleServiceConfig(
            playtime_recorder=playtime_service,
            post_exit_sync=save_sync_service,
            achievement_sync=achievements_service,
            migration_reader=migration_service,
            logger=cfg.runtime.logger,
        ),
    )

    return {
        "save_sync_service": save_sync_service,
        "playtime_service": playtime_service,
        "sync_service": sync_service,
        "download_service": download_service,
        "rom_removal_service": rom_removal_service,
        "firmware_service": firmware_service,
        "sgdb_service": sgdb_service,
        "metadata_service": metadata_service,
        "achievements_service": achievements_service,
        "migration_service": migration_service,
        "game_detail_service": game_detail_service,
        "artwork_service": artwork_service,
        "shortcut_removal_service": shortcut_removal_service,
        "settings_service": settings_service,
        "core_service": core_service,
        "disc_service": disc_service,
        "connection_service": connection_service,
        "startup_healing_service": startup_healing_service,
        "launch_gate_service": launch_gate_service,
        "session_lifecycle_service": session_lifecycle_service,
    }
