import asyncio
import os
import sys
from dataclasses import asdict
from typing import Any

plugin_dir = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(plugin_dir, "py_modules"))
sys.path.insert(0, plugin_dir)

import decky
from bootstrap import (
    RuntimeBundle,
    WiringConfig,
    bootstrap,
    wire_services,
)

from lib.migration_gate import migration_blocked


class Plugin:
    settings: dict
    loop: asyncio.AbstractEventLoop

    # Test-only attribute slots — production ``Plugin`` does not read
    # these after ``_main`` (the wired services own them), but the
    # test suite constructs ``Plugin()`` bare and pokes the same handles
    # the production wiring would set. Annotated as ``Any`` because
    # tests pass real adapters, ``MagicMock``s, or fakes interchangeably.
    # Annotations alone do not create the attribute, so bare access still
    # raises ``AttributeError`` (the ``TestPersistenceAttributeIsLoud``
    # regression remains green).
    _persistence: Any
    _state: Any
    _metadata_cache: Any
    _state_persister: Any
    _settings_persister: Any
    _metadata_cache_persister: Any
    _http_adapter: Any
    _romm_api: Any
    _steam_config: Any
    _retrodeck_paths: Any
    _save_sync_state: Any

    _MIN_REQUIRED_VERSION = (4, 8, 1)

    # -- logging ---------------------------------------------------------------
    #
    # ``_debug_logger`` is wired by ``_main()`` to the
    # ``SettingsAwareDebugLogger`` adapter built in ``bootstrap``. The
    # class-level default is a no-op so a bare ``Plugin()`` (used in test
    # fixtures that don't reach ``_main``) does not raise on
    # ``_log_debug`` — production always replaces it before any service
    # consumes the callback.
    _debug_logger = staticmethod(lambda _msg: None)

    def _log_debug(self, msg):
        """Forward a debug message through the wired ``DebugLogger`` adapter.

        Thin compatibility shim: production wiring sets ``_debug_logger``
        from bootstrap; tests construct ``Plugin`` bare and may patch in
        their own logger. The actual filtering logic lives in
        :class:`adapters.debug_logger.SettingsAwareDebugLogger`.
        """
        self._debug_logger(msg)

    async def _main(self):  # Decky lifecycle — must be async
        self.loop = asyncio.get_event_loop()

        # ── 1. Wire adapters ────────────────────────────────────────────────
        # Bootstrap loads + migrates settings as part of adapter construction
        # so RommHttpAdapter binds the live, migrated dict in one pass.
        result = bootstrap(
            settings_dir=decky.DECKY_PLUGIN_SETTINGS_DIR,
            runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            user_home=decky.DECKY_USER_HOME,
            logger=decky.logger,
        )
        self.settings = result.stores.settings
        self._debug_logger = result.handles.debug_logger

        # ── 4. Wire services ────────────────────────────────────────────────
        services = wire_services(
            WiringConfig(
                adapters=result.adapters,
                stores=result.stores,
                runtime=RuntimeBundle(
                    loop=self.loop,
                    logger=decky.logger,
                    plugin_dir=decky.DECKY_PLUGIN_DIR,
                    runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
                    emit=decky.emit,
                    clock=result.runtime_adapters.clock,
                    uuid_gen=result.runtime_adapters.uuid_gen,
                    sleeper=result.runtime_adapters.sleeper,
                    hostname_provider=result.runtime_adapters.hostname_provider,
                ),
                callbacks=result.callbacks,
                min_required_version=self._MIN_REQUIRED_VERSION,
            )
        )
        self._save_sync_service = services["save_sync_service"]
        self._playtime_service = services["playtime_service"]
        self._sync_service = services["sync_service"]
        self._download_service = services["download_service"]
        self._rom_removal_service = services["rom_removal_service"]
        self._firmware_service = services["firmware_service"]
        self._sgdb_service = services["sgdb_service"]
        self._metadata_service = services["metadata_service"]
        self._achievements_service = services["achievements_service"]
        self._migration_service = services["migration_service"]
        self._game_detail_service = services["game_detail_service"]
        self._artwork_service = services["artwork_service"]
        self._shortcut_removal_service = services["shortcut_removal_service"]
        self._settings_service = services["settings_service"]
        self._core_service = services["core_service"]
        self._connection_service = services["connection_service"]
        self._startup_healing_service = services["startup_healing_service"]
        self._launch_gate_service = services["launch_gate_service"]
        self._session_lifecycle_service = services["session_lifecycle_service"]

        # ── 5. Startup healing ──────────────────────────────────────────────
        self._save_sync_service.init_state()
        self._save_sync_service.load_state()
        # Detect retrodeck path changes BEFORE pruning so the prune can skip
        # entries living under a pending migration's previous home.
        self._migration_service.detect_retrodeck_path_change()
        self._startup_healing_service.prune_stale_installed_roms()
        self._startup_healing_service.prune_stale_registry()
        self._save_sync_service.prune_orphaned_state()
        self._sgdb_service.prune_orphaned_artwork_cache()
        self._artwork_service.prune_orphaned_staging_artwork()
        self._download_service.cleanup_leftover_tmp_files()

        # ── 6. Background tasks ─────────────────────────────────────────────
        self._migration_service.detect_save_sort_change()
        self._download_service.start()
        decky.logger.info("RomM Sync plugin loaded")

    async def _unload(self):  # Decky lifecycle — must be async
        self._sync_service.shutdown()
        await self._download_service.shutdown()
        decky.logger.info("RomM Sync plugin unloaded")

    # ── Callables ──────────────────────────────────────────────────────
    # All methods below are exposed to the frontend via Decky's callable()
    # framework, which requires `async def` even when no `await` is used.
    # S7503 warnings are suppressed in sonar-project.properties (fp1).

    async def test_connection(self):
        return await self._connection_service.test_connection()

    async def save_settings(self, romm_url, romm_user, romm_pass, allow_insecure_ssl=None):
        return self._settings_service.save_settings(romm_url, romm_user, romm_pass, allow_insecure_ssl)

    async def frontend_log(self, level, message):
        self._settings_service.frontend_log(level, message)

    async def debug_log(self, message):
        self._settings_service.frontend_log("debug", message)

    async def save_log_level(self, level):
        return self._settings_service.save_log_level(level)

    async def save_steam_input_setting(self, mode):
        return self._settings_service.save_steam_input_setting(mode)

    async def apply_steam_input_setting(self):
        return self._settings_service.apply_steam_input_setting()

    async def fix_retroarch_input_driver(self):
        return self._settings_service.fix_retroarch_input_driver()

    async def get_settings(self):
        return self._settings_service.get_settings()

    async def get_whitelist_settings(self):
        return self._settings_service.get_whitelist_settings()

    async def update_whitelist_settings(self, disabled_defaults, custom_names):
        return self._settings_service.update_whitelist_settings(disabled_defaults, custom_names)

    async def get_cached_game_detail(self, app_id):
        return self._game_detail_service.get_cached_game_detail(app_id)

    @migration_blocked
    async def set_system_core(self, platform_slug, core_label):
        return await self._core_service.set_system_core(platform_slug, core_label)

    @migration_blocked
    async def set_game_core(self, platform_slug, rom_path, core_label):
        return await self._core_service.set_game_core(platform_slug, rom_path, core_label)

    # ── Firmware delegation to FirmwareService ──────────────

    async def get_firmware_status(self):
        return await self._firmware_service.get_firmware_status()

    @migration_blocked
    async def download_all_firmware(self, platform_slug):
        return await self._firmware_service.download_all_firmware(platform_slug)

    @migration_blocked
    async def download_required_firmware(self, platform_slug):
        return await self._firmware_service.download_required_firmware(platform_slug)

    async def check_platform_bios(self, platform_slug, rom_filename=None):
        return await self._firmware_service.check_platform_bios(platform_slug, rom_filename=rom_filename)

    async def get_bios_status(self, rom_id):
        return await self._game_detail_service.get_bios_status(rom_id)

    @migration_blocked
    async def delete_platform_bios(self, platform_slug):
        return await self._firmware_service.delete_platform_bios(platform_slug)

    # ── Sync delegation to LibraryService ─────────────────────

    async def get_platforms(self):
        return await self._sync_service.get_platforms()

    @migration_blocked
    async def save_platform_sync(self, platform_id, enabled):
        return self._sync_service.save_platform_sync(platform_id, enabled)

    @migration_blocked
    async def set_all_platforms_sync(self, enabled):
        return await self._sync_service.set_all_platforms_sync(enabled)

    async def get_collections(self):
        return await self._sync_service.get_collections()

    @migration_blocked
    async def save_collection_sync(self, collection_id, enabled):
        return self._sync_service.save_collection_sync(collection_id, enabled)

    @migration_blocked
    async def set_all_collections_sync(self, enabled, category=None):
        return await self._sync_service.set_all_collections_sync(enabled, category)

    async def save_collection_platform_groups(self, enabled):
        return self._settings_service.save_collection_platform_groups(enabled)

    @migration_blocked
    async def start_sync(self):
        return self._sync_service.start_sync()

    async def cancel_sync(self):
        return self._sync_service.cancel_sync()

    async def sync_heartbeat(self):
        return self._sync_service.sync_heartbeat()

    @migration_blocked
    async def sync_preview(self):
        return await self._sync_service.sync_preview()

    @migration_blocked
    async def sync_apply_delta(self, preview_id):
        return await self._sync_service.sync_apply_delta(preview_id)

    async def sync_cancel_preview(self):
        return self._sync_service.sync_cancel_preview()

    async def report_unit_results(self, rom_id_to_app_id):
        return await self._sync_service.report_unit_results(rom_id_to_app_id)

    async def get_registry_platforms(self):
        return self._sync_service.get_registry_platforms()

    @migration_blocked
    async def remove_platform_shortcuts(self, platform_slug):
        return await self._shortcut_removal_service.remove_platform_shortcuts(platform_slug)

    @migration_blocked
    async def remove_all_shortcuts(self):
        return self._shortcut_removal_service.remove_all_shortcuts()

    async def report_removal_results(self, removed_rom_ids):
        return await self._shortcut_removal_service.report_removal_results(removed_rom_ids)

    async def get_artwork_base64(self, rom_id):
        return await self._artwork_service.get_artwork_base64(rom_id)

    @migration_blocked
    async def clear_sync_cache(self):
        return self._sync_service.clear_sync_cache()

    async def get_sync_stats(self):
        return self._sync_service.get_sync_stats()

    async def evaluate_launch(self, steam_app_id):
        verdict = await self._launch_gate_service.evaluate(steam_app_id)
        return asdict(verdict)

    async def finalize_game_session(self, rom_id):
        result = await self._session_lifecycle_service.finalize(rom_id)
        return asdict(result)

    # ── Download delegation to DownloadService ──────────────

    @migration_blocked
    async def start_download(self, rom_id):
        return await self._download_service.start_download(rom_id)

    async def cancel_download(self, rom_id):
        return self._download_service.cancel_download(rom_id)

    async def get_download_queue(self):
        return self._download_service.get_download_queue()

    async def get_installed_rom(self, rom_id):
        return self._download_service.get_installed_rom(rom_id)

    @migration_blocked
    async def remove_rom(self, rom_id):
        return await self._rom_removal_service.remove_rom(rom_id)

    @migration_blocked
    async def uninstall_all_roms(self):
        return await self._rom_removal_service.uninstall_all_roms()

    # ── Save Sync / Playtime delegation to services ──────────

    async def ensure_device_registered(self):
        return await self._save_sync_service.ensure_device_registered()

    async def list_devices(self):
        return await self._save_sync_service.list_devices()

    async def get_save_status(self, rom_id):
        return await self._save_sync_service.get_save_status(rom_id)

    async def check_core_change(self, rom_id):
        return self._save_sync_service.check_core_change(rom_id)

    @migration_blocked
    async def pre_launch_sync(self, rom_id):
        return await self._save_sync_service.pre_launch_sync(rom_id)

    @migration_blocked
    async def sync_rom_saves(self, rom_id):
        return await self._save_sync_service.sync_rom_saves(rom_id)

    async def get_save_slots(self, rom_id):
        return await self._save_sync_service.get_save_slots(rom_id)

    async def get_slot_saves(self, rom_id, slot):
        return await self._save_sync_service.get_slot_saves(rom_id, slot)

    @migration_blocked
    async def switch_slot(self, rom_id, new_slot):
        return await self._save_sync_service.switch_slot(rom_id, new_slot)

    async def get_slot_delete_info(self, rom_id, slot):
        return await self._save_sync_service.get_slot_delete_info(rom_id, slot)

    @migration_blocked
    async def delete_slot(self, rom_id, slot):
        return await self._save_sync_service.delete_slot(rom_id, slot)

    async def is_save_tracking_configured(self, rom_id):
        return self._save_sync_service.is_save_tracking_configured(rom_id)

    async def get_save_setup_info(self, rom_id):
        return await self._save_sync_service.get_save_setup_info(rom_id)

    @migration_blocked
    async def confirm_slot_choice(self, rom_id, chosen_slot, migrate_from_slot="__no_migration__"):
        return await self._save_sync_service.confirm_slot_choice(rom_id, chosen_slot, migrate_from_slot)

    @migration_blocked
    async def sync_all_saves(self):
        return await self._save_sync_service.sync_all_saves()

    @migration_blocked
    async def resolve_sync_conflict(self, rom_id, filename, server_save_id, action):
        return await self._save_sync_service.resolve_sync_conflict(rom_id, filename, server_save_id, action)

    async def get_save_sync_settings(self):
        return self._save_sync_service.get_save_sync_settings()

    @migration_blocked
    async def update_save_sync_settings(self, settings):
        return self._save_sync_service.update_save_sync_settings(settings)

    @migration_blocked
    async def delete_local_saves(self, rom_id):
        return self._save_sync_service.delete_local_saves(rom_id)

    @migration_blocked
    async def delete_platform_saves(self, platform_slug):
        return self._save_sync_service.delete_platform_saves(platform_slug)

    async def saves_list_file_versions(self, rom_id, slot, filename):
        return await self._save_sync_service.list_file_versions(rom_id, slot, filename)

    @migration_blocked
    async def saves_rollback_to_version(self, rom_id, slot, save_id):
        return await self._save_sync_service.rollback_to_version(rom_id, slot, save_id)

    async def record_session_start(self, rom_id):
        return self._playtime_service.record_session_start(rom_id)

    async def get_all_playtime(self):
        return self._playtime_service.get_all_playtime()

    # ── SGDB delegation to SteamGridService ───────────────────────

    async def get_sgdb_artwork_base64(self, rom_id, asset_type_num):
        return await self._sgdb_service.get_sgdb_artwork_base64(rom_id, asset_type_num)

    async def verify_sgdb_api_key(self, api_key=None):
        return await self._sgdb_service.verify_sgdb_api_key(api_key)

    async def save_sgdb_api_key(self, api_key):
        return self._sgdb_service.save_sgdb_api_key(api_key)

    async def save_shortcut_icon(self, app_id, icon_base64):
        return await self._sgdb_service.save_shortcut_icon(app_id, icon_base64)

    # ── Metadata delegation to MetadataService ────────────────

    async def get_rom_metadata(self, rom_id):
        return self._metadata_service.get_rom_metadata(rom_id)

    async def get_all_metadata_cache(self):
        return self._metadata_service.get_all_metadata_cache()

    async def get_app_id_rom_id_map(self):
        return self._metadata_service.get_app_id_rom_id_map()

    # ── Achievements delegation to AchievementsService ───────

    async def get_achievements(self, rom_id):
        return await self._achievements_service.get_achievements(rom_id)

    async def get_achievement_progress(self, rom_id):
        return await self._achievements_service.get_achievement_progress(rom_id)

    # ── Migration delegation to MigrationService ──────────────

    async def migrate_retrodeck_files(self, conflict_strategy=None):
        return await self._migration_service.migrate_retrodeck_files(conflict_strategy)

    async def get_migration_status(self):
        return await self._migration_service.get_migration_status()

    async def get_save_sort_migration_status(self):
        return await self._migration_service.get_save_sort_migration_status()

    async def migrate_save_sort_files(self, conflict_strategy=None):
        return await self._migration_service.migrate_save_sort_files(conflict_strategy)

    async def dismiss_save_sort_migration(self):
        return self._migration_service.dismiss_save_sort_migration()

    async def dismiss_retrodeck_migration(self):
        return self._migration_service.dismiss_retrodeck_migration()

    async def refresh_migration_state(self):
        return await self._migration_service.refresh_state()
