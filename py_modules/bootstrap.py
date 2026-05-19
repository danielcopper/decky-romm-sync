"""Composition root — instantiates adapters and wires services.

Adapter construction lives here so ``main.py`` only deals with the
Decky lifecycle and the callable surface. ``bootstrap()`` also loads
and migrates settings as part of adapter wiring so adapters that bind
a live mutable settings dict (such as ``RommHttpAdapter``) bind the
migrated dict in a single pass; that same dict is returned for the
caller to keep as its source of truth.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import cast

from models.state import MetadataCache, PluginState, make_default_plugin_state

from adapters.asyncio_sleeper import AsyncioSleeper
from adapters.cover_art_file_store import CoverArtFileStoreAdapter
from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.download_file import DownloadFileAdapter
from adapters.download_queue import DownloadQueueAdapter
from adapters.es_de_config import CoreResolver, GamelistXmlEditorAdapter
from adapters.firmware_file import FirmwareFileAdapter
from adapters.hostname import HostnameAdapter
from adapters.metadata_cache_store import MetadataCacheStoreAdapter
from adapters.migration_file import MigrationFileAdapter
from adapters.path_probe import PathProbeAdapter
from adapters.persistence import (
    FirmwareCachePersisterAdapter,
    MetadataCachePersisterAdapter,
    PersistenceAdapter,
    SaveSyncStatePersisterAdapter,
    SettingsPersisterAdapter,
    StatePersisterAdapter,
)
from adapters.plugin_metadata import PluginMetadataAdapter
from adapters.registry_store import RegistryStoreAdapter
from adapters.retroarch_config import RetroArchConfigAdapter
from adapters.retroarch_core_info import RetroArchCoreInfoAdapter
from adapters.retrodeck_paths import RetroDeckPathsAdapter
from adapters.rom_files import RomFileAdapter
from adapters.romm.http import RommHttpAdapter
from adapters.romm.romm_api import RommApiAdapter
from adapters.save_file import SaveFileAdapter
from adapters.sgdb_artwork_cache import SgdbArtworkCacheAdapter
from adapters.steam_config import SteamConfigAdapter
from adapters.steamgriddb import SteamGridDbAdapter
from adapters.system_clock import SystemClock
from adapters.system_uuid_gen import SystemUuidGen
from domain.save_state import SaveSyncState
from domain.state_migrations import migrate_settings, migrate_state
from lib.late_binding import LateBinding
from services.achievements import AchievementsService, AchievementsServiceConfig
from services.artwork import ArtworkService, ArtworkServiceConfig
from services.connection import ConnectionService, ConnectionServiceConfig
from services.cores import CoreService, CoreServiceConfig
from services.downloads import DownloadService, DownloadServiceConfig
from services.firmware import FirmwareService, FirmwareServiceConfig
from services.game_detail import GameDetailService, GameDetailServiceConfig
from services.launch_gate import LaunchGateService, LaunchGateServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService, MetadataServiceConfig
from services.migration import MigrationService, MigrationServiceConfig
from services.playtime import PlaytimeService, PlaytimeServiceConfig
from services.protocols import (
    Clock,
    CoreInfoProvider,
    CoreNameProviderFn,
    CoverArtFileStore,
    DebugLogger,
    DownloadFileStore,
    DownloadQueueStore,
    EventEmitter,
    FirmwareCachePersister,
    FirmwareFileStore,
    GamelistXmlEditor,
    HostnameReader,
    MetadataCachePersister,
    MetadataCacheStore,
    MigrationFileStore,
    PathExistsReader,
    PluginMetadataReader,
    RetroArchSaveSortingProvider,
    RetroDeckPaths,
    RomFileStore,
    RommApi,
    SaveFileStore,
    SaveSyncStatePersister,
    SettingsPersister,
    SgdbArtworkCache,
    ShortcutRegistryStore,
    Sleeper,
    StatePersister,
    SteamConfigStore,
    UuidGen,
)
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig
from services.saves import SaveService, SaveServiceConfig
from services.session_lifecycle import SessionLifecycleService, SessionLifecycleServiceConfig
from services.settings import SettingsService, SettingsServiceConfig
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig
from services.startup_healing import StartupHealingService, StartupHealingServiceConfig
from services.steamgrid import SteamGridService, SteamGridServiceConfig


def _default_state() -> PluginState:
    """Fresh default ``state`` dict for first-run persistence.

    Delegates to :func:`models.state.make_default_plugin_state` so the
    canonical shape lives next to the :class:`PluginState` schema. Wrapped
    here (rather than re-exported) to preserve the module-level factory
    indirection — callers always receive independent inner containers
    even if the underlying default ever changes.
    """
    return make_default_plugin_state()


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
    download_queue: DownloadQueueStore
    firmware_file_store: FirmwareFileStore
    migration_file_store: MigrationFileStore
    rom_file_store: RomFileStore
    save_file_store: SaveFileStore
    gamelist_editor: GamelistXmlEditor
    path_probe: PathExistsReader
    core_info_provider: CoreInfoProvider


@dataclass(frozen=True)
class StateBundle:
    """Live mutable state stores shared across services."""

    state: PluginState
    settings: dict
    metadata_cache: MetadataCache
    save_sync_state: SaveSyncState


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


@dataclass(frozen=True)
class CallbackBundle:
    """Provider callables and persister Protocols injected into services."""

    retrodeck_paths: RetroDeckPaths
    get_retroarch_save_sorting: RetroArchSaveSortingProvider
    get_core_name: CoreNameProviderFn
    state_persister: StatePersister
    settings_persister: SettingsPersister
    metadata_cache_persister: MetadataCachePersister
    firmware_cache_persister: FirmwareCachePersister
    save_sync_state_persister: SaveSyncStatePersister
    registry_store: ShortcutRegistryStore
    metadata_store: MetadataCacheStore
    log_debug: DebugLogger
    plugin_metadata: PluginMetadataReader


@dataclass(frozen=True)
class RuntimeAdaptersBundle:
    """Concrete adapters for the Clock/UuidGen/Sleeper/HostnameReader seams.

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


