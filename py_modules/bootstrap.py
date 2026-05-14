"""Composition root — wires adapters and services for the plugin.

Called from ``Plugin._main()`` to create adapter instances with
the correct Decky paths and logger.  Returns a dict so that
``_main()`` can assign them to the plugin's lazy-property backing
attributes (bypassing auto-creation from ``self.settings``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from adapters.asyncio_sleeper import AsyncioSleeper
from adapters.cover_art_file_store import CoverArtFileStoreAdapter
from adapters.download_file import DownloadFileAdapter as DownloadFileAdapterImpl
from adapters.download_queue import DownloadQueueAdapter as DownloadQueueAdapterImpl
from adapters.es_de_config import CoreResolver, GamelistXmlEditor
from adapters.firmware_file import FirmwareFileAdapter as FirmwareFileAdapterImpl
from adapters.migration_file import MigrationFileAdapter as MigrationFileAdapterImpl
from adapters.path_probe import PathProbeAdapter
from adapters.persistence import PersistenceAdapter
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
from services.achievements import AchievementsService
from services.artwork import ArtworkService, ArtworkServiceConfig
from services.connection import ConnectionService, ConnectionServiceConfig
from services.cores import CoreService, CoreServiceConfig
from services.downloads import DownloadService, DownloadServiceConfig
from services.firmware import FirmwareService, FirmwareServiceConfig
from services.game_detail import GameDetailService
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService
from services.migration import MigrationService, MigrationServiceConfig
from services.playtime import PlaytimeService
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
from services.shortcut_removal import ShortcutRemovalService
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
    """Live mutable state dicts shared across services."""

    state: dict
    settings: dict
    metadata_cache: dict
    save_sync_state: dict


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
    min_required_version: tuple[int, ...]


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
    """Composition-root inputs for ``wire_services`` grouped into four bundles."""

    adapters: AdapterBundle
    stores: StateBundle
    runtime: RuntimeBundle
    callbacks: CallbackBundle


def bootstrap(
    *,
    settings_dir: str,
    runtime_dir: str,
    plugin_dir: str,
    user_home: str,
    logger: logging.Logger,
    settings: dict,
) -> dict:
    """Create and return all adapters.

    Parameters
    ----------
    settings_dir:
        ``decky.DECKY_PLUGIN_SETTINGS_DIR``
    runtime_dir:
        ``decky.DECKY_PLUGIN_RUNTIME_DIR``
    plugin_dir:
        ``decky.DECKY_PLUGIN_DIR``
    logger:
        ``decky.logger``
    settings:
        The live settings dict (passed by reference to ``RommHttpAdapter``).

    Returns
    -------
    dict with keys ``persistence``, ``http_adapter``, and ``wire_services``
    (a factory callable for deferred service creation).
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

    return {
        "persistence": persistence,
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
    # MigrationService is constructed before SaveService so that
    # save_sync_service can receive a bound reference to
    # ``migration_service.detect_save_sort_change``. SaveService must observe
    # fresh sort state before computing saves_dir (#238).
    # ``get_bios_files_index`` is a lambda that defers the ``firmware_service``
    # lookup to call time, so it is safe to reference here even though
    # ``firmware_service`` is constructed later in this function.
    migration_service = MigrationService(
        migration_files=cfg.adapters.migration_files,
        config=MigrationServiceConfig(
            state=cfg.stores.state,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            save_state=cfg.callbacks.save_state,
            emit=cfg.runtime.emit,
            get_bios_files_index=lambda: firmware_service.bios_files_index,
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
        runtime_dir=cfg.runtime.runtime_dir,
        save_sync_state_persister=cfg.callbacks.save_sync_state_persister,
        save_file=cfg.adapters.save_file,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        get_saves_path=cfg.callbacks.get_saves_path,
        get_roms_path=cfg.callbacks.get_roms_path,
        get_active_core=cfg.callbacks.core_info_provider.get_active_core,
        get_core_name=cfg.callbacks.get_core_name,
        plugin_version=_read_plugin_version(cfg.runtime.plugin_dir),
        emit=cfg.runtime.emit,
        # SaveService must observe fresh sort state before computing saves_dir (#238).
        detect_sort_change=migration_service.detect_save_sort_change,
        is_retrodeck_migration_pending=migration_service.is_retrodeck_migration_pending,
    )
    save_sync_service = SaveService(
        romm_api=cfg.adapters.romm_api,
        retry=cfg.adapters.http_adapter,
        settings=cfg.stores.settings,
        state=cfg.stores.state,
        save_sync_state=cfg.stores.save_sync_state,
        config=save_service_config,
    )

    playtime_service = PlaytimeService(
        romm_api=cfg.adapters.romm_api,
        retry=cfg.adapters.http_adapter,
        save_sync_state=cfg.stores.save_sync_state,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        save_state=save_sync_service.save_state,
    )

    metadata_service = MetadataService(
        romm_api=cfg.adapters.romm_api,
        state=cfg.stores.state,
        metadata_cache=cfg.stores.metadata_cache,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        save_metadata_cache=cfg.callbacks.save_metadata_cache,
        log_debug=cfg.callbacks.log_debug,
    )

    artwork_service = ArtworkService(
        romm_api=cfg.adapters.romm_api,
        steam_config=cfg.adapters.steam_config,
        cover_art_file_store=cfg.adapters.cover_art_file_store,
        state=cfg.stores.state,
        config=ArtworkServiceConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            # ``sync_service`` is constructed after ArtworkService; the
            # lambda binds at call time, so the deferred lookup is safe
            # as long as ``get_artwork_base64`` is only invoked after
            # wire_services returns.
            get_pending_sync=lambda: sync_service.pending_sync,
        ),
    )

    shortcut_removal_service = ShortcutRemovalService(
        romm_api=cfg.adapters.romm_api,
        steam_config=cfg.adapters.steam_config,
        state=cfg.stores.state,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        emit=cfg.runtime.emit,
        save_state=cfg.callbacks.save_state,
        artwork_remover=artwork_service,
    )

    sync_service = LibraryService(
        romm_api=cfg.adapters.romm_api,
        steam_config=cfg.adapters.steam_config,
        state=cfg.stores.state,
        settings=cfg.stores.settings,
        metadata_cache=cfg.stores.metadata_cache,
        config=LibraryServiceConfig(
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
        ),
        metadata_service=metadata_service,
        artwork=artwork_service,
    )

    download_service = DownloadService(
        romm_api=cfg.adapters.romm_api,
        state=cfg.stores.state,
        download_files=cfg.adapters.download_files,
        download_queue=cfg.adapters.download_queue,
        config=DownloadServiceConfig(
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

    sgdb_service = SteamGridService(
        sgdb_api=cfg.adapters.sgdb_adapter,
        romm_api=cfg.adapters.romm_api,
        steam_config=cfg.adapters.steam_config,
        sgdb_artwork_cache=cfg.adapters.sgdb_artwork_cache,
        state=cfg.stores.state,
        settings=cfg.stores.settings,
        config=SteamGridConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            save_state=cfg.callbacks.save_state,
            save_settings_to_disk=cfg.callbacks.save_settings_to_disk,
            get_pending_sync=lambda: sync_service.pending_sync,
        ),
    )

    achievements_service = AchievementsService(
        romm_api=cfg.adapters.romm_api,
        state=cfg.stores.state,
        loop=cfg.runtime.loop,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        log_debug=cfg.callbacks.log_debug,
    )

    game_detail_service = GameDetailService(
        state=cfg.stores.state,
        metadata_cache=cfg.stores.metadata_cache,
        save_sync_state=cfg.stores.save_sync_state,
        logger=cfg.runtime.logger,
        clock=cfg.runtime.clock,
        bios_checker=firmware_service,
        achievements=achievements_service,
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
            min_required_version=cfg.runtime.min_required_version,
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
