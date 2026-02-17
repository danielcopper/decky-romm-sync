import os
import sys
import asyncio

plugin_dir = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(plugin_dir, "py_modules"))
sys.path.insert(0, plugin_dir)

import decky

from backend.state import StateMixin
from backend.romm_client import RommClientMixin
from backend.steam_config import SteamConfigMixin
from backend.firmware import FirmwareMixin, BIOS_DEST_MAP
from backend.metadata import MetadataMixin
from backend.sgdb import SgdbMixin
from backend.downloads import DownloadMixin
from backend.sync import SyncMixin


class Plugin(StateMixin, RommClientMixin, SgdbMixin, SteamConfigMixin, FirmwareMixin, MetadataMixin, DownloadMixin, SyncMixin):
    settings: dict
    loop: asyncio.AbstractEventLoop

    async def _main(self):
        self.loop = asyncio.get_event_loop()
        self._load_settings()
        self._sync_running = False
        self._sync_cancel = False
        self._sync_progress = {
            "running": False,
            "phase": "",
            "current": 0,
            "total": 0,
            "message": "",
        }
        self._state = {
            "shortcut_registry": {},
            "installed_roms": {},
            "last_sync": None,
            "sync_stats": {"platforms": 0, "roms": 0},
        }
        self._pending_sync = {}
        self._download_tasks = {}   # rom_id -> asyncio.Task
        self._download_queue = {}   # rom_id -> DownloadItem dict
        self._download_in_progress = set()  # rom_ids currently being processed
        self._metadata_cache = {}
        self._load_state()
        self._load_metadata_cache()
        self._prune_stale_state()
        self.loop.create_task(self._poll_download_requests())
        decky.logger.info("RomM Sync plugin loaded")

    async def _unload(self):
        if self._sync_running:
            self._sync_cancel = True
        # Cancel all active downloads
        for rom_id, task in list(self._download_tasks.items()):
            task.cancel()
        self._download_tasks.clear()
        decky.logger.info("RomM Sync plugin unloaded")

    async def test_connection(self):
        if not self.settings.get("romm_url"):
            return {"success": False, "message": "No server URL configured"}
        try:
            await self.loop.run_in_executor(
                None, self._romm_request, "/api/heartbeat"
            )
        except Exception as e:
            return {"success": False, "message": f"Cannot reach server: {e}"}
        try:
            await self.loop.run_in_executor(
                None, self._romm_request, "/api/platforms"
            )
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}
        return {"success": True, "message": "Connected to RomM"}

    async def save_settings(self, romm_url, romm_user, romm_pass):
        try:
            self.settings["romm_url"] = romm_url
            self.settings["romm_user"] = romm_user
            # Only update password if user entered a new one (not the masked placeholder)
            if romm_pass and romm_pass != "••••":
                self.settings["romm_pass"] = romm_pass
            self._save_settings_to_disk()
            return {"success": True, "message": "Settings saved"}
        except Exception as e:
            decky.logger.error(f"Failed to save settings: {e}")
            return {"success": False, "message": f"Save failed: {e}"}

    async def save_debug_logging(self, enabled):
        self.settings["debug_logging"] = bool(enabled)
        self._save_settings_to_disk()
        return {"success": True}

    async def save_steam_input_setting(self, mode):
        if mode not in ("default", "force_on", "force_off"):
            return {"success": False, "message": f"Invalid mode: {mode}"}
        self.settings["steam_input_mode"] = mode
        self._save_settings_to_disk()
        return {"success": True}

    async def get_settings(self):
        has_credentials = bool(
            self.settings.get("romm_user") and self.settings.get("romm_pass")
        )
        return {
            "romm_url": self.settings.get("romm_url", ""),
            "romm_user": self.settings.get("romm_user", ""),
            "romm_pass_masked": "••••" if self.settings.get("romm_pass") else "",
            "has_credentials": has_credentials,
            "steam_input_mode": self.settings.get("steam_input_mode", "default"),
            "sgdb_api_key_masked": "••••" if self.settings.get("steamgriddb_api_key") else "",
            "retroarch_input_check": self._check_retroarch_input_driver(),
            "debug_logging": self.settings.get("debug_logging", False),
        }
