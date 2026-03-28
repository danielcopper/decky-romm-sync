"""Composition root — wires adapters and services for the plugin.

Called from ``Plugin._main()`` to create adapter instances with
the correct Decky paths and logger.  Returns a dict so that
``_main()`` can assign them to the plugin's lazy-property backing
attributes (bypassing auto-creation from ``self.settings``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from adapters.persistence import PersistenceAdapter
from adapters.retrodeck_config import RetroDeckConfigAdapter
from adapters.romm.api_router import ApiRouter
from adapters.romm.http import RommHttpAdapter
from adapters.steam_config import SteamConfigAdapter
from adapters.steamgriddb import SteamGridDbAdapter
from domain import es_de_config as _es_de_config
from services.achievements import AchievementsService
from services.artwork import ArtworkService
from services.downloads import DownloadService
from services.firmware import FirmwareService
from services.game_detail import GameDetailService
from services.library import LibraryService
from services.metadata import MetadataService
from services.migration import MigrationService
from services.playtime import PlaytimeService
from services.protocols import (
    BiosPathProvider,
    DebugLogger,
    EventEmitter,
    RetroArchSaveSortingProvider,
    RetroDeckHomeProvider,
    RommApiProtocol,
    RomsPathProvider,
    SavesPathProvider,
    SettingsPersister,
    StatePersister,
)
from services.protocols import SteamConfigAdapter as SteamConfigProtocol
from services.rom_removal import RomRemovalService
from services.saves import SaveService
from services.shortcut_removal import ShortcutRemovalService
from services.steamgrid import SteamGridService


@dataclass
class WiringConfig:
    """Configuration bundle for wire_services — groups the 17 parameters
    into a single object to keep the composition root readable."""

    # Adapters
    http_adapter: RommHttpAdapter
    romm_api: RommApiProtocol
    steam_config: SteamConfigProtocol
    sgdb_adapter: SteamGridDbAdapter

    # State (live dict refs)
    state: dict
    settings: dict
    metadata_cache: dict
    save_sync_state: dict

    # Runtime
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    runtime_dir: str
    emit: EventEmitter

    # Callbacks
    get_saves_path: SavesPathProvider
    get_roms_path: RomsPathProvider
    get_bios_path: BiosPathProvider
    get_retrodeck_home: RetroDeckHomeProvider
    get_retroarch_save_sorting: RetroArchSaveSortingProvider
    save_state: StatePersister
    save_settings_to_disk: SettingsPersister
    save_metadata_cache: StatePersister
    save_firmware_cache: Callable[[dict], None]
    load_firmware_cache: Callable[[], dict]
    log_debug: DebugLogger


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
    # Configure domain modules with runtime paths/logger (removes decky coupling from domain).
    retrodeck_adapter = RetroDeckConfigAdapter(user_home=user_home, logger=logger)
    _es_de_config.configure(
        plugin_dir=plugin_dir,
        logger=logger,
        get_retrodeck_home=retrodeck_adapter.get_retrodeck_home,
    )

    persistence = PersistenceAdapter(settings_dir, runtime_dir, logger)
    http_adapter = RommHttpAdapter(settings, plugin_dir, logger)
    romm_api = cast(RommApiProtocol, ApiRouter(http_adapter))
    steam_config = SteamConfigAdapter(user_home=user_home, logger=logger)
    sgdb_adapter = SteamGridDbAdapter(settings=settings, logger=logger)

    return {
        "persistence": persistence,
        "http_adapter": http_adapter,
        "romm_api": romm_api,
        "steam_config": steam_config,
        "sgdb_adapter": sgdb_adapter,
        "retrodeck_config": retrodeck_adapter,
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
    save_sync_service = SaveService(
        romm_api=cfg.romm_api,
        retry=cfg.http_adapter,
        settings=cfg.settings,
        state=cfg.state,
        save_sync_state=cfg.save_sync_state,
        loop=cfg.loop,
        logger=cfg.logger,
        runtime_dir=cfg.runtime_dir,
        get_saves_path=cfg.get_saves_path,
        get_roms_path=cfg.get_roms_path,
        get_active_core=_es_de_config.get_active_core,
        plugin_version=_read_plugin_version(cfg.plugin_dir),
        emit=cfg.emit,
    )

    playtime_service = PlaytimeService(
        romm_api=cfg.romm_api,
        retry=cfg.http_adapter,
        save_sync_state=cfg.save_sync_state,
        loop=cfg.loop,
        logger=cfg.logger,
        save_state=save_sync_service.save_state,
    )

    metadata_service = MetadataService(
        romm_api=cfg.romm_api,
        state=cfg.state,
        metadata_cache=cfg.metadata_cache,
        loop=cfg.loop,
        logger=cfg.logger,
        save_metadata_cache=cfg.save_metadata_cache,
        log_debug=cfg.log_debug,
    )

    # Mutable container to break the circular dependency:
    # ArtworkService needs sync_state_ref but LibraryService isn't created yet.
    # We put a mutable list with one element; after LibraryService is created we
    # replace that element.  ArtworkService reads _sync_state_box[0]() so it always
    # gets the live value without any post-construction attribute mutation.
    _sync_state_box: list = [lambda: None]

    artwork_service = ArtworkService(
        romm_api=cfg.romm_api,
        steam_config=cfg.steam_config,
        state=cfg.state,
        loop=cfg.loop,
        logger=cfg.logger,
        emit=cfg.emit,
        sync_state_ref=lambda: _sync_state_box[0](),
    )

    shortcut_removal_service = ShortcutRemovalService(
        romm_api=cfg.romm_api,
        steam_config=cfg.steam_config,
        state=cfg.state,
        loop=cfg.loop,
        logger=cfg.logger,
        emit=cfg.emit,
        save_state=cfg.save_state,
        remove_artwork_files=artwork_service.remove_artwork_files,
    )

    sync_service = LibraryService(
        romm_api=cfg.romm_api,
        steam_config=cfg.steam_config,
        state=cfg.state,
        settings=cfg.settings,
        metadata_cache=cfg.metadata_cache,
        loop=cfg.loop,
        logger=cfg.logger,
        plugin_dir=cfg.plugin_dir,
        emit=cfg.emit,
        save_state=cfg.save_state,
        save_settings_to_disk=cfg.save_settings_to_disk,
        log_debug=cfg.log_debug,
        metadata_service=metadata_service,
        artwork=artwork_service,
    )

    # Resolve the circular dependency: point the box at the real sync_state getter.
    _sync_state_box[0] = lambda: sync_service.sync_state

    download_service = DownloadService(
        romm_api=cfg.romm_api,
        resolve_system=cfg.http_adapter.resolve_system,
        state=cfg.state,
        loop=cfg.loop,
        logger=cfg.logger,
        runtime_dir=cfg.runtime_dir,
        emit=cfg.emit,
        save_state=cfg.save_state,
        get_roms_path=cfg.get_roms_path,
        get_bios_path=cfg.get_bios_path,
    )

    rom_removal_service = RomRemovalService(
        state=cfg.state,
        save_sync_state=cfg.save_sync_state,
        logger=cfg.logger,
        loop=cfg.loop,
        save_state=cfg.save_state,
        save_save_sync_state=save_sync_service.save_state,
        get_roms_path=cfg.get_roms_path,
    )

    firmware_service = FirmwareService(
        romm_api=cfg.romm_api,
        state=cfg.state,
        loop=cfg.loop,
        logger=cfg.logger,
        plugin_dir=cfg.plugin_dir,
        save_state=cfg.save_state,
        save_firmware_cache=cfg.save_firmware_cache,
        load_firmware_cache=cfg.load_firmware_cache,
        get_bios_path=cfg.get_bios_path,
    )

    sgdb_service = SteamGridService(
        sgdb_api=cfg.sgdb_adapter,
        romm_api=cfg.romm_api,
        steam_config=cfg.steam_config,
        state=cfg.state,
        settings=cfg.settings,
        loop=cfg.loop,
        logger=cfg.logger,
        runtime_dir=cfg.runtime_dir,
        save_state=cfg.save_state,
        save_settings_to_disk=cfg.save_settings_to_disk,
        get_pending_sync=lambda: sync_service.pending_sync,
    )

    achievements_service = AchievementsService(
        romm_api=cfg.romm_api,
        state=cfg.state,
        loop=cfg.loop,
        logger=cfg.logger,
        log_debug=cfg.log_debug,
    )

    migration_service = MigrationService(
        state=cfg.state,
        loop=cfg.loop,
        logger=cfg.logger,
        save_state=cfg.save_state,
        emit=cfg.emit,
        get_bios_files_index=lambda: firmware_service.bios_files_index,
        get_retrodeck_home=cfg.get_retrodeck_home,
        get_saves_path=cfg.get_saves_path,
        get_bios_path=cfg.get_bios_path,
        get_retroarch_save_sorting=cfg.get_retroarch_save_sorting,
        get_roms_path=cfg.get_roms_path,
        get_active_core=_es_de_config.get_active_core,
    )

    game_detail_service = GameDetailService(
        state=cfg.state,
        metadata_cache=cfg.metadata_cache,
        save_sync_state=cfg.save_sync_state,
        logger=cfg.logger,
        bios_checker=firmware_service,
        achievements=achievements_service,
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
    }
