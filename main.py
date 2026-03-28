import asyncio
import contextlib
import os
import sys
from typing import ClassVar

plugin_dir = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(plugin_dir, "py_modules"))
sys.path.insert(0, plugin_dir)

import decky
from bootstrap import WiringConfig, bootstrap, wire_services

from adapters.persistence import PersistenceAdapter
from adapters.retrodeck_config import RetroDeckConfigAdapter


class Plugin:
    settings: dict
    loop: asyncio.AbstractEventLoop

    # -- logging ---------------------------------------------------------------

    LOG_LEVELS: ClassVar[dict] = {"debug": 0, "info": 1, "warn": 2, "error": 3}

    def _log_debug(self, msg):
        """Log a message only when log_level allows debug messages."""
        configured = self.settings.get("log_level", "warn")
        if self.LOG_LEVELS.get("debug", 0) >= self.LOG_LEVELS.get(configured, 2):
            decky.logger.info(msg)

    # -- persistence delegates -------------------------------------------------

    @property
    def _persistence(self) -> PersistenceAdapter:
        """Lazy-init persistence adapter; overwritten by _main() with the bootstrap instance."""
        if not hasattr(self, "_persistence_instance"):
            self._persistence_instance = PersistenceAdapter(
                decky.DECKY_PLUGIN_SETTINGS_DIR,
                decky.DECKY_PLUGIN_RUNTIME_DIR,
                decky.logger,
            )
        return self._persistence_instance

    @_persistence.setter
    def _persistence(self, value: PersistenceAdapter) -> None:
        self._persistence_instance = value

    def _save_state(self):
        self._persistence.save_state(self._state)

    def _save_settings_to_disk(self):
        self._persistence.save_settings(self.settings)

    def _save_metadata_cache(self):
        self._persistence.save_metadata_cache(self._metadata_cache)

    def _load_metadata_cache(self):
        self._metadata_cache = self._persistence.load_metadata_cache()

    # -- pruning ---------------------------------------------------------------

    def _prune_stale_installed_roms(self):
        """Remove installed_roms entries whose files no longer exist on disk."""
        pruned = []
        for rom_id, entry in list(self._state["installed_roms"].items()):  # list(): dict mutated below
            file_path = entry.get("file_path", "")
            rom_dir = entry.get("rom_dir", "")
            if (file_path and os.path.exists(file_path)) or (rom_dir and os.path.exists(rom_dir)):
                continue
            decky.logger.info(f"Pruned stale installed_roms entry: {rom_id} ({file_path})")
            pruned.append(rom_id)
        for rom_id in pruned:
            del self._state["installed_roms"][rom_id]
        if pruned:
            self._save_state()

    def _prune_stale_registry(self):
        """Remove shortcut_registry entries with missing or invalid app_id."""
        pruned = []
        for rom_id, entry in list(self._state["shortcut_registry"].items()):  # list(): dict mutated below
            app_id = entry.get("app_id")
            if not app_id or not isinstance(app_id, int):
                decky.logger.info(f"Pruned stale registry entry: rom_id={rom_id} (invalid app_id={app_id})")
                pruned.append(rom_id)
        for rom_id in pruned:
            del self._state["shortcut_registry"][rom_id]
        if pruned:
            self._save_state()

    async def _main(self):  # Decky lifecycle — must be async
        self.loop = asyncio.get_event_loop()

        # ── 1. Load settings & run migrations ───────────────────────────────
        from domain.state_migrations import migrate_settings, migrate_state

        self.settings = self._persistence.load_settings()
        self.settings = migrate_settings(self.settings)
        self._save_settings_to_disk()

        # ── 2. Wire adapters ────────────────────────────────────────────────
        adapters = bootstrap(
            settings_dir=decky.DECKY_PLUGIN_SETTINGS_DIR,
            runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            user_home=decky.DECKY_USER_HOME,
            logger=decky.logger,
            settings=self.settings,
        )
        self._persistence = adapters["persistence"]
        self._http_adapter = adapters["http_adapter"]
        self._romm_api = adapters["romm_api"]
        self._steam_config = adapters["steam_config"]
        self._sgdb_adapter = adapters["sgdb_adapter"]
        self._retrodeck_config: RetroDeckConfigAdapter = adapters["retrodeck_config"]

        # ── 3. Load state ───────────────────────────────────────────────────
        self._state = {
            "shortcut_registry": {},
            "installed_roms": {},
            "last_sync": None,
            "sync_stats": {"platforms": 0, "roms": 0},
            "downloaded_bios": {},
            "retrodeck_home_path": "",
            "save_sort_settings": None,
        }
        self._metadata_cache = {}
        self._romm_version = None  # Detected on test_connection
        self._state = self._persistence.load_state(self._state)
        self._state = migrate_state(self._state)
        self._metadata_cache = self._persistence.load_metadata_cache()

        # ── 4. Wire services ────────────────────────────────────────────────
        from services.saves import SaveService

        self._save_sync_state = SaveService.make_default_state()
        services = wire_services(
            WiringConfig(
                http_adapter=self._http_adapter,
                romm_api=self._romm_api,
                steam_config=self._steam_config,
                sgdb_adapter=self._sgdb_adapter,
                state=self._state,
                settings=self.settings,
                metadata_cache=self._metadata_cache,
                save_sync_state=self._save_sync_state,
                loop=self.loop,
                logger=decky.logger,
                plugin_dir=decky.DECKY_PLUGIN_DIR,
                runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
                emit=decky.emit,
                get_saves_path=self._retrodeck_config.get_saves_path,
                get_roms_path=self._retrodeck_config.get_roms_path,
                get_bios_path=self._retrodeck_config.get_bios_path,
                get_retrodeck_home=self._retrodeck_config.get_retrodeck_home,
                get_retroarch_save_sorting=self._retrodeck_config.get_retroarch_save_sorting,
                save_state=self._save_state,
                save_settings_to_disk=self._save_settings_to_disk,
                save_metadata_cache=self._save_metadata_cache,
                save_firmware_cache=self._persistence.save_firmware_cache,
                load_firmware_cache=self._persistence.load_firmware_cache,
                log_debug=self._log_debug,
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
        self._firmware_service.load_bios_registry()

        # ── 5. Startup healing ──────────────────────────────────────────────
        self._save_sync_service.init_state()
        self._save_sync_service.load_state()
        self._prune_stale_installed_roms()
        self._prune_stale_registry()
        self._save_sync_service.prune_orphaned_state()
        self._sgdb_service.prune_orphaned_artwork_cache()
        self._artwork_service.prune_orphaned_staging_artwork()
        self._download_service.cleanup_leftover_tmp_files()

        # ── 6. Background tasks ─────────────────────────────────────────────
        self._migration_service.detect_retrodeck_path_change()
        self._migration_service.detect_save_sort_change()
        self.loop.create_task(self._download_service.poll_download_requests())
        decky.logger.info("RomM Sync plugin loaded")

    async def migrate_retrodeck_files(self, conflict_strategy=None):
        """Delegate to MigrationService."""
        return await self._migration_service.migrate_retrodeck_files(conflict_strategy)

    async def get_migration_status(self):
        """Delegate to MigrationService."""
        return await self._migration_service.get_migration_status()

    async def get_save_sort_migration_status(self):
        return await self._migration_service.get_save_sort_migration_status()

    async def migrate_save_sort_files(self, conflict_strategy=None):
        return await self._migration_service.migrate_save_sort_files(conflict_strategy)

    async def dismiss_save_sort_migration(self):
        return self._migration_service.dismiss_save_sort_migration()

    async def _unload(self):  # Decky lifecycle — must be async
        self._sync_service.shutdown()
        self._download_service.shutdown()
        decky.logger.info("RomM Sync plugin unloaded")

    _MIN_TESTED_VERSION = "4.6.1"

    # ── Callables ──────────────────────────────────────────────────────
    # All methods below are exposed to the frontend via Decky's callable()
    # framework, which requires `async def` even when no `await` is used.
    # S7503 warnings are suppressed in sonar-project.properties (fp1).

    async def test_connection(self):
        from lib.errors import error_response

        if not self.settings.get("romm_url"):
            return {"success": False, "message": "No server URL configured", "error_code": "config_error"}
        # Test basic connectivity (heartbeat may not require auth)
        try:
            heartbeat = await self.loop.run_in_executor(None, self._romm_api.heartbeat)
        except Exception as e:
            self._romm_version = None
            return error_response(e)

        # Extract server version from heartbeat
        self._romm_version = None
        with contextlib.suppress(AttributeError, TypeError):
            self._romm_version = heartbeat.get("SYSTEM", {}).get("VERSION")
        if self._romm_version:
            decky.logger.info(f"RomM server version: {self._romm_version}")
            self._romm_api.set_version(self._romm_version)

        # Test authenticated access
        try:
            await self.loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            resp = error_response(e)
            if resp["error_code"] not in ("auth_error", "forbidden_error"):
                resp["message"] = f"Server reachable but API request failed: {resp['message']}"
            return resp

        result = {"success": True, "message": "Connected to RomM"}
        if self._romm_version and self._romm_version != "development":
            result["message"] = f"Connected to RomM {self._romm_version}"
            result["romm_version"] = self._romm_version
            if self._romm_version < self._MIN_TESTED_VERSION:
                result["version_warning"] = (
                    f"RomM {self._romm_version} has not been tested. "
                    f"Minimum tested version: {self._MIN_TESTED_VERSION}."
                )
        elif self._romm_version == "development":
            result["romm_version"] = self._romm_version
        return result

    async def get_romm_version(self):
        """Return cached RomM version (detected on last test_connection)."""
        return {"version": self._romm_version}

    async def save_settings(self, romm_url, romm_user, romm_pass, allow_insecure_ssl=None):
        try:
            self.settings["romm_url"] = romm_url
            self.settings["romm_user"] = romm_user
            # Only update password if user entered a new one (not the masked placeholder)
            if romm_pass and romm_pass != "••••":
                self.settings["romm_pass"] = romm_pass
            if allow_insecure_ssl is not None:
                self.settings["romm_allow_insecure_ssl"] = bool(allow_insecure_ssl)
            self._save_settings_to_disk()
            return {"success": True, "message": "Settings saved"}
        except Exception as e:
            decky.logger.error(f"Failed to save settings: {e}")
            return {"success": False, "message": f"Save failed: {e}"}

    async def frontend_log(self, level, message):
        """Log a frontend message. Respects log_level setting."""
        configured = self.settings.get("log_level", "warn")
        if self.LOG_LEVELS.get(level, 0) >= self.LOG_LEVELS.get(configured, 2):
            if level == "error":
                decky.logger.error(f"[FE] {message}")
            elif level == "warn":
                decky.logger.warning(f"[FE] {message}")
            else:
                decky.logger.info(f"[FE] {message}")

    async def debug_log(self, message):
        """Backward-compat wrapper: logs at debug level."""
        await self.frontend_log("debug", message)

    async def save_log_level(self, level):
        if level not in ("debug", "info", "warn", "error"):
            return {"success": False, "message": "Invalid log level"}
        self.settings["log_level"] = level
        self._save_settings_to_disk()
        return {"success": True}

    async def save_steam_input_setting(self, mode):
        if mode not in ("default", "force_on", "force_off"):
            return {"success": False, "message": f"Invalid mode: {mode}"}
        self.settings["steam_input_mode"] = mode
        self._save_settings_to_disk()
        return {"success": True}

    async def apply_steam_input_setting(self):
        """Apply current Steam Input setting to all existing ROM shortcuts."""
        mode = self.settings.get("steam_input_mode", "default")
        app_ids = [entry["app_id"] for entry in self._state["shortcut_registry"].values() if "app_id" in entry]
        if not app_ids:
            return {"success": True, "message": "No shortcuts to update"}
        try:
            self._steam_config.set_steam_input_config(app_ids, mode=mode)
            return {"success": True, "message": f"Steam Input set to '{mode}' for {len(app_ids)} shortcuts"}
        except Exception as e:
            decky.logger.error(f"Failed to apply Steam Input setting: {e}")
            return {"success": False, "message": "Operation failed"}

    async def fix_retroarch_input_driver(self):
        """Change RetroArch input_driver from 'x' to 'sdl2'."""
        return self._steam_config.fix_retroarch_input_driver()

    async def get_settings(self):
        has_credentials = bool(self.settings.get("romm_user") and self.settings.get("romm_pass"))
        return {
            "romm_url": self.settings.get("romm_url", ""),
            "romm_user": self.settings.get("romm_user", ""),
            "romm_pass_masked": "••••" if self.settings.get("romm_pass") else "",
            "has_credentials": has_credentials,
            "steam_input_mode": self.settings.get("steam_input_mode", "default"),
            "sgdb_api_key_masked": "••••" if self.settings.get("steamgriddb_api_key") else "",
            "retroarch_input_check": self._steam_config.check_retroarch_input_driver(),
            "log_level": self.settings.get("log_level", "warn"),
            "romm_allow_insecure_ssl": self.settings.get("romm_allow_insecure_ssl", False),
            "collection_create_platform_groups": self.settings.get("collection_create_platform_groups", False),
        }

    async def get_whitelist_settings(self):
        """Return whitelist settings for the non-Steam game removal feature."""
        return {
            "disabled_defaults": self.settings.get("whitelist_disabled_defaults", []),
            "custom_names": self.settings.get("whitelist_custom_names", []),
        }

    async def update_whitelist_settings(self, disabled_defaults, custom_names):
        """Update whitelist settings. Both params must be lists of strings."""
        if not isinstance(disabled_defaults, list) or not all(isinstance(s, str) for s in disabled_defaults):
            return {"success": False, "message": "disabled_defaults must be a list of strings"}
        if not isinstance(custom_names, list) or not all(isinstance(s, str) for s in custom_names):
            return {"success": False, "message": "custom_names must be a list of strings"}
        self.settings["whitelist_disabled_defaults"] = disabled_defaults
        self.settings["whitelist_custom_names"] = custom_names
        self._save_settings_to_disk()
        return {"success": True}

    async def get_cached_game_detail(self, app_id):
        return self._game_detail_service.get_cached_game_detail(app_id)

    async def get_available_cores(self, platform_slug):
        """Return available RetroArch cores for a platform."""
        from domain import es_de_config

        cores = es_de_config.get_available_cores(platform_slug)
        active_core_so, active_core_label = es_de_config.get_active_core(platform_slug)
        return {
            "cores": cores,
            "active_core": active_core_so,
            "active_core_label": active_core_label,
        }

    @staticmethod
    def _set_system_core_io(retrodeck_home, platform_slug, core_label):
        """Sync helper for set_system_core — XML read/parse/write in executor."""
        from domain import es_de_config

        es_de_config.set_system_override(retrodeck_home, platform_slug, core_label or None)
        es_de_config._resolver.reset_cache()

    async def set_system_core(self, platform_slug, core_label):
        """Set system-wide core override. Pass empty string to reset to default."""
        retrodeck_home = self._retrodeck_config.get_retrodeck_home()
        if not retrodeck_home:
            return {"success": False, "message": "RetroDECK home not found"}
        try:
            await self.loop.run_in_executor(None, self._set_system_core_io, retrodeck_home, platform_slug, core_label)
            bios = await self._firmware_service.check_platform_bios(platform_slug)
            return {"success": True, "bios_status": bios}
        except Exception as e:
            decky.logger.error(f"Failed to set system core: {e}")
            return {"success": False, "message": str(e)}

    @staticmethod
    def _set_game_core_io(retrodeck_home, platform_slug, rom_path, core_label):
        """Sync helper for set_game_core — XML read/parse/write in executor."""
        from domain import es_de_config

        es_de_config.set_game_override(retrodeck_home, platform_slug, rom_path, core_label or None)
        es_de_config._resolver.reset_cache()

    async def set_game_core(self, platform_slug, rom_path, core_label):
        """Set per-game core override. Pass empty string to reset to platform default."""
        retrodeck_home = self._retrodeck_config.get_retrodeck_home()
        if not retrodeck_home:
            return {"success": False, "message": "RetroDECK home not found"}
        try:
            await self.loop.run_in_executor(
                None, self._set_game_core_io, retrodeck_home, platform_slug, rom_path, core_label
            )
            # Extract rom filename from path for per-game core detection
            rom_filename = rom_path.lstrip("./") if rom_path else None
            bios = await self._firmware_service.check_platform_bios(platform_slug, rom_filename=rom_filename)
            return {"success": True, "bios_status": bios}
        except Exception as e:
            decky.logger.error(f"Failed to set game core: {e}")
            return {"success": False, "message": str(e)}

    # ── Firmware delegation to FirmwareService ──────────────

    async def get_firmware_status(self):
        return await self._firmware_service.get_firmware_status()

    async def download_firmware(self, firmware_id):
        return await self._firmware_service.download_firmware(firmware_id)

    async def download_all_firmware(self, platform_slug):
        return await self._firmware_service.download_all_firmware(platform_slug)

    async def download_required_firmware(self, platform_slug):
        return await self._firmware_service.download_required_firmware(platform_slug)

    async def check_platform_bios(self, platform_slug, rom_filename=None):
        return await self._firmware_service.check_platform_bios(platform_slug, rom_filename=rom_filename)

    async def get_bios_status(self, rom_id):
        return await self._game_detail_service.get_bios_status(rom_id)

    async def delete_platform_bios(self, platform_slug):
        return await self._firmware_service.delete_platform_bios(platform_slug)

    # ── Sync delegation to LibraryService ─────────────────────

    async def get_platforms(self):
        return await self._sync_service.get_platforms()

    async def save_platform_sync(self, platform_id, enabled):
        return self._sync_service.save_platform_sync(platform_id, enabled)

    async def set_all_platforms_sync(self, enabled):
        return await self._sync_service.set_all_platforms_sync(enabled)

    async def get_collections(self):
        return await self._sync_service.get_collections()

    async def save_collection_sync(self, collection_id, enabled):
        return self._sync_service.save_collection_sync(collection_id, enabled)

    async def set_all_collections_sync(self, enabled, category=None):
        return await self._sync_service.set_all_collections_sync(enabled, category)

    async def save_collection_platform_groups(self, enabled):
        self.settings["collection_create_platform_groups"] = bool(enabled)
        self._save_settings_to_disk()
        return {"success": True}

    async def start_sync(self):
        return self._sync_service.start_sync()

    async def cancel_sync(self):
        return self._sync_service.cancel_sync()

    async def get_sync_progress(self):
        return self._sync_service.get_sync_progress()

    async def sync_heartbeat(self):
        return self._sync_service.sync_heartbeat()

    async def sync_preview(self):
        return await self._sync_service.sync_preview()

    async def sync_apply_delta(self, preview_id):
        return await self._sync_service.sync_apply_delta(preview_id)

    async def sync_cancel_preview(self):
        return self._sync_service.sync_cancel_preview()

    async def report_sync_results(self, rom_id_to_app_id, removed_rom_ids, cancelled=False):
        return await self._sync_service.report_sync_results(rom_id_to_app_id, removed_rom_ids, cancelled)

    async def get_registry_platforms(self):
        return self._sync_service.get_registry_platforms()

    async def remove_platform_shortcuts(self, platform_slug):
        return await self._shortcut_removal_service.remove_platform_shortcuts(platform_slug)

    async def remove_all_shortcuts(self):
        return self._shortcut_removal_service.remove_all_shortcuts()

    async def report_removal_results(self, removed_rom_ids):
        return await self._shortcut_removal_service.report_removal_results(removed_rom_ids)

    async def get_artwork_base64(self, rom_id):
        return await self._artwork_service.get_artwork_base64(int(rom_id), self._sync_service.pending_sync)

    async def clear_sync_cache(self):
        return self._sync_service.clear_sync_cache()

    async def get_sync_stats(self):
        return self._sync_service.get_sync_stats()

    async def get_rom_by_steam_app_id(self, app_id):
        return self._sync_service.get_rom_by_steam_app_id(app_id)

    # ── Download delegation to DownloadService ──────────────

    async def start_download(self, rom_id):
        return await self._download_service.start_download(rom_id)

    async def cancel_download(self, rom_id):
        return self._download_service.cancel_download(rom_id)

    async def get_download_queue(self):
        return self._download_service.get_download_queue()

    async def clear_completed_downloads(self):
        return self._download_service.clear_completed_downloads()

    async def get_installed_rom(self, rom_id):
        return self._download_service.get_installed_rom(rom_id)

    async def remove_rom(self, rom_id):
        result = await self._rom_removal_service.remove_rom(rom_id)
        if result.get("success"):
            self._download_service._download_queue.pop(int(rom_id), None)
        return result

    async def uninstall_all_roms(self):
        result = await self._rom_removal_service.uninstall_all_roms()
        if result.get("success"):
            self._download_service._download_queue.clear()
        return result

    # ── Save Sync / Playtime delegation to services ──────────

    async def ensure_device_registered(self):
        # Ensure RomM version is detected before device registration so that
        # supports_device_sync() returns True on v4.7+ servers.  Without this,
        # a plugin-start call to ensure_device_registered that runs before the
        # first test_connection would fall back to local-only UUID generation.
        if not getattr(self, "_romm_version", None):
            try:
                heartbeat = await self.loop.run_in_executor(None, self._romm_api.heartbeat)
                with contextlib.suppress(AttributeError, TypeError):
                    self._romm_version = heartbeat.get("SYSTEM", {}).get("VERSION")
                if self._romm_version:
                    self._romm_api.set_version(self._romm_version)
            except Exception:
                pass
        return self._save_sync_service.ensure_device_registered()

    async def get_save_status(self, rom_id):
        return await self._save_sync_service.get_save_status(rom_id)

    async def pre_launch_sync(self, rom_id):
        return await self._save_sync_service.pre_launch_sync(rom_id)

    async def post_exit_sync(self, rom_id):
        return await self._save_sync_service.post_exit_sync(rom_id)

    async def sync_rom_saves(self, rom_id):
        return await self._save_sync_service.sync_rom_saves(rom_id)

    async def get_save_slots(self, rom_id):
        return await self._save_sync_service.get_save_slots(rom_id)

    async def set_game_slot(self, rom_id, slot):
        return self._save_sync_service.set_game_slot(rom_id, slot)

    async def is_save_tracking_configured(self, rom_id):
        return self._save_sync_service.is_save_tracking_configured(rom_id)

    async def get_save_setup_info(self, rom_id):
        return await self._save_sync_service.get_save_setup_info(rom_id)

    async def confirm_slot_choice(self, rom_id, chosen_slot, migrate_from_slot="__no_migration__"):
        from services.saves import _NO_MIGRATION

        # null from frontend = None in Python — treat same as no migration
        actual = _NO_MIGRATION if migrate_from_slot in ("__no_migration__", None) else migrate_from_slot
        return await self._save_sync_service.confirm_slot_choice(rom_id, chosen_slot, actual)

    async def sync_all_saves(self):
        return await self._save_sync_service.sync_all_saves()

    async def resolve_conflict(self, rom_id, filename, resolution, server_save_id=None, local_path=None):
        return await self._save_sync_service.resolve_conflict(rom_id, filename, resolution, server_save_id, local_path)

    async def resolve_newer_in_slot(self, rom_id, filename, resolution, newer_save_id):
        return await self._save_sync_service.resolve_newer_in_slot(rom_id, filename, resolution, newer_save_id)

    async def get_save_sync_settings(self):
        return self._save_sync_service.get_save_sync_settings()

    async def update_save_sync_settings(self, settings):
        return self._save_sync_service.update_save_sync_settings(settings)

    async def delete_local_saves(self, rom_id):
        return self._save_sync_service.delete_local_saves(rom_id)

    async def delete_platform_saves(self, platform_slug):
        return self._save_sync_service.delete_platform_saves(platform_slug)

    async def record_session_start(self, rom_id):
        return self._playtime_service.record_session_start(rom_id)

    async def record_session_end(self, rom_id):
        return await self._playtime_service.record_session_end(rom_id)

    async def get_server_playtime(self, rom_id):
        return await self._playtime_service.get_server_playtime(rom_id)

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

    async def sync_achievements_after_session(self, rom_id):
        return await self._achievements_service.sync_achievements_after_session(rom_id)