@dataclass(frozen=True)
class BootstrapHandles:
    """Bootstrap outputs ``main.py`` needs that don't fit the wiring bundles.

    Anything ``Plugin`` itself binds (not the services) lives here:
    the debug logger forwarded by ``Plugin._log_debug``. The bundles
    already cover everything passed to ``wire_services``; this struct
    keeps those Plugin-only handles typed instead of returning them
    via the untyped dict shape of yore.
    """

    debug_logger: DebugLogger


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
    constructs ``PersistenceAdapter``. Settings, plugin state, and the
    metadata cache are loaded + migrated inside here so the three
    domain-specific persister adapters (``StatePersisterAdapter`` /
    ``SettingsPersisterAdapter`` / ``MetadataCachePersisterAdapter``)
    bind the live dicts at construction; mutating any of those dicts
    from the caller side is visible to every adapter/service that
    holds the same reference.

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
    retrodeck_paths = RetroDeckPathsAdapter(user_home=user_home, logger=logger)
    retroarch_config = RetroArchConfigAdapter(user_home=user_home, logger=logger)
    retroarch_core_info = RetroArchCoreInfoAdapter(user_home=user_home, logger=logger)
    core_resolver = CoreResolver(
        plugin_dir=plugin_dir,
        logger=logger,
        get_retrodeck_home=retrodeck_paths.retrodeck_home,
    )
    gamelist_editor = GamelistXmlEditorAdapter(logger=logger)

    persistence = PersistenceAdapter(settings_dir, runtime_dir, logger)
    firmware_cache_persister = FirmwareCachePersisterAdapter(persistence)
    save_sync_state_persister = SaveSyncStatePersisterAdapter(persistence)
    settings = persistence.load_settings()
    settings = migrate_settings(settings)
    persistence.save_settings(settings)
    # Persistence + migration round-trip through bare ``dict`` because
    # ``load_state`` / ``migrate_state`` / ``load_metadata_cache`` predate the
    # TypedDicts and operate on the on-disk JSON shape. Cast down to ``dict``
    # at the boundary, cast up to ``PluginState`` / ``MetadataCache`` once the
    # shape is in hand so the rest of bootstrap sees the typed dict.
    state = cast("PluginState", persistence.load_state(cast("dict", _default_state())))
    state = cast("PluginState", migrate_state(cast("dict", state)))
    metadata_cache = cast("MetadataCache", persistence.load_metadata_cache())
    state_persister = StatePersisterAdapter(persistence, state)
    settings_persister = SettingsPersisterAdapter(persistence, settings)
    metadata_cache_persister = MetadataCachePersisterAdapter(persistence, metadata_cache)
    registry_store = RegistryStoreAdapter(state=state, logger=logger)
    metadata_store = MetadataCacheStoreAdapter(metadata_cache=metadata_cache)
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
    download_queue = DownloadQueueAdapter()
    firmware_file_store = FirmwareFileAdapter()
    migration_file_store = MigrationFileAdapter()
    rom_file_store = RomFileAdapter()
    save_file_store = SaveFileAdapter()
    path_probe = PathProbeAdapter()
    clock = SystemClock()
    uuid_gen = SystemUuidGen()
    sleeper = AsyncioSleeper()
    hostname_provider = HostnameAdapter()
    debug_logger = SettingsAwareDebugLogger(settings=settings, logger=logger)
    save_sync_state = SaveService.make_default_state()

    adapters = AdapterBundle(
        http_adapter=http_adapter,
        romm_api=romm_api,
        steam_config=steam_config,
        sgdb_adapter=sgdb_adapter,
        cover_art_file_store=cover_art_file_store,
        sgdb_artwork_cache=sgdb_artwork_cache,
        download_file_store=download_file_store,
        download_queue=download_queue,
        firmware_file_store=firmware_file_store,
        migration_file_store=migration_file_store,
        rom_file_store=rom_file_store,
        save_file_store=save_file_store,
        gamelist_editor=gamelist_editor,
        path_probe=path_probe,
        core_info_provider=core_resolver,
    )
    stores = StateBundle(
        state=state,
        settings=settings,
        metadata_cache=metadata_cache,
        save_sync_state=save_sync_state,
    )
    callbacks = CallbackBundle(
        retrodeck_paths=retrodeck_paths,
        get_retroarch_save_sorting=retroarch_config.get_retroarch_save_sorting,
        get_core_name=retroarch_core_info.get_corename,
        state_persister=state_persister,
        settings_persister=settings_persister,
        metadata_cache_persister=metadata_cache_persister,
        firmware_cache_persister=firmware_cache_persister,
        save_sync_state_persister=save_sync_state_persister,
        registry_store=registry_store,
        metadata_store=metadata_store,
        log_debug=debug_logger,
        plugin_metadata=plugin_metadata,
    )
    runtime_adapters = RuntimeAdaptersBundle(
        clock=clock,
        uuid_gen=uuid_gen,
        sleeper=sleeper,
        hostname_provider=hostname_provider,
    )
    handles = BootstrapHandles(debug_logger=debug_logger)

    return BootstrapResult(
        adapters=adapters,
        stores=stores,
        callbacks=callbacks,
        runtime_adapters=runtime_adapters,
        handles=handles,
    )


