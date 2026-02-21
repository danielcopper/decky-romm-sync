import os
import json
import base64
import struct
import ssl
import urllib.parse
import urllib.request
import urllib.error
from typing import TYPE_CHECKING, Any

import decky

if TYPE_CHECKING:
    import asyncio
    from typing import Optional, Protocol

    class _SgdbDeps(Protocol):
        settings: dict
        _state: dict
        _pending_sync: dict
        loop: asyncio.AbstractEventLoop
        def _save_settings_to_disk(self) -> None: ...
        def _log_debug(self, msg: str) -> None: ...
        def _romm_request(self, path: str) -> Any: ...
        def _save_state(self) -> None: ...
        def _grid_dir(self) -> Optional[str]: ...
        def _read_shortcuts(self) -> dict: ...
        def _write_shortcuts(self, data: dict) -> None: ...


class SgdbMixin:
    def _sgdb_artwork_dir(self):
        art_dir = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "artwork")
        os.makedirs(art_dir, exist_ok=True)
        return art_dir

    def _sgdb_request(self, path):
        api_key = self.settings.get("steamgriddb_api_key", "")
        if not api_key:
            return None
        url = "https://www.steamgriddb.com/api/v2" + path
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("User-Agent", "decky-romm-sync/0.1")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def _get_sgdb_game_id(self, igdb_id):
        try:
            result = self._sgdb_request(f"/games/igdb/{igdb_id}")
            if result and result.get("success") and result.get("data"):
                return result["data"]["id"]
        except Exception as e:
            decky.logger.warning(f"SGDB lookup failed for IGDB {igdb_id}: {e}")
        return None

    def _download_sgdb_artwork(self, sgdb_game_id, rom_id, asset_type):
        type_map = {
            "hero": "heroes",
            "logo": "logos",
            "grid": "grids",
            "icon": "icons",
        }
        endpoint = type_map.get(asset_type)
        if not endpoint:
            return None

        art_dir = self._sgdb_artwork_dir()
        cached = os.path.join(art_dir, f"{rom_id}_{asset_type}.png")
        if os.path.exists(cached):
            return cached

        path = f"/{endpoint}/game/{sgdb_game_id}"
        if asset_type == "grid":
            path += "?dimensions=460x215,920x430"

        try:
            result = self._sgdb_request(path)
            if not result or not result.get("success") or not result.get("data"):
                return None
            image_url = result["data"][0]["url"]
            req = urllib.request.Request(image_url, method="GET")
            req.add_header("User-Agent", "decky-romm-sync/0.1")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            tmp_path = cached + ".tmp"
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
            os.replace(tmp_path, cached)
            return cached
        except Exception as e:
            decky.logger.warning(f"SGDB {asset_type} download failed for game {sgdb_game_id}: {e}")
            if os.path.exists(cached + ".tmp"):
                try:
                    os.remove(cached + ".tmp")
                except OSError:
                    pass
            return None

    async def save_steamgriddb_key(self, api_key):
        self.settings["steamgriddb_api_key"] = api_key
        self._save_settings_to_disk()
        return {"success": True}

    async def get_sgdb_artwork_base64(self, rom_id, asset_type_num):
        rom_id = int(rom_id)
        asset_type_num = int(asset_type_num)
        type_names = {1: "hero", 2: "logo", 3: "grid", 4: "icon"}
        asset_type = type_names.get(asset_type_num)
        self._log_debug(f"SGDB artwork request: rom_id={rom_id}, asset_type={asset_type_num}")
        if not asset_type:
            return {"base64": None, "no_api_key": False}

        art_dir = self._sgdb_artwork_dir()
        cached = os.path.join(art_dir, f"{rom_id}_{asset_type}.png")

        # Return from cache if available
        if os.path.exists(cached):
            self._log_debug(f"SGDB artwork cache hit: {cached}")
            try:
                with open(cached, "rb") as f:
                    return {"base64": base64.b64encode(f.read()).decode("ascii"), "no_api_key": False}
            except Exception as e:
                decky.logger.warning(f"Failed to read cached SGDB artwork: {e}")

        # Try to fetch from SGDB
        if not self.settings.get("steamgriddb_api_key"):
            self._log_debug("SGDB artwork skipped: no API key configured")
            return {"base64": None, "no_api_key": True}

        # Look up SGDB game ID from registry or pending sync
        reg = self._state["shortcut_registry"].get(str(rom_id), {})
        sgdb_id = reg.get("sgdb_id")
        igdb_id = reg.get("igdb_id")
        if not sgdb_id:
            pending = self._pending_sync.get(rom_id, {})
            sgdb_id = sgdb_id or pending.get("sgdb_id")
            igdb_id = igdb_id or pending.get("igdb_id")

        # On-demand fetch from RomM API for pre-existing ROMs missing IDs
        if not sgdb_id:
            try:
                rom_data = await self.loop.run_in_executor(None, self._romm_request, f"/api/roms/{rom_id}")
                if rom_data:
                    sgdb_id = rom_data.get("sgdb_id")
                    igdb_id = igdb_id or rom_data.get("igdb_id")
                self._log_debug(f"SGDB artwork: fetched sgdb_id={sgdb_id}, igdb_id={igdb_id} from RomM for rom_id={rom_id}")
                if str(rom_id) in self._state["shortcut_registry"]:
                    if sgdb_id:
                        self._state["shortcut_registry"][str(rom_id)]["sgdb_id"] = sgdb_id
                    if igdb_id:
                        self._state["shortcut_registry"][str(rom_id)]["igdb_id"] = igdb_id
                    self._save_state()
            except Exception as e:
                decky.logger.warning(f"SGDB artwork: failed to fetch IDs from RomM for rom_id={rom_id}: {e}")

        # Fallback: look up SGDB via IGDB ID if we have igdb_id but no sgdb_id
        if not sgdb_id and igdb_id:
            sgdb_id = await self.loop.run_in_executor(
                None, self._get_sgdb_game_id, igdb_id
            )
            if sgdb_id and str(rom_id) in self._state["shortcut_registry"]:
                self._state["shortcut_registry"][str(rom_id)]["sgdb_id"] = sgdb_id
                self._save_state()

        if not sgdb_id:
            self._log_debug(f"SGDB artwork skipped: no SGDB game found for rom_id={rom_id}")
            return {"base64": None, "no_api_key": False}

        path = await self.loop.run_in_executor(
            None, self._download_sgdb_artwork, sgdb_id, rom_id, asset_type
        )
        if path and os.path.exists(path):
            self._log_debug(f"SGDB artwork download success: rom_id={rom_id}, asset_type={asset_type}")
            try:
                with open(path, "rb") as f:
                    return {"base64": base64.b64encode(f.read()).decode("ascii"), "no_api_key": False}
            except Exception as e:
                decky.logger.warning(f"Failed to read SGDB artwork: {e}")
        else:
            self._log_debug(f"SGDB artwork download failed: rom_id={rom_id}, asset_type={asset_type}")

        return {"base64": None, "no_api_key": False}

    async def verify_sgdb_api_key(self, api_key=None):
        # Use saved key if no valid key provided (modal pattern doesn't hold the real key)
        if not api_key or api_key == "••••":
            api_key = self.settings.get("steamgriddb_api_key", "")
        if not api_key:
            return {"success": False, "message": "No API key configured"}
        try:
            url = "https://www.steamgriddb.com/api/v2/search/autocomplete/test"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("User-Agent", "decky-romm-sync/0.1")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = await self.loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, context=ctx, timeout=30)
            )
            data = json.loads(resp.read().decode())
            if data.get("success"):
                return {"success": True, "message": "API key is valid"}
            return {"success": False, "message": "API key rejected by SteamGridDB"}
        except urllib.error.HTTPError as e:
            decky.logger.warning(f"SGDB API key verification HTTP error: {e.code}")
            if e.code in (401, 403):
                return {"success": False, "message": "Invalid API key"}
            return {"success": False, "message": f"SteamGridDB error: HTTP {e.code}"}
        except Exception as e:
            decky.logger.error(f"SGDB API key verification failed: {e}")
            return {"success": False, "message": f"Connection failed: {e}"}

    async def save_sgdb_api_key(self, api_key):
        if api_key and api_key != "••••":
            self.settings["steamgriddb_api_key"] = api_key
            self._save_settings_to_disk()
        return {"success": True, "message": "SteamGridDB API key saved"}

    def _save_icon_to_grid(self, app_id, icon_bytes):
        """Write icon PNG to Steam's grid dir and update shortcuts.vdf icon field."""
        grid_dir = self._grid_dir()
        if not grid_dir:
            decky.logger.warning("Cannot find Steam grid directory for icon save")
            return False

        # Write icon file to grid dir
        icon_path = os.path.join(grid_dir, f"{app_id}_icon.png")
        tmp_path = icon_path + ".tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(icon_bytes)
            os.replace(tmp_path, icon_path)
        except Exception as e:
            decky.logger.error(f"Failed to write icon file {icon_path}: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

        # Update shortcuts.vdf icon field
        try:
            vdf_data = self._read_shortcuts()
            # Convert unsigned app_id to signed int32 for VDF comparison
            signed_id = struct.unpack("i", struct.pack("I", app_id & 0xFFFFFFFF))[0]
            shortcuts = vdf_data.get("shortcuts", {})
            for entry in shortcuts.values():
                if entry.get("appid") == signed_id:
                    entry["icon"] = icon_path
                    break
            self._write_shortcuts(vdf_data)
        except Exception as e:
            decky.logger.warning(f"Failed to update shortcuts.vdf icon field: {e}")
            # Icon file is still saved, just VDF field not set — non-fatal

        return True

    async def save_shortcut_icon(self, app_id, icon_base64):
        """Save icon PNG to Steam grid dir and update VDF. Called from frontend."""
        app_id = int(app_id)
        try:
            icon_bytes = base64.b64decode(icon_base64)
        except Exception as e:
            decky.logger.error(f"Failed to decode icon base64: {e}")
            return {"success": False}

        success = await self.loop.run_in_executor(
            None, self._save_icon_to_grid, app_id, icon_bytes
        )
        return {"success": success}
