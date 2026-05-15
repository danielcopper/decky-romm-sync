"""Composition root — instantiates adapters and wires services.

Adapter construction lives here so ``main.py`` only deals with the
Decky lifecycle and the callable surface. ``bootstrap()`` also loads
and migrates settings as part of adapter wiring so ``RommHttpAdapter``
binds the live, migrated dict in a single pass; that same dict is
returned for the caller to keep as its source of truth.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from adapters.asyncio_sleeper import AsyncioSleeper
from adapters.cover_art_file_store import CoverArtFileStoreAdapter
from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.download_file import DownloadFileAdapter as DownloadFileAdapterImpl
from adapters.download_queue import DownloadQueueAdapter as DownloadQueueAdapterImpl
from adapters.es_de_config import CoreResolver, GamelistXmlEditor
from adapters.firmware_file import FirmwareFileAdapter as FirmwareFileAdapterImpl
from adapters.migration_file import MigrationFileAdapter as MigrationFileAdapterImpl
from adapters.path_probe import PathProbeAdapter
from adapters.persistence import (
    FirmwareCachePersisterAdapter,
    PersistenceAdapter,
    SaveSyncStatePersisterAdapter,
)
from adapters.retroarch_config import RetroArchConfigAdapter
from adapters.retroarch_core_info import RetroArchCoreInfoAdapter
from adapters.retrodeck_paths import RetroDeckPathsAdapter
from adapters.rom_files import RomFileAdapter as RomFileAdapterImpl
from adapters.romm.http import RommHttpAdapter
from adapters.romm.romm_api import RommApi
from adapters.save_file import SaveFileAdapter as SaveFileAdapterImpl
from adapters.sgdb_artwork_cache import SgdbArtworkCacheAdapter
from adapters.steam_config import SteamConfigAdapter
from adapters.steamgriddb import SteamGridDbAdapter
from adapters.system_clock import SystemClock
from adapters.system_uuid_gen import SystemUuidGen
from domain.save_state import SaveSyncState
from domain.state_migrations import migrate_settings
from lib.late_binding import LateBinding
from services.achievements import AchievementsService, AchievementsServiceConfig
from services.artwork import ArtworkService, ArtworkServiceConfig
from services.connection import ConnectionService, ConnectionServiceConfig
from services.cores import CoreService, CoreServiceConfig
from services.downloads import DownloadService, DownloadServiceConfig
from services.firmware import FirmwareService, FirmwareServiceConfig
from services.game_detail import GameDetailService, GameDetailServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService, MetadataServiceConfig
from services.migration import MigrationService, MigrationServiceConfig
from services.playtime import PlaytimeService, PlaytimeServiceConfig
from services.protocols import (
    BiosPathProvider,
    Clock,
    CoreInfoProvider,
    CoreNameProviderFn,
    CoverArtFileStore,
    DebugLogger,
    DownloadFileAdapter,
    DownloadQueueAdapter,
    EventEmitter,
    FirmwareCachePersister,
    FirmwareFileAdapter,
    GamelistXmlEditorProtocol,
    MigrationFileAdapter,
    PathExistsProbe,
    RetroArchSaveSortingProvider,
    RetroDeckHomeProvider,
    RomFileAdapter,
    RommApiProtocol,
    RomsPathProvider,
    SaveFileAdapter,
    SavesPathProvider,
    SaveSyncStatePersister,
    SettingsPersister,
    SgdbArtworkCache,
    Sleeper,
    StatePersister,
    UuidGen,
)
from services.protocols import SteamConfigAdapter as SteamConfigProtocol
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig
from services.saves import SaveService, SaveServiceConfig
from services.settings import SettingsService, SettingsServiceConfig
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig
from services.startup_healing import StartupHealingService, StartupHealingServiceConfig
from services.steamgrid import SteamGridConfig, SteamGridService


@dataclass(frozen=True)
class AdapterBundle:
    """Concrete I/O adapters wired into services."""

    http_adapter: RommHttpAdapter
    romm_api: RommApiProtocol
    steam_config: SteamConfigProtocol
    sgdb_adapter: SteamGridDbAdapter
    cover_art_file_store: CoverArtFileStore
    sgdb_artwork_cache: SgdbArtworkCache
    download_files: DownloadFileAdapter
    download_queue: DownloadQueueAdapter
    firmware_files: FirmwareFileAdapter
    migration_files: MigrationFileAdapter
    rom_files: RomFileAdapter
    save_file: SaveFileAdapter
    gamelist_editor: GamelistXmlEditorProtocol
    path_probe: PathExistsProbe


@dataclass(frozen=True)
class StateBundle:
    """Live mutable state stores shared across services."""

    state: dict
    settings: dict
    metadata_cache: dict
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


@dataclass(frozen=True)
class CallbackBundle:
    """Provider callables and persister Protocols injected into services."""

    get_saves_path: SavesPathProvider
    get_roms_path: RomsPathProvider
    get_bios_path: BiosPathProvider
    get_retrodeck_home: RetroDeckHomeProvider
    get_retroarch_save_sorting: RetroArchSaveSortingProvider
    get_core_name: CoreNameProviderFn
    save_state: StatePersister
    save_settings_to_disk: SettingsPersister
    save_metadata_cache: StatePersister
    firmware_cache_persister: FirmwareCachePersister
    core_info_provider: CoreInfoProvider
    save_sync_state_persister: SaveSyncStatePersister
    log_debug: DebugLogger


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
) -> dict:
    """Create and return all adapters.

    Bootstrap owns adapter instantiation and is the only path that
    constructs ``PersistenceAdapter``. Settings are loaded + migrated
    inside here so ``RommHttpAdapter`` receives a stable reference to
    the live dict; the migrated dict is written back to disk before
    return and shared with the caller under the ``"settings"`` key —
    mutating that dict from the caller side is visible to all
    adapters/services that bound the same reference.

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
    Mapping of adapter name to instance, plus ``"settings"`` (the live
    migrated settings dict shared with ``RommHttpAdapter``).
    """
    retrodeck_paths = RetroDeckPathsAdapter(user_home=user_home, logger=logger)
    retroarch_config = RetroArchConfigAdapter(user_home=user_home, logger=logger)
    retroarch_core_info = RetroArchCoreInfoAdapter(user_home=user_home, logger=logger)
    core_resolver = CoreResolver(
        plugin_dir=plugin_dir,
        logger=logger,
        get_retrodeck_home=retrodeck_paths.get_retrodeck_home,
    )
    gamelist_editor = GamelistXmlEditor(logger=logger)

    persistence = PersistenceAdapter(settings_dir, runtime_dir, logger)
    firmware_cache_persister = FirmwareCachePersisterAdapter(persistence)
    save_sync_state_persister = SaveSyncStatePersisterAdapter(persistence)
    settings = persistence.load_settings()
    settings = migrate_settings(settings)
    persistence.save_settings(settings)
    http_adapter = RommHttpAdapter(settings, plugin_dir, logger)
    romm_api = RommApi(http_adapter)
    steam_config = SteamConfigAdapter(user_home=user_home, logger=logger)
    sgdb_adapter = SteamGridDbAdapter(settings=settings, logger=logger)
    cover_art_file_store = CoverArtFileStoreAdapter()
    sgdb_artwork_cache = SgdbArtworkCacheAdapter(runtime_dir=runtime_dir)
    download_files = DownloadFileAdapterImpl()
    download_queue = DownloadQueueAdapterImpl()
    firmware_files = FirmwareFileAdapterImpl()
    migration_files = MigrationFileAdapterImpl()
    rom_files = RomFileAdapterImpl()
    save_file = SaveFileAdapterImpl()
    path_probe = PathProbeAdapter()
    clock = SystemClock()
    uuid_gen = SystemUuidGen()
    sleeper = AsyncioSleeper()
    debug_logger = SettingsAwareDebugLogger(settings=settings, logger=logger)

    return {
        "persistence": persistence,
        "firmware_cache_persister": firmware_cache_persister,
        "save_sync_state_persister": save_sync_state_persister,
        "settings": settings,
        "http_adapter": http_adapter,
        "romm_api": romm_api,
        "steam_config": steam_config,
        "sgdb_adapter": sgdb_adapter,
        "cover_art_file_store": cover_art_file_store,
        "sgdb_artwork_cache": sgdb_artwork_cache,
        "download_files": download_files,
        "download_queue": download_queue,
        "firmware_files": firmware_files,
        "migration_files": migration_files,
        "rom_files": rom_files,
        "save_file": save_file,
        "path_probe": path_probe,
        "retrodeck_paths": retrodeck_paths,
        "retroarch_config": retroarch_config,
        "retroarch_core_info": retroarch_core_info,
        "clock": clock,
        "uuid_gen": uuid_gen,
        "sleeper": sleeper,
        "debug_logger": debug_logger,
        "core_resolver": core_resolver,
        "gamelist_editor": gamelist_editor,
    }


def _read_plugin_version(plugin_dir: str) -> str:
    """Read plugin version from package.json."""
    import json
    import os

    try:
        with open(os.path.join(plugin_dir, "package.json")) as f:
            return json.load(f).get("version", "0.0.0")
    except (OSError, json.JSONDecodeError):
        return "0.0.0"


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
            migration_files=cfg.adapters.migration_files,
            state=cfg.stores.state,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            save_state=cfg.callbacks.save_state,
            emit=cfg.runtime.emit,
            get_bios_files_index=bios_files_index_binding.get,
            get_retrodeck_home=cfg.callbacks.get_retrodeck_home,
            get_saves_path=cfg.callbacks.get_saves_path,
            get_bios_path=cfg.callbacks.get_bios_path,
            get_retroarch_save_sorting=cfg.callbacks.get_retroarch_save_sorting,
            get_roms_path=cfg.callbacks.get_roms_path,
            get_active_core=cfg.callbacks.core_info_provider.get_active_core,
            get_core_name=cfg.callbacks.get_core_name,
        ),
    )

    save_service_config = SaveServiceConfig(
        romm_api=cfg.adapters.romm_api,
        retry=cfg.adapters.http_adapter,
        settings=cfg.stores.settings,
        state=cfg.stores.state,
        save_sync_state=cfg.stores.save_sync_state,
        runtime_dir=cfg.runtime.runtime_dir,
        save_sync_state_persister=cfg.callbacks.save_sync_state_persister,
        save_file=cfg.adapters.save_file,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        get_saves_path=cfg.callbacks.get_saves_path,
        get_roms_path=cfg.callbacks.get_roms_path,
        get_active_core=cfg.callbacks.core_info_provider.get_active_core,
        log_debug=cfg.callbacks.log_debug,
        get_core_name=cfg.callbacks.get_core_name,
        plugin_version=_read_plugin_version(cfg.runtime.plugin_dir),
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
            save_state=save_sync_service.save_state,
            log_debug=cfg.callbacks.log_debug,
        ),
    )

    metadata_service = MetadataService(
        config=MetadataServiceConfig(
            romm_api=cfg.adapters.romm_api,
            state=cfg.stores.state,
            metadata_cache=cfg.stores.metadata_cache,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            save_metadata_cache=cfg.callbacks.save_metadata_cache,
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
            save_state=cfg.callbacks.save_state,
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
            save_state=cfg.callbacks.save_state,
            save_settings_to_disk=cfg.callbacks.save_settings_to_disk,
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
            download_files=cfg.adapters.download_files,
            download_queue=cfg.adapters.download_queue,
            resolve_system=cfg.adapters.http_adapter.resolve_system,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            runtime_dir=cfg.runtime.runtime_dir,
            emit=cfg.runtime.emit,
            clock=cfg.runtime.clock,
            sleeper=cfg.runtime.sleeper,
            save_state=cfg.callbacks.save_state,
            get_roms_path=cfg.callbacks.get_roms_path,
            get_bios_path=cfg.callbacks.get_bios_path,
            is_retrodeck_migration_pending=migration_service.is_retrodeck_migration_pending,
        ),
    )

    rom_removal_service = RomRemovalService(
        config=RomRemovalServiceConfig(
            state=cfg.stores.state,
            save_sync_state=cfg.stores.save_sync_state,
            logger=cfg.runtime.logger,
            loop=cfg.runtime.loop,
            save_state=cfg.callbacks.save_state,
            save_save_sync_state=save_sync_service.save_state,
            rom_files=cfg.adapters.rom_files,
            get_roms_path=cfg.callbacks.get_roms_path,
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
            save_state=cfg.callbacks.save_state,
            firmware_cache_persister=cfg.callbacks.firmware_cache_persister,
            firmware_files=cfg.adapters.firmware_files,
            get_bios_path=cfg.callbacks.get_bios_path,
            core_info=cfg.callbacks.core_info_provider,
        ),
    )
    # Load the BIOS registry from disk now so the property does not raise
    # the pre-load RuntimeError when the binding's reader is later invoked.
    firmware_service.load_bios_registry()
    bios_files_index_binding.set(lambda: firmware_service.bios_files_index)

    sgdb_service = SteamGridService(
        config=SteamGridConfig(
            sgdb_api=cfg.adapters.sgdb_adapter,
            romm_api=cfg.adapters.romm_api,
            steam_config=cfg.adapters.steam_config,
            sgdb_artwork_cache=cfg.adapters.sgdb_artwork_cache,
            state=cfg.stores.state,
            settings=cfg.stores.settings,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            save_state=cfg.callbacks.save_state,
            save_settings_to_disk=cfg.callbacks.save_settings_to_disk,
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
            save_settings_to_disk=cfg.callbacks.save_settings_to_disk,
            steam_config=cfg.adapters.steam_config,
        ),
    )

    core_service = CoreService(
        config=CoreServiceConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            core_info=cfg.callbacks.core_info_provider,
            gamelist_editor=cfg.adapters.gamelist_editor,
            retrodeck_home=cfg.callbacks.get_retrodeck_home,
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
            save_state=cfg.callbacks.save_state,
            retrodeck_home=cfg.callbacks.get_retrodeck_home,
            path_probe=cfg.adapters.path_probe,
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
    }