def wire_services(cfg: WiringConfig) -> dict:
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
    bios_files_index_binding: LateBinding[dict] = LateBinding("bios_files_index")
    pending_sync_binding: LateBinding[dict] = LateBinding("pending_sync")

    # MigrationService is constructed before SaveService so that
    # save_sync_service can receive a bound reference to
    # ``migration_service.detect_save_sort_change``. SaveService must observe
    # fresh sort state before computing saves_dir (#238).
    migration_service = MigrationService(
        config=MigrationServiceConfig(
            migration_file_store=cfg.adapters.migration_file_store,
            state=cfg.stores.state,
            settings=cfg.stores.settings,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            state_persister=cfg.callbacks.state_persister,
            settings_persister=cfg.callbacks.settings_persister,
            emit=cfg.runtime.emit,
            get_bios_files_index=bios_files_index_binding.get,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            get_retroarch_save_sorting=cfg.callbacks.get_retroarch_save_sorting,
            get_active_core=cfg.adapters.core_info_provider.get_active_core,
            get_core_name=cfg.callbacks.get_core_name,
        ),
    )

    save_service_config = SaveServiceConfig(
        romm_api=cfg.adapters.romm_api,
        retry=cfg.adapters.http_adapter,
        settings=cfg.stores.settings,
        state=cfg.stores.state,
        save_sync_state=cfg.stores.save_sync_state,
        save_sync_state_persister=cfg.callbacks.save_sync_state_persister,
        save_file_store=cfg.adapters.save_file_store,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        retrodeck_paths=cfg.callbacks.retrodeck_paths,
        get_active_core=cfg.adapters.core_info_provider.get_active_core,
        hostname_provider=cfg.runtime.hostname_provider,
        log_debug=cfg.callbacks.log_debug,
        get_core_name=cfg.callbacks.get_core_name,
        plugin_metadata=cfg.callbacks.plugin_metadata,
        plugin_dir=cfg.runtime.plugin_dir,
        emit=cfg.runtime.emit,
        # SaveService must observe fresh sort state before computing saves_dir (#238).
        detect_sort_change=migration_service.detect_save_sort_change,
        is_retrodeck_migration_pending=migration_service.is_retrodeck_migration_pending,
    )
    save_sync_service = SaveService(config=save_service_config)

    playtime_service = PlaytimeService(
        config=PlaytimeServiceConfig(
            romm_api=cfg.adapters.romm_api,
            retry=cfg.adapters.http_adapter,
            save_sync_state=cfg.stores.save_sync_state,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            state_persister=save_sync_service,
            log_debug=cfg.callbacks.log_debug,
        ),
    )

    metadata_service = MetadataService(
        config=MetadataServiceConfig(
            state=cfg.stores.state,
            metadata_cache=cfg.stores.metadata_cache,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            metadata_cache_persister=cfg.callbacks.metadata_cache_persister,
            metadata_store=cfg.callbacks.metadata_store,
            log_debug=cfg.callbacks.log_debug,
        ),
    )

    artwork_service = ArtworkService(
        config=ArtworkServiceConfig(
            romm_api=cfg.adapters.romm_api,
            steam_config=cfg.adapters.steam_config,
            cover_art_file_store=cfg.adapters.cover_art_file_store,
            state=cfg.stores.state,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            get_pending_sync=pending_sync_binding.get,
            registry_store=cfg.callbacks.registry_store,
            state_persister=cfg.callbacks.state_persister,
        ),
    )

    shortcut_removal_service = ShortcutRemovalService(
        config=ShortcutRemovalServiceConfig(
            romm_api=cfg.adapters.romm_api,
            steam_config=cfg.adapters.steam_config,
            state=cfg.stores.state,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            emit=cfg.runtime.emit,
            state_persister=cfg.callbacks.state_persister,
            registry_store=cfg.callbacks.registry_store,
            artwork_remover=artwork_service,
        ),
    )

    sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=cfg.adapters.romm_api,
            steam_config=cfg.adapters.steam_config,
            state=cfg.stores.state,
            settings=cfg.stores.settings,
            metadata_cache=cfg.stores.metadata_cache,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            plugin_dir=cfg.runtime.plugin_dir,
            emit=cfg.runtime.emit,
            clock=cfg.runtime.clock,
            uuid_gen=cfg.runtime.uuid_gen,
            sleeper=cfg.runtime.sleeper,
            state_persister=cfg.callbacks.state_persister,
            settings_persister=cfg.callbacks.settings_persister,
            registry_store=cfg.callbacks.registry_store,
            log_debug=cfg.callbacks.log_debug,
            metadata_service=metadata_service,
            artwork=artwork_service,
        ),
    )
    pending_sync_binding.set(lambda: sync_service.pending_sync)

    download_service = DownloadService(
        config=DownloadServiceConfig(
            romm_api=cfg.adapters.romm_api,
            state=cfg.stores.state,
            download_file_store=cfg.adapters.download_file_store,
            download_queue=cfg.adapters.download_queue,
            resolve_system=cfg.adapters.http_adapter.resolve_system,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            runtime_dir=cfg.runtime.runtime_dir,
            emit=cfg.runtime.emit,
            clock=cfg.runtime.clock,
            sleeper=cfg.runtime.sleeper,
            state_persister=cfg.callbacks.state_persister,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            is_retrodeck_migration_pending=migration_service.is_retrodeck_migration_pending,
        ),
    )

    rom_removal_service = RomRemovalService(
        config=RomRemovalServiceConfig(
            state=cfg.stores.state,
            save_sync_state=cfg.stores.save_sync_state,
            logger=cfg.runtime.logger,
            loop=cfg.runtime.loop,
            state_persister=cfg.callbacks.state_persister,
            save_sync_state_writer=save_sync_service,
            rom_file_store=cfg.adapters.rom_file_store,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            download_queue_cleanup=download_service,
        ),
    )

    firmware_service = FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=cfg.adapters.romm_api,
            state=cfg.stores.state,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            plugin_dir=cfg.runtime.plugin_dir,
            clock=cfg.runtime.clock,
            state_persister=cfg.callbacks.state_persister,
            firmware_cache_persister=cfg.callbacks.firmware_cache_persister,
            firmware_file_store=cfg.adapters.firmware_file_store,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            core_info=cfg.adapters.core_info_provider,
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
            state=cfg.stores.state,
            settings=cfg.stores.settings,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            state_persister=cfg.callbacks.state_persister,
            settings_persister=cfg.callbacks.settings_persister,
            registry_store=cfg.callbacks.registry_store,
            get_pending_sync=pending_sync_binding.get,
            log_debug=cfg.callbacks.log_debug,
        ),
    )

    achievements_service = AchievementsService(
        config=AchievementsServiceConfig(
            romm_api=cfg.adapters.romm_api,
            state=cfg.stores.state,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            log_debug=cfg.callbacks.log_debug,
        ),
    )

    game_detail_service = GameDetailService(
        config=GameDetailServiceConfig(
            state=cfg.stores.state,
            metadata_cache=cfg.stores.metadata_cache,
            save_sync_state=cfg.stores.save_sync_state,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            bios_checker=firmware_service,
            achievements=achievements_service,
        ),
    )

    settings_service = SettingsService(
        config=SettingsServiceConfig(
            settings=cfg.stores.settings,
            state=cfg.stores.state,
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
            gamelist_editor=cfg.adapters.gamelist_editor,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            bios_checker=firmware_service,
        ),
    )

    connection_service = ConnectionService(
        config=ConnectionServiceConfig(
            settings=cfg.stores.settings,
            romm_api=cfg.adapters.romm_api,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            min_required_version=cfg.min_required_version,
        ),
    )

    startup_healing_service = StartupHealingService(
        config=StartupHealingServiceConfig(
            state=cfg.stores.state,
            logger=cfg.runtime.logger,
            state_persister=cfg.callbacks.state_persister,
            registry_store=cfg.callbacks.registry_store,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            path_probe=cfg.adapters.path_probe,
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
        "connection_service": connection_service,
        "startup_healing_service": startup_healing_service,
        "launch_gate_service": launch_gate_service,
        "session_lifecycle_service": session_lifecycle_service,
    }
