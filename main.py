import os
import json
import asyncio
import base64
import ssl
import urllib.request
import urllib.error
from pathlib import Path

import decky


class Plugin:
    settings: dict
    loop: asyncio.AbstractEventLoop

    async def _main(self):
        self.loop = asyncio.get_event_loop()
        self._load_settings()
        decky.logger.info("RomM Library plugin loaded")

    async def _unload(self):
        decky.logger.info("RomM Library plugin unloaded")

    def _load_settings(self):
        settings_path = os.path.join(
            decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"
        )
        try:
            with open(settings_path, "r") as f:
                self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.settings = {}
        self.settings.setdefault("romm_url", "")
        self.settings.setdefault("romm_user", "")
        self.settings.setdefault("romm_pass", "")

    def _save_settings_to_disk(self):
        settings_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
        os.makedirs(settings_dir, exist_ok=True)
        settings_path = os.path.join(settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump(self.settings, f, indent=2)

    def _romm_request(self, path):
        url = self.settings["romm_url"].rstrip("/") + path
        req = urllib.request.Request(url, method="GET")
        credentials = base64.b64encode(
            f"{self.settings['romm_user']}:{self.settings['romm_pass']}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {credentials}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read().decode())

    def _romm_download(self, path, dest, progress_callback=None):
        url = self.settings["romm_url"].rstrip("/") + path
        req = urllib.request.Request(url, method="GET")
        credentials = base64.b64encode(
            f"{self.settings['romm_user']}:{self.settings['romm_pass']}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {credentials}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, context=ctx) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            downloaded = 0
            block_size = 8192
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total:
                        progress_callback(downloaded, total)

    async def test_connection(self):
        try:
            self._romm_request("/api/heartbeat")
        except Exception as e:
            return {"success": False, "message": f"Cannot reach server: {e}"}
        try:
            self._romm_request("/api/platforms")
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}
        return {"success": True, "message": "Connected to RomM"}

    async def save_settings(self, romm_url, romm_user, romm_pass):
        self.settings["romm_url"] = romm_url
        self.settings["romm_user"] = romm_user
        self.settings["romm_pass"] = romm_pass
        self._save_settings_to_disk()
        return {"success": True, "message": "Settings saved"}

    async def get_settings(self):
        has_credentials = bool(
            self.settings.get("romm_user") and self.settings.get("romm_pass")
        )
        return {
            "romm_url": self.settings.get("romm_url", ""),
            "romm_user": self.settings.get("romm_user", ""),
            "romm_pass": "••••" if self.settings.get("romm_pass") else "",
            "has_credentials": has_credentials,
        }

    async def start_sync(self):
        return {"success": False, "message": "Not implemented yet"}

    async def cancel_sync(self):
        return {"success": False, "message": "Not implemented yet"}

    async def get_sync_progress(self):
        return {"success": False, "message": "Not implemented yet"}

    async def start_download(self):
        return {"success": False, "message": "Not implemented yet"}

    async def cancel_download(self):
        return {"success": False, "message": "Not implemented yet"}

    async def get_download_queue(self):
        return {"success": False, "message": "Not implemented yet"}

    async def get_installed_rom(self):
        return {"success": False, "message": "Not implemented yet"}

    async def get_rom_by_steam_app_id(self):
        return {"success": False, "message": "Not implemented yet"}

    async def remove_rom(self):
        return {"success": False, "message": "Not implemented yet"}
