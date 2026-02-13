import os
import sys
import json
import struct
import binascii
import asyncio
import base64
import ssl
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))
import vdf

import decky


class Plugin:
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
        self._load_state()
        decky.logger.info("RomM Library plugin loaded")

    async def _unload(self):
        if self._sync_running:
            self._sync_cancel = True
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
        self.settings.setdefault("enabled_platforms", {})

    def _save_settings_to_disk(self):
        settings_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
        os.makedirs(settings_dir, exist_ok=True)
        settings_path = os.path.join(settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump(self.settings, f, indent=2)

    def _load_state(self):
        state_path = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "state.json")
        try:
            with open(state_path, "r") as f:
                saved = json.load(f)
            self._state.update(saved)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_state(self):
        state_dir = decky.DECKY_PLUGIN_RUNTIME_DIR
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "state.json")
        tmp_path = state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp_path, state_path)

    def _find_steam_user_dir(self):
        """Find the active Steam user's userdata directory."""
        steam_paths = [
            os.path.join(decky.DECKY_USER_HOME, ".local", "share", "Steam", "userdata"),
            os.path.join(decky.DECKY_USER_HOME, ".steam", "steam", "userdata"),
        ]
        for base in steam_paths:
            if os.path.isdir(base):
                users = [d for d in os.listdir(base) if d.isdigit()]
                if len(users) == 1:
                    return os.path.join(base, users[0])
                # If multiple users, prefer most recently modified
                for u in users:
                    return os.path.join(base, u)
        return None

    def _shortcuts_vdf_path(self):
        user_dir = self._find_steam_user_dir()
        if not user_dir:
            return None
        return os.path.join(user_dir, "config", "shortcuts.vdf")

    def _grid_dir(self):
        user_dir = self._find_steam_user_dir()
        if not user_dir:
            return None
        grid = os.path.join(user_dir, "config", "grid")
        os.makedirs(grid, exist_ok=True)
        return grid

    def _generate_app_id(self, exe, appname):
        """Generate Steam shortcut app ID (signed int32)."""
        key = exe + appname
        crc = binascii.crc32(key.encode("utf-8")) & 0xFFFFFFFF
        return struct.unpack("i", struct.pack("I", crc | 0x80000000))[0]

    def _generate_artwork_id(self, exe, appname):
        """Generate unsigned artwork ID for grid filenames."""
        key = exe + appname
        crc = binascii.crc32(key.encode("utf-8")) & 0xFFFFFFFF
        return crc | 0x80000000

    def _read_shortcuts(self):
        path = self._shortcuts_vdf_path()
        if not path or not os.path.exists(path):
            return {"shortcuts": {}}
        with open(path, "rb") as f:
            return vdf.binary_loads(f.read())

    def _write_shortcuts(self, data):
        path = self._shortcuts_vdf_path()
        if not path:
            raise RuntimeError("Cannot find Steam shortcuts.vdf path")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(vdf.binary_dumps(data))
        os.replace(tmp_path, path)

    def _load_platform_map(self):
        config_path = os.path.join(decky.DECKY_PLUGIN_DIR, "defaults", "config.json")
        with open(config_path, "r") as f:
            config = json.load(f)
        return config.get("platform_map", {})

    def _resolve_system(self, platform_slug, platform_fs_slug=None):
        platform_map = self._load_platform_map()
        if platform_slug in platform_map:
            return platform_map[platform_slug]
        if platform_fs_slug and platform_fs_slug in platform_map:
            return platform_map[platform_fs_slug]
        return platform_slug

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
        # URL-encode the path, preserving already-valid characters (/:?=&)
        # RomM API returns paths with unencoded spaces in query params
        encoded_path = urllib.parse.quote(path, safe="/:?=&@")
        url = self.settings["romm_url"].rstrip("/") + encoded_path
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
        # Only update password if user entered a new one (not the masked placeholder)
        if romm_pass and romm_pass != "••••":
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
            "romm_pass_masked": "••••" if self.settings.get("romm_pass") else "",
            "has_credentials": has_credentials,
        }

    async def get_platforms(self):
        try:
            platforms = await self.loop.run_in_executor(
                None, self._romm_request, "/api/platforms"
            )
        except Exception as e:
            decky.logger.error(f"Failed to fetch platforms: {e}")
            return {"success": False, "message": f"Failed to fetch platforms: {e}"}

        enabled = self.settings.get("enabled_platforms", {})
        result = []
        for p in platforms:
            rom_count = p.get("rom_count", 0)
            if rom_count == 0:
                continue
            pid = str(p["id"])
            result.append({
                "id": p["id"],
                "name": p.get("name", ""),
                "slug": p.get("slug", ""),
                "rom_count": rom_count,
                "sync_enabled": enabled.get(pid, len(enabled) == 0),
            })
        return {"success": True, "platforms": result}

    async def save_platform_sync(self, platform_id, enabled):
        pid = str(platform_id)
        self.settings["enabled_platforms"][pid] = bool(enabled)
        self._save_settings_to_disk()
        return {"success": True}

    async def set_all_platforms_sync(self, enabled):
        enabled = bool(enabled)
        try:
            platforms = await self.loop.run_in_executor(
                None, self._romm_request, "/api/platforms"
            )
        except Exception as e:
            decky.logger.error(f"Failed to fetch platforms: {e}")
            return {"success": False, "message": f"Failed to fetch platforms: {e}"}

        ep = {}
        for p in platforms:
            ep[str(p["id"])] = enabled
        self.settings["enabled_platforms"] = ep
        self._save_settings_to_disk()
        return {"success": True}

    async def start_sync(self):
        if self._sync_running:
            return {"success": False, "message": "Sync already in progress"}
        self._sync_running = True
        self._sync_cancel = False
        self.loop.create_task(self._do_sync())
        return {"success": True, "message": "Sync started"}

    async def cancel_sync(self):
        self._sync_cancel = True
        return {"success": True, "message": "Sync cancelling..."}

    async def get_sync_progress(self):
        return self._sync_progress

    async def _do_sync(self):
        try:
            # Phase 1: Fetch platforms
            self._sync_progress = {
                "running": True,
                "phase": "platforms",
                "current": 0,
                "total": 0,
                "message": "Fetching platforms...",
            }
            await asyncio.sleep(0)

            try:
                platforms = await self.loop.run_in_executor(
                    None, self._romm_request, "/api/platforms"
                )
            except Exception as e:
                decky.logger.error(f"Failed to fetch platforms: {e}")
                self._sync_progress = {
                    "running": False,
                    "phase": "error",
                    "current": 0,
                    "total": 0,
                    "message": f"Failed to fetch platforms: {e}",
                }
                self._sync_running = False
                return

            if self._sync_cancel:
                self._finish_sync("Sync cancelled")
                return

            # Filter platforms by enabled_platforms setting
            # Default: all enabled only if no preferences saved yet
            enabled = self.settings.get("enabled_platforms", {})
            no_prefs = len(enabled) == 0
            decky.logger.info(f"Platform filter: {len(enabled)} prefs saved, no_prefs={no_prefs}")
            decky.logger.info(f"Enabled platforms: {[k for k,v in enabled.items() if v]}")
            platforms = [
                p for p in platforms
                if enabled.get(str(p["id"]), no_prefs)
            ]
            decky.logger.info(f"Syncing {len(platforms)} platforms: {[p['name'] for p in platforms]}")

            # Phase 2: Fetch ROMs per platform
            self._sync_progress = {
                "running": True,
                "phase": "roms",
                "current": 0,
                "total": 0,
                "message": "Fetching ROMs...",
            }
            await asyncio.sleep(0)

            all_roms = []
            for platform in platforms:
                if self._sync_cancel:
                    self._finish_sync("Sync cancelled")
                    return

                platform_id = platform["id"]
                platform_name = platform.get("name", platform.get("display_name", "Unknown"))
                offset = 0
                limit = 50

                while True:
                    if self._sync_cancel:
                        self._finish_sync("Sync cancelled")
                        return

                    try:
                        roms = await self.loop.run_in_executor(
                            None,
                            self._romm_request,
                            f"/api/roms?platform_ids={platform_id}&limit={limit}&offset={offset}",
                        )
                    except Exception as e:
                        decky.logger.error(
                            f"Failed to fetch ROMs for platform {platform_name}: {e}"
                        )
                        break

                    # API returns paginated envelope {"items": [...], "total": N}
                    if isinstance(roms, dict):
                        rom_list = roms.get("items", [])
                    else:
                        rom_list = roms

                    for rom in rom_list:
                        rom["platform_name"] = platform_name

                    all_roms.extend(rom_list)
                    self._sync_progress["current"] = len(all_roms)
                    self._sync_progress["message"] = (
                        f"Fetching ROMs... ({len(all_roms)} found)"
                    )
                    await asyncio.sleep(0)

                    if len(rom_list) < limit:
                        break
                    offset += limit

            if self._sync_cancel:
                self._finish_sync("Sync cancelled")
                return

            decky.logger.info(
                f"Fetched {len(all_roms)} ROMs from {len(platforms)} platforms"
            )

            # Phase 3: Create shortcuts
            self._sync_progress = {
                "running": True,
                "phase": "shortcuts",
                "current": 0,
                "total": len(all_roms),
                "message": "Creating Steam shortcuts...",
            }
            await asyncio.sleep(0)

            try:
                platform_apps = await self.loop.run_in_executor(
                    None, self._create_shortcuts, all_roms
                )
            except Exception as e:
                decky.logger.error(f"Failed to create shortcuts: {e}")
                self._sync_progress = {
                    "running": False,
                    "phase": "error",
                    "current": 0,
                    "total": 0,
                    "message": f"Failed to create shortcuts: {e}",
                }
                self._sync_running = False
                return

            if self._sync_cancel:
                self._finish_sync("Sync cancelled")
                return

            # Phase 4: Download artwork
            self._sync_progress = {
                "running": True,
                "phase": "artwork",
                "current": 0,
                "total": len(all_roms),
                "message": "Downloading artwork...",
            }
            await asyncio.sleep(0)

            await self._download_artwork(all_roms)

            if self._sync_cancel:
                self._finish_sync("Sync cancelled")
                return

            # Save state
            self._state["last_sync"] = datetime.now().isoformat()
            self._state["sync_stats"] = {
                "platforms": len(platforms),
                "roms": len(all_roms),
            }
            self._save_state()

            # Emit completion event
            summary = {
                "platform_app_ids": {
                    name: ids for name, ids in platform_apps.items()
                },
                "total_games": len(all_roms),
            }
            await decky.emit("sync_complete", summary)
            decky.logger.info(f"Sync complete: {len(all_roms)} games across {len(platforms)} platforms")

            self._sync_progress = {
                "running": False,
                "phase": "done",
                "current": len(all_roms),
                "total": len(all_roms),
                "message": f"Sync complete — {len(all_roms)} ROMs from {len(platforms)} platforms",
            }
        except Exception as e:
            import traceback
            decky.logger.error(f"Sync failed: {e}\n{traceback.format_exc()}")
            self._sync_progress = {
                "running": False,
                "phase": "error",
                "current": 0,
                "total": 0,
                "message": f"Sync failed: {e}",
            }
        finally:
            self._sync_running = False

    def _finish_sync(self, message):
        self._sync_progress = {
            "running": False,
            "phase": "cancelled",
            "current": self._sync_progress.get("current", 0),
            "total": self._sync_progress.get("total", 0),
            "message": message,
        }
        self._sync_running = False
        decky.logger.info(message)

    def _create_shortcuts(self, all_roms):
        data = self._read_shortcuts()
        shortcuts = data.get("shortcuts", {})

        # Index existing RomM shortcuts by rom_id
        existing_romm = {}
        for key, entry in shortcuts.items():
            launch_opts = entry.get("LaunchOptions", "")
            if isinstance(launch_opts, str) and launch_opts.startswith("romm:"):
                try:
                    rom_id = int(launch_opts.split(":", 1)[1])
                    existing_romm[rom_id] = key
                except (ValueError, IndexError):
                    pass

        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")
        start_dir = os.path.dirname(exe)
        current_rom_ids = set()
        platform_apps = {}

        # Find the next available numeric key
        if shortcuts:
            next_key = max(int(k) for k in shortcuts) + 1
        else:
            next_key = 0

        for rom in all_roms:
            rom_id = rom["id"]
            current_rom_ids.add(rom_id)
            app_id = self._generate_app_id(exe, rom["name"])
            artwork_id = self._generate_artwork_id(exe, rom["name"])
            platform_name = rom.get(
                "platform_name",
                rom.get("platform_display_name", "Unknown"),
            )

            entry = {
                "appid": app_id,
                "AppName": rom["name"],
                "Exe": f'"{exe}"',
                "StartDir": f'"{start_dir}"',
                "LaunchOptions": f"romm:{rom_id}",
                "icon": "",
                "ShortcutPath": "",
                "IsHidden": 0,
                "AllowDesktopConfig": 1,
                "AllowOverlay": 1,
                "OpenVR": 0,
                "Devkit": 0,
                "DevkitGameID": "",
                "DevkitOverrideAppID": 0,
                "LastPlayTime": 0,
                "tags": {"0": "RomM", "1": platform_name},
            }

            if rom_id in existing_romm:
                shortcuts[existing_romm[rom_id]] = entry
            else:
                shortcuts[str(next_key)] = entry
                next_key += 1

            # Track for platform_apps return and state
            platform_apps.setdefault(platform_name, []).append(app_id)
            self._state["shortcut_registry"][str(rom_id)] = {
                "app_id": app_id,
                "artwork_id": artwork_id,
                "name": rom["name"],
            }

        # Remove stale RomM shortcuts
        for rom_id, key in existing_romm.items():
            if rom_id not in current_rom_ids:
                del shortcuts[key]
                self._state["shortcut_registry"].pop(str(rom_id), None)

        data["shortcuts"] = shortcuts
        self._write_shortcuts(data)
        decky.logger.info(f"Wrote {len(current_rom_ids)} shortcuts")
        return platform_apps

    async def _download_artwork(self, all_roms):
        grid = self._grid_dir()
        if not grid:
            decky.logger.warning("Cannot find grid directory, skipping artwork")
            return

        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")
        total = len(all_roms)

        for i, rom in enumerate(all_roms):
            if self._sync_cancel:
                return

            self._sync_progress["current"] = i + 1
            self._sync_progress["message"] = (
                f"Downloading artwork... ({i + 1}/{total})"
            )
            await asyncio.sleep(0)

            # Determine cover path from ROM data
            cover_path = rom.get("path_cover_large") or rom.get("path_cover_small")
            if not cover_path:
                continue

            artwork_id = self._generate_artwork_id(exe, rom["name"])
            target = os.path.join(grid, f"{artwork_id}p.png")

            if os.path.exists(target):
                continue

            try:
                await self.loop.run_in_executor(
                    None, self._romm_download, cover_path, target
                )
            except Exception as e:
                decky.logger.warning(
                    f"Failed to download artwork for {rom['name']}: {e}"
                )

    async def remove_platform_shortcuts(self, platform_slug):
        """Remove all Steam shortcuts for a specific platform."""
        try:
            # Resolve slug to platform name via API
            platforms = await self.loop.run_in_executor(
                None, self._romm_request, "/api/platforms"
            )
            platform_name = None
            for p in platforms:
                if p.get("slug") == platform_slug:
                    platform_name = p.get("name", "")
                    break
            if not platform_name:
                return {
                    "success": False,
                    "message": f"Platform '{platform_slug}' not found",
                    "removed_count": 0,
                }

            data = self._read_shortcuts()
            shortcuts = data.get("shortcuts", {})
            grid = self._grid_dir()

            keys_to_remove = []
            rom_ids_to_remove = []

            for key, entry in shortcuts.items():
                launch_opts = entry.get("LaunchOptions", "")
                if not (isinstance(launch_opts, str) and launch_opts.startswith("romm:")):
                    continue
                tags = entry.get("tags", {})
                tag_values = tags.values() if isinstance(tags, dict) else []
                if platform_name in tag_values:
                    keys_to_remove.append(key)
                    try:
                        rom_id = launch_opts.split(":", 1)[1]
                        rom_ids_to_remove.append(rom_id)
                    except (IndexError,):
                        pass

            # Remove shortcuts and clean up artwork
            for key in keys_to_remove:
                del shortcuts[key]

            for rom_id in rom_ids_to_remove:
                reg_entry = self._state["shortcut_registry"].pop(rom_id, None)
                if reg_entry and grid:
                    art_path = os.path.join(grid, f"{reg_entry['artwork_id']}p.png")
                    if os.path.exists(art_path):
                        os.remove(art_path)

            data["shortcuts"] = shortcuts
            self._write_shortcuts(data)
            self._save_state()

            count = len(keys_to_remove)
            decky.logger.info(
                f"Removed {count} shortcuts for platform '{platform_name}'"
            )
            return {
                "success": True,
                "message": f"Removed {count} shortcuts for {platform_name}",
                "removed_count": count,
            }
        except Exception as e:
            decky.logger.error(f"Failed to remove platform shortcuts: {e}")
            return {
                "success": False,
                "message": f"Failed to remove shortcuts: {e}",
                "removed_count": 0,
            }

    async def remove_all_shortcuts(self):
        """Remove ALL RomM shortcuts from Steam."""
        try:
            data = self._read_shortcuts()
            shortcuts = data.get("shortcuts", {})
            grid = self._grid_dir()

            keys_to_remove = []
            for key, entry in shortcuts.items():
                launch_opts = entry.get("LaunchOptions", "")
                if isinstance(launch_opts, str) and launch_opts.startswith("romm:"):
                    keys_to_remove.append(key)

            # Delete artwork for all registry entries
            if grid:
                for rom_id, reg_entry in self._state["shortcut_registry"].items():
                    art_path = os.path.join(grid, f"{reg_entry['artwork_id']}p.png")
                    if os.path.exists(art_path):
                        os.remove(art_path)

            # Remove shortcuts
            for key in keys_to_remove:
                del shortcuts[key]

            data["shortcuts"] = shortcuts
            self._write_shortcuts(data)

            # Clear registry
            self._state["shortcut_registry"] = {}
            self._save_state()

            count = len(keys_to_remove)
            decky.logger.info(f"Removed all {count} RomM shortcuts")
            return {
                "success": True,
                "message": f"Removed {count} RomM shortcuts from Steam",
                "removed_count": count,
            }
        except Exception as e:
            decky.logger.error(f"Failed to remove all shortcuts: {e}")
            return {
                "success": False,
                "message": f"Failed to remove shortcuts: {e}",
                "removed_count": 0,
            }

    async def get_sync_stats(self):
        return {
            "last_sync": self._state.get("last_sync"),
            "platforms": self._state.get("sync_stats", {}).get("platforms", 0),
            "roms": self._state.get("sync_stats", {}).get("roms", 0),
            "total_shortcuts": len(self._state.get("shortcut_registry", {})),
        }

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
