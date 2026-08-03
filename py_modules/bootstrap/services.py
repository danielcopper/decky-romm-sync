"""Service half of the composition root — bundles in, live services out.

Service construction is separated from adapter construction because it
needs runtime state only ``main.py`` can supply (the event loop,
``decky.emit``) plus plugin state that exists once ``bootstrap()`` has
run. Services never reach each other by import: every cross-service
reference is threaded through a ``*ServiceConfig`` here, or deferred
through a ``LateBinding`` when the two constructors form a cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.shortcut_data import RETRODECK_APP_ID
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
from services.game_process import GameProcessService, GameProcessServiceConfig
from services.launch_gate import LaunchGateService, LaunchGateServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService, MetadataServiceConfig
from services.migration import MigrationService, MigrationServiceConfig
from services.playtime import PlaytimeService, PlaytimeServiceConfig
from services.prune import PruneService, PruneServiceConfig
from services.relaunch_options_resolver import RelaunchOptionsResolver, RelaunchOptionsResolverConfig
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig
from services.saves import SaveService, SaveServiceConfig
from services.session_lifecycle import SessionLifecycleService, SessionLifecycleServiceConfig
from services.settings import SettingsService, SettingsServiceConfig
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig
from services.startup_healing import StartupHealingService, StartupHealingServiceConfig
from services.steamgrid import SteamGridService, SteamGridServiceConfig
from services.version_switch import VersionSwitchService, VersionSwitchServiceConfig

if TYPE_CHECKING:
    from typing import Any

    from services.protocols import InstalledRomRemoverFn

    from .adapters import AdapterBundle, CallbackBundle, RuntimeBundle, StateBundle


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


def wire_services(cfg: WiringConfig) -> dict[str, Any]:
    """Create service instances after plugin state is initialised.

    Called from ``Plugin._main()`` after save-sync state is populated
    so that services receive live references to the fully-populated
    state dicts.

    Returns
    -------
    Every wired service, keyed by the attribute name ``Plugin._main()``
    binds it to. Callers index the keys they need; the mapping is not
    enumerated here because it grows with the service surface.
    """

    # Retry-progress surface (#1345): the RommHttpAdapter runs its retry+backoff
    # ladder inside ``run_in_executor`` worker threads, so each retry is off the
    # event loop. Wire a listener that marshals a ``server_retry_progress`` emit
    # back onto the loop (the same call_soon_threadsafe → create_task pattern the
    # download-progress emits use), letting the saves surfaces show a live
    # "connecting… (attempt N/M)" indicator instead of a frozen spinner. Wired
    # here (not in ``bootstrap``) because the loop and emit only exist at
    # service-wiring time; the adapter stays service-free (plain injected callable).
    def _emit_server_retry(attempt: int, max_attempts: int, delay_s: float) -> None:
        payload = {"attempt": attempt, "max_attempts": max_attempts, "delay_s": delay_s}
        loop = cfg.runtime.loop
        loop.call_soon_threadsafe(
            lambda: loop.create_task(cfg.runtime.emit("server_retry_progress", payload)),
        )

    cfg.adapters.http_adapter.on_retry = _emit_server_retry

    # Forward-reference bindings for producers constructed later in this
    # function. Consumers receive ``binding.get`` (a bound method); the
    # binding is populated via ``.set(...)`` once the producer exists.
    # Accessing ``.get()`` before ``.set()`` raises RuntimeError instead of
    # the NameError a bare forward-ref lambda would produce.
    bios_files_index_binding: LateBinding[dict[str, dict[str, Any]]] = LateBinding("bios_files_index")
    pending_sync_binding: LateBinding[dict[int, dict[str, Any]]] = LateBinding("pending_sync")
    # DownloadService needs RomRemovalService.remove_rom for the #1298 sibling
    # supersede, but RomRemovalService needs DownloadService's queue-cleanup seam —
    # a construction cycle. Bind the remover after both services exist.
    rom_remover_binding: LateBinding[InstalledRomRemoverFn] = LateBinding("rom_remover")

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

    # The single installed+bound relaunch-items resolver (#1154): snapshots the
    # rows in one short read UoW it closes before resolving core + disc, so the
    # nested resolver UoW never deadlocks. Both the RetroDECK-home migration and
    # the startup launch-options reconcile draw their relaunch items from this
    # SAME seam, so the two never carry a divergent build of the list.
    relaunch_options_resolver = RelaunchOptionsResolver(
        config=RelaunchOptionsResolverConfig(
            uow_factory=cfg.callbacks.uow_factory,
            active_core=active_core_resolver,
            disc_resolver=disc_launch_resolver,
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
            relaunch_options=relaunch_options_resolver,
            get_core_name=cfg.callbacks.get_core_name,
            uow_factory=cfg.callbacks.uow_factory,
        ),
    )

    save_service_config = SaveServiceConfig(
        romm_api=cfg.adapters.romm_api,
        retry=cfg.adapters.http_adapter,
        resolve_upload_conflict=cfg.adapters.resolve_upload_conflict,
        compute_sync_action=cfg.adapters.compute_sync_action,
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
            device_id_provider=save_sync_service,
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
            cover_cache_dir=os.path.join(cfg.runtime.runtime_dir, "covers"),
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
            renderer_rss=cfg.adapters.renderer_rss,
            renderer_gc=cfg.adapters.renderer_gc,
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
            rom_remover=rom_remover_binding.get,
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
    # Close the download↔removal cycle: DownloadService's sibling supersede now
    # resolves the live remover through this binding (#1298).
    rom_remover_binding.set(lambda: rom_removal_service.remove_rom)

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
            platform_core_reader=cfg.callbacks.platform_core_reader,
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
            forget_device=save_sync_service.forget_device,
            clear_playtime_scope_notice=playtime_service.clear_scope_notice,
        ),
    )

    startup_healing_service = StartupHealingService(
        config=StartupHealingServiceConfig(
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            path_probe=cfg.adapters.path_probe,
            uow_factory=cfg.callbacks.uow_factory,
            relaunch_options=relaunch_options_resolver,
        ),
    )

    launch_gate_service = LaunchGateService(
        config=LaunchGateServiceConfig(
            rom_lookup=sync_service,
            installed_checker=download_service,
            save_status_reader=save_sync_service,
            drift_reader=save_sync_service,
            save_file_store=cfg.adapters.save_file_store,
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
        ),
    )

    # Built after LaunchGateService + ConnectionService: the version-switch
    # save-stranding gate draws drift from LaunchGateService.check_local_drift and
    # reachability from ConnectionService.probe_reachability, and re-bakes a
    # switched-onto install via the relaunch resolver. The active-download guard
    # reads DownloadService's in-progress set (built earlier — no LateBinding) (#1298).
    version_switch_service = VersionSwitchService(
        config=VersionSwitchServiceConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            uow_factory=cfg.callbacks.uow_factory,
            romm_api=cfg.adapters.romm_api,
            settings=cfg.stores.settings,
            drift_probe=launch_gate_service.check_local_drift,
            reachability_probe=connection_service.probe_reachability,
            relaunch_resolver=relaunch_options_resolver,
            active_downloads=download_service.active_download_rom_ids,
        ),
    )

    # Stop Game (the running-overlay chevron action). Steam's own TerminateApp
    # cannot reach a flatpak-detached emulator, so the kill is backend-side; the
    # service owns the escalation policy and takes the RetroDECK app id from the
    # single domain constant the launch command is built from. The instance the
    # ladder signals is matched against the launch path from the SAME resolver
    # that bakes the shortcut's launch command, so the path it compares a live
    # process against is the path that was launched.
    game_process_service = GameProcessService(
        config=GameProcessServiceConfig(
            game_process=cfg.adapters.game_process,
            launch_path=relaunch_options_resolver,
            sleeper=cfg.runtime.sleeper,
            logger=cfg.runtime.logger,
            log_debug=cfg.callbacks.log_debug,
            flatpak_app_id=RETRODECK_APP_ID,
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

    prune_service = PruneService(
        config=PruneServiceConfig(
            loop=cfg.runtime.loop,
            logger=cfg.runtime.logger,
            clock=cfg.runtime.clock,
            uuid_gen=cfg.runtime.uuid_gen,
            emit=cfg.runtime.emit,
            uow_factory=cfg.callbacks.uow_factory,
            romm_api=cfg.adapters.romm_api,
            recovery_store=cfg.adapters.recovery_store,
            prune_artifacts=cfg.adapters.prune_artifacts,
            steam_recovery=cfg.adapters.steam_recovery,
            retrodeck_paths=cfg.callbacks.retrodeck_paths,
            save_coordinator=save_sync_service.prune_support,
            active_downloads=download_service.active_download_rom_ids,
            drift_probe=launch_gate_service.check_local_drift,
            remove_installed_files=rom_removal_service.delete_rom_files,
            switch_version=version_switch_service.switch_version,
            settings=cfg.stores.settings,
        )
    )

    return {
        "save_sync_service": save_sync_service,
        "playtime_service": playtime_service,
        "sync_service": sync_service,
        "download_service": download_service,
        "rom_removal_service": rom_removal_service,
        "prune_service": prune_service,
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
        "version_switch_service": version_switch_service,
        "connection_service": connection_service,
        "startup_healing_service": startup_healing_service,
        "launch_gate_service": launch_gate_service,
        "session_lifecycle_service": session_lifecycle_service,
        "game_process_service": game_process_service,
        "relaunch_options_resolver": relaunch_options_resolver,
    }
