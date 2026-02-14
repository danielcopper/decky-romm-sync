import os
import sys
import json
import struct
import binascii
import asyncio
import base64
import shutil
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile
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
        self._pending_sync = {}
        self._download_tasks = {}   # rom_id -> asyncio.Task
        self._download_queue = {}   # rom_id -> DownloadItem dict
        self._download_in_progress = set()  # rom_ids currently being processed
        self._load_state()
        self._prune_stale_state()
        self.loop.create_task(self._poll_download_requests())
        decky.logger.info("RomM Library plugin loaded")

    async def _unload(self):
        if self._sync_running:
            self._sync_cancel = True
        # Cancel all active downloads
        for rom_id, task in list(self._download_tasks.items()):
            task.cancel()
        self._download_tasks.clear()
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

    def _prune_stale_state(self):
        """Remove installed_roms entries whose files no longer exist on disk."""
        pruned = []
        for rom_id, entry in list(self._state["installed_roms"].items()):
            file_path = entry.get("file_path", "")
            rom_dir = entry.get("rom_dir", "")
            # Keep if either the file or the rom_dir still exists
            if (file_path and os.path.exists(file_path)) or (rom_dir and os.path.exists(rom_dir)):
                continue
            decky.logger.info(f"Pruned stale installed_roms entry: {rom_id} ({file_path})")
            pruned.append(rom_id)
        for rom_id in pruned:
            del self._state["installed_roms"][rom_id]
        if pruned:
            self._save_state()

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

    # Deprecated: frontend now gets app_id from SteamClient.Apps.AddShortcut()
    def _generate_app_id(self, exe, appname):
        """Generate Steam shortcut app ID (signed int32). Deprecated."""
        key = exe + appname
        crc = binascii.crc32(key.encode("utf-8")) & 0xFFFFFFFF
        return struct.unpack("i", struct.pack("I", crc | 0x80000000))[0]

    def _generate_artwork_id(self, exe, appname):
        """Generate unsigned artwork ID for grid filenames."""
        key = exe + appname
        crc = binascii.crc32(key.encode("utf-8")) & 0xFFFFFFFF
        return crc | 0x80000000

    # Deprecated: VDF read/write replaced by frontend SteamClient API
    def _read_shortcuts(self):
        path = self._shortcuts_vdf_path()
        if not path or not os.path.exists(path):
            return {"shortcuts": {}}
        with open(path, "rb") as f:
            return vdf.binary_loads(f.read())

    # Deprecated: VDF read/write replaced by frontend SteamClient API
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
            total = int(total) if total else 0
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
        if total > 0 and downloaded != total:
            raise IOError(f"Download incomplete: got {downloaded} bytes, expected {total}")

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

    async def _emit_progress(self, phase, current=0, total=0, message="", running=True):
        """Update _sync_progress and emit sync_progress event to frontend."""
        self._sync_progress = {
            "running": running,
            "phase": phase,
            "current": current,
            "total": total,
            "message": message,
        }
        await decky.emit("sync_progress", self._sync_progress)

    async def _do_sync(self):
        try:
            # Phase 1: Fetch platforms
            await self._emit_progress("platforms", message="Fetching platforms...")

            try:
                platforms = await self.loop.run_in_executor(
                    None, self._romm_request, "/api/platforms"
                )
            except Exception as e:
                decky.logger.error(f"Failed to fetch platforms: {e}")
                await self._emit_progress("error", message=f"Failed to fetch platforms: {e}", running=False)
                self._sync_running = False
                return

            if self._sync_cancel:
                await self._finish_sync("Sync cancelled")
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
            await self._emit_progress("roms", message="Fetching ROMs...")

            all_roms = []
            for platform in platforms:
                if self._sync_cancel:
                    await self._finish_sync("Sync cancelled")
                    return

                platform_id = platform["id"]
                platform_name = platform.get("name", platform.get("display_name", "Unknown"))
                offset = 0
                limit = 50

                while True:
                    if self._sync_cancel:
                        await self._finish_sync("Sync cancelled")
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
                        rom["platform_slug"] = platform.get("slug", "")

                    all_roms.extend(rom_list)
                    await self._emit_progress("roms", current=len(all_roms), message=f"Fetching ROMs... ({len(all_roms)} found)")

                    if len(rom_list) < limit:
                        break
                    offset += limit

            if self._sync_cancel:
                await self._finish_sync("Sync cancelled")
                return

            decky.logger.info(
                f"Fetched {len(all_roms)} ROMs from {len(platforms)} platforms"
            )

            # Phase 3: Prepare shortcut data
            await self._emit_progress("shortcuts", total=len(all_roms), message="Preparing shortcut data...")

            exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")
            start_dir = os.path.join(decky.DECKY_PLUGIN_DIR, "bin")

            shortcuts_data = []
            for i, rom in enumerate(all_roms):
                shortcuts_data.append({
                    "rom_id": rom["id"],
                    "name": rom["name"],
                    "exe": exe,
                    "start_dir": start_dir,
                    "launch_options": f"romm:{rom['id']}",
                    "platform_name": rom.get("platform_name", "Unknown"),
                    "platform_slug": rom.get("platform_slug", ""),
                    "cover_path": "",  # Filled after artwork download
                })
                # No need to emit per-item here, this loop is fast

            if self._sync_cancel:
                await self._finish_sync("Sync cancelled")
                return

            # Phase 4: Download artwork
            await self._emit_progress("artwork", total=len(all_roms), message="Downloading artwork...")

            cover_paths = await self._download_artwork(all_roms)

            if self._sync_cancel:
                await self._finish_sync("Sync cancelled")
                return

            # Update shortcuts_data with cover paths (artwork fetched on demand via get_artwork_base64)
            for sd in shortcuts_data:
                sd["cover_path"] = cover_paths.get(sd["rom_id"], "")

            # Determine stale rom_ids by comparing current sync with registry
            current_rom_ids = {r["id"] for r in all_roms}
            stale_rom_ids = [
                int(rid) for rid in self._state["shortcut_registry"]
                if int(rid) not in current_rom_ids
            ]

            # Phase 5: Emit sync_apply for frontend to process via SteamClient
            await self._emit_progress("applying", total=len(shortcuts_data), message="Applying shortcuts...")

            # Save sync stats (registry updated by report_sync_results)
            self._state["sync_stats"] = {
                "platforms": len(platforms),
                "roms": len(all_roms),
            }
            self._save_state()

            # Store pending data for report_sync_results to reference
            self._pending_sync = {sd["rom_id"]: sd for sd in shortcuts_data}

            await decky.emit("sync_apply", {
                "shortcuts": shortcuts_data,
                "remove_rom_ids": stale_rom_ids,
            })

            decky.logger.info(
                f"Sync data emitted: {len(shortcuts_data)} shortcuts, "
                f"{len(stale_rom_ids)} stale"
            )

            # sync_complete will be emitted by report_sync_results()
            # Keep running=True until report_sync_results sets it to False,
            # or the finally block resets it as a fallback.
        except Exception as e:
            import traceback
            decky.logger.error(f"Sync failed: {e}\n{traceback.format_exc()}")
            # Can't await in except, so set directly; finally will not override
            self._sync_progress = {
                "running": False,
                "phase": "error",
                "current": 0,
                "total": 0,
                "message": f"Sync failed: {e}",
            }
            # Fire-and-forget emit
            self.loop.create_task(decky.emit("sync_progress", self._sync_progress))
        finally:
            self._sync_running = False
            # If sync completed normally (sync_apply emitted), keep progress.running=True
            # until report_sync_results() clears it. Only set to False as emergency fallback.
            if self._sync_progress.get("phase") == "error":
                pass  # Already handled by except block
            elif self._sync_progress.get("running"):
                # Normal completion — frontend is processing. Set a 60s safety timeout.
                async def _safety_timeout():
                    await asyncio.sleep(60)
                    if self._sync_progress.get("running"):
                        stats = self._state.get("sync_stats", {})
                        await self._emit_progress("done",
                            current=stats.get("roms", 0),
                            total=stats.get("roms", 0),
                            message=f"Sync complete: {stats.get('roms', 0)} games from {stats.get('platforms', 0)} platforms",
                            running=False)
                self.loop.create_task(_safety_timeout())

    async def _finish_sync(self, message):
        self._sync_progress = {
            "running": False,
            "phase": "cancelled",
            "current": self._sync_progress.get("current", 0),
            "total": self._sync_progress.get("total", 0),
            "message": message,
        }
        await decky.emit("sync_progress", self._sync_progress)
        self._sync_running = False
        decky.logger.info(message)

    async def report_sync_results(self, rom_id_to_app_id, removed_rom_ids):
        """Called by frontend after applying shortcuts via SteamClient."""
        grid = self._grid_dir()

        # Update registry with new mappings from frontend
        for rom_id_str, app_id in rom_id_to_app_id.items():
            pending = self._pending_sync.get(int(rom_id_str), {})
            cover_path = pending.get("cover_path", "")

            # Rename staged artwork to final Steam app_id filename
            if grid and cover_path:
                final_path = os.path.join(grid, f"{app_id}p.png")
                if cover_path != final_path and os.path.exists(cover_path):
                    try:
                        os.replace(cover_path, final_path)
                        cover_path = final_path
                    except OSError as e:
                        decky.logger.warning(
                            f"Failed to rename artwork for rom {rom_id_str}: {e}"
                        )
                elif os.path.exists(final_path):
                    cover_path = final_path

            self._state["shortcut_registry"][rom_id_str] = {
                "app_id": app_id,
                "name": pending.get("name", ""),
                "platform_name": pending.get("platform_name", ""),
                "platform_slug": pending.get("platform_slug", ""),
                "cover_path": cover_path,
            }

        # Remove stale entries
        for rom_id in removed_rom_ids:
            self._state["shortcut_registry"].pop(str(rom_id), None)

        # Update timestamp and save
        self._state["last_sync"] = datetime.now().isoformat()
        self._save_state()
        self._pending_sync = {}

        # Rebuild platform_app_ids from registry
        platform_app_ids = {}
        for entry in self._state["shortcut_registry"].values():
            pname = entry.get("platform_name", "Unknown")
            platform_app_ids.setdefault(pname, []).append(entry.get("app_id"))

        total = len(self._state["shortcut_registry"])
        await decky.emit("sync_complete", {
            "platform_app_ids": platform_app_ids,
            "total_games": total,
        })

        await self._emit_progress("done", current=total, total=total,
            message=f"Sync complete: {total} games from {len(platform_app_ids)} platforms",
            running=False)
        decky.logger.info(f"Sync results reported: {total} games")
        return {"success": True}

    # Deprecated: VDF-based shortcut creation (replaced by frontend SteamClient API)
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
        """Download cover artwork to staging filenames (romm_{rom_id}_cover.png).

        Decouples download from the final Steam app_id, which isn't known until
        after AddShortcut. report_sync_results() renames to {app_id}p.png.
        Returns dict of rom_id -> local cover path.
        """
        cover_paths = {}
        grid = self._grid_dir()
        if not grid:
            decky.logger.warning("Cannot find grid directory, skipping artwork")
            return cover_paths

        total = len(all_roms)

        for i, rom in enumerate(all_roms):
            if self._sync_cancel:
                return cover_paths

            await self._emit_progress("artwork", current=i + 1, total=total, message=f"Downloading artwork... ({i + 1}/{total})")

            # Determine cover URL from ROM data
            cover_url = rom.get("path_cover_large") or rom.get("path_cover_small")
            if not cover_url:
                continue

            rom_id = rom["id"]
            staging = os.path.join(grid, f"romm_{rom_id}_cover.png")

            # If already synced and final artwork exists, skip download
            reg = self._state["shortcut_registry"].get(str(rom_id))
            if reg and reg.get("app_id"):
                final = os.path.join(grid, f"{reg['app_id']}p.png")
                if os.path.exists(final):
                    cover_paths[rom_id] = final
                    continue

            # If staging file already exists (e.g. retry), skip download
            if os.path.exists(staging):
                cover_paths[rom_id] = staging
                continue

            try:
                await self.loop.run_in_executor(
                    None, self._romm_download, cover_url, staging
                )
                cover_paths[rom_id] = staging
            except Exception as e:
                decky.logger.warning(
                    f"Failed to download artwork for {rom['name']}: {e}"
                )

        return cover_paths

    async def get_registry_platforms(self):
        """Return platforms from the shortcut registry (works offline, no RomM API call)."""
        platforms = {}
        for rom_id, entry in self._state["shortcut_registry"].items():
            pname = entry.get("platform_name", "Unknown")
            slug = entry.get("platform_slug", "")
            platforms.setdefault(pname, {"count": 0, "slug": slug})
            platforms[pname]["count"] += 1
        return {"platforms": [{"name": k, "slug": v["slug"], "count": v["count"]} for k, v in sorted(platforms.items())]}

    async def remove_platform_shortcuts(self, platform_slug):
        """Return app_ids and rom_ids for a platform for the frontend to remove via SteamClient."""
        try:
            # Try registry first (works offline)
            platform_name = None
            for entry in self._state["shortcut_registry"].values():
                if entry.get("platform_slug") == platform_slug:
                    platform_name = entry.get("platform_name")
                    break

            # Fall back to API if slug not in registry
            if not platform_name:
                platforms = await self.loop.run_in_executor(
                    None, self._romm_request, "/api/platforms"
                )
                for p in platforms:
                    if p.get("slug") == platform_slug:
                        platform_name = p.get("name", "")
                        break

            if not platform_name:
                return {
                    "success": False,
                    "message": f"Platform '{platform_slug}' not found",
                    "app_ids": [],
                    "rom_ids": [],
                }

            app_ids = []
            rom_ids = []
            for rom_id, entry in self._state["shortcut_registry"].items():
                if entry.get("platform_name") == platform_name:
                    if "app_id" in entry:
                        app_ids.append(entry["app_id"])
                    rom_ids.append(rom_id)

            return {
                "success": True,
                "app_ids": app_ids,
                "rom_ids": rom_ids,
                "platform_name": platform_name,
            }
        except Exception as e:
            decky.logger.error(f"Failed to get platform shortcuts: {e}")
            return {
                "success": False,
                "message": f"Failed: {e}",
                "app_ids": [],
                "rom_ids": [],
            }

    async def remove_all_shortcuts(self):
        """Return app_ids and rom_ids for the frontend to remove via SteamClient."""
        registry = self._state.get("shortcut_registry", {})
        app_ids = [entry["app_id"] for entry in registry.values() if "app_id" in entry]
        rom_ids = list(registry.keys())
        return {"success": True, "app_ids": app_ids, "rom_ids": rom_ids}

    async def report_removal_results(self, removed_rom_ids):
        """Called by frontend after removing shortcuts via SteamClient."""
        grid = self._grid_dir()
        for rom_id in removed_rom_ids:
            entry = self._state["shortcut_registry"].pop(str(rom_id), None)
            if entry and grid:
                removed = False
                # Try cover_path first (stores the final renamed path)
                cover_path = entry.get("cover_path", "")
                if cover_path and os.path.exists(cover_path):
                    os.remove(cover_path)
                    removed = True
                # Try {app_id}p.png (the standard Steam grid filename)
                if not removed and entry.get("app_id"):
                    app_path = os.path.join(grid, f"{entry['app_id']}p.png")
                    if os.path.exists(app_path):
                        os.remove(app_path)
                        removed = True
                # Fallback: legacy artwork_id format
                if not removed:
                    artwork_id = entry.get("artwork_id")
                    if artwork_id:
                        art_path = os.path.join(grid, f"{artwork_id}p.png")
                        if os.path.exists(art_path):
                            os.remove(art_path)
                # Clean up any leftover staging file
                staging = os.path.join(grid, f"romm_{rom_id}_cover.png")
                if os.path.exists(staging):
                    os.remove(staging)

        # Update sync_stats to reflect current registry
        registry = self._state.get("shortcut_registry", {})
        platforms = set(e.get("platform_name", "") for e in registry.values())
        self._state["sync_stats"] = {
            "platforms": len(platforms),
            "roms": len(registry),
        }
        self._save_state()
        return {"success": True, "message": f"Removed {len(removed_rom_ids)} shortcuts"}

    async def get_artwork_base64(self, rom_id):
        """Return base64-encoded cover artwork for a single ROM (callable from frontend)."""
        rom_id = int(rom_id)
        grid = self._grid_dir()
        if not grid:
            return {"base64": None}

        # Check pending sync data first (staging path)
        pending = self._pending_sync.get(rom_id, {})
        cover_path = pending.get("cover_path", "")

        # Fall back to registry
        if not cover_path:
            reg = self._state["shortcut_registry"].get(str(rom_id), {})
            cover_path = reg.get("cover_path", "")

        # Try staging filename as last resort
        if not cover_path:
            staging = os.path.join(grid, f"romm_{rom_id}_cover.png")
            if os.path.exists(staging):
                cover_path = staging

        if cover_path and os.path.exists(cover_path):
            try:
                with open(cover_path, "rb") as f:
                    return {"base64": base64.b64encode(f.read()).decode("ascii")}
            except Exception as e:
                decky.logger.warning(f"Failed to read artwork for rom {rom_id}: {e}")

        return {"base64": None}

    async def get_sync_stats(self):
        registry = self._state.get("shortcut_registry", {})
        platforms = set(e.get("platform_name", "") for e in registry.values())
        return {
            "last_sync": self._state.get("last_sync"),
            "platforms": len(platforms),
            "roms": len(registry),
            "total_shortcuts": len(registry),
        }

    async def _poll_download_requests(self):
        """Poll for download requests from the launcher script."""
        requests_path = os.path.join(
            decky.DECKY_PLUGIN_RUNTIME_DIR, "download_requests.json"
        )
        while True:
            try:
                await asyncio.sleep(2)
                if not os.path.exists(requests_path):
                    continue
                with open(requests_path, "r") as f:
                    requests = json.load(f)
                if not requests:
                    continue
                # Clear the file immediately
                with open(requests_path, "w") as f:
                    json.dump([], f)
                for req in requests:
                    rom_id = req.get("rom_id")
                    if rom_id:
                        await self.start_download(rom_id)
            except asyncio.CancelledError:
                return
            except Exception as e:
                decky.logger.warning(f"Download request poll error: {e}")

    async def start_download(self, rom_id):
        rom_id = int(rom_id)
        if rom_id in self._download_in_progress:
            return {"success": False, "message": "Already downloading"}

        self._download_in_progress.add(rom_id)
        try:
            rom_detail = await self.loop.run_in_executor(
                None, self._romm_request, f"/api/roms/{rom_id}"
            )
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            decky.logger.error(f"Failed to fetch ROM {rom_id}: {e}")
            return {"success": False, "message": f"Failed to fetch ROM details: {e}"}

        platform_slug = rom_detail.get("platform_slug", "")
        platform_fs_slug = rom_detail.get("platform_fs_slug")
        system = self._resolve_system(platform_slug, platform_fs_slug)

        roms_dir = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "roms", system)
        file_name = rom_detail.get("fs_name", f"rom_{rom_id}")
        # Fix 1: Sanitize fs_name to prevent path traversal
        safe_name = os.path.basename(file_name)
        if safe_name != file_name:
            decky.logger.warning(f"Sanitized fs_name from '{file_name}' to '{safe_name}'")
            file_name = safe_name
        file_size = rom_detail.get("fs_size_bytes", 0)

        # Check disk space (need file size + 100MB buffer)
        os.makedirs(roms_dir, exist_ok=True)
        free_space = shutil.disk_usage(roms_dir).free
        required = file_size + (100 * 1024 * 1024)
        if file_size and free_space < required:
            self._download_in_progress.discard(rom_id)
            free_mb = free_space // (1024 * 1024)
            need_mb = required // (1024 * 1024)
            return {"success": False, "message": f"Not enough disk space ({free_mb}MB free, need {need_mb}MB)"}

        target_path = os.path.join(roms_dir, file_name)

        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", platform_slug)

        self._download_queue[rom_id] = {
            "rom_id": rom_id,
            "rom_name": rom_name,
            "platform_name": platform_name,
            "file_name": file_name,
            "status": "downloading",
            "progress": 0,
            "bytes_downloaded": 0,
            "total_bytes": file_size,
        }

        task = self.loop.create_task(
            self._do_download(rom_id, rom_detail, target_path, system)
        )
        self._download_tasks[rom_id] = task
        return {"success": True, "message": "Download started"}

    async def _do_download(self, rom_id, rom_detail, target_path, system):
        file_name = rom_detail.get("fs_name", f"rom_{rom_id}")
        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", rom_detail.get("platform_slug", ""))
        has_multiple = rom_detail.get("has_multiple_files", False)
        last_emit = [0.0]  # mutable container for closure

        def progress_callback(downloaded, total):
            now = time.monotonic()
            if now - last_emit[0] < 0.5 and downloaded < total:
                return
            last_emit[0] = now
            progress = downloaded / total if total else 0
            self._download_queue[rom_id].update({
                "progress": progress,
                "bytes_downloaded": downloaded,
                "total_bytes": total,
            })
            self.loop.call_soon_threadsafe(
                self.loop.create_task,
                decky.emit("download_progress", {
                    "rom_id": rom_id,
                    "rom_name": rom_name,
                    "status": "downloading",
                    "progress": progress,
                    "bytes_downloaded": downloaded,
                    "total_bytes": total,
                })
            )

        try:
            download_path = f"/api/roms/{rom_id}/content/{urllib.parse.quote(file_name, safe='')}"

            if has_multiple:
                # Multi-file ROM: API returns ZIP, download to temp then extract
                tmp_zip = target_path + ".zip.tmp"
                await self.loop.run_in_executor(
                    None, self._romm_download, download_path, tmp_zip, progress_callback
                )
                # Extract to a subdirectory named after the ROM
                rom_dir_name = os.path.splitext(file_name)[0]
                extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
                os.makedirs(extract_dir, exist_ok=True)
                # Fix 4: Validate extract_dir is within roms_dir
                roms_base = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "roms")
                if not os.path.realpath(extract_dir).startswith(os.path.realpath(roms_base) + os.sep):
                    raise ValueError(f"Extract directory would be outside roms directory: {extract_dir}")
                with zipfile.ZipFile(tmp_zip, "r") as zf:
                    # Fix 3: ZIP slip protection
                    real_extract = os.path.realpath(extract_dir)
                    for member in zf.namelist():
                        member_path = os.path.realpath(os.path.join(extract_dir, member))
                        if not member_path.startswith(real_extract + os.sep) and member_path != real_extract:
                            raise ValueError(f"ZIP member {member} would extract outside target directory")
                    zf.extractall(extract_dir)
                os.remove(tmp_zip)
                # Fix URL-encoded filenames from RomM (e.g. %20 -> space)
                for root, dirs, files in os.walk(extract_dir, topdown=False):
                    for fname in files:
                        decoded = urllib.parse.unquote(fname)
                        if decoded != fname:
                            old_path = os.path.join(root, fname)
                            new_path = os.path.join(root, decoded)
                            os.replace(old_path, new_path)
                            decky.logger.info(f"Renamed URL-encoded file: {fname} -> {decoded}")
                    for dname in dirs:
                        decoded = urllib.parse.unquote(dname)
                        if decoded != dname:
                            old_path = os.path.join(root, dname)
                            new_path = os.path.join(root, decoded)
                            os.replace(old_path, new_path)
                            decky.logger.info(f"Renamed URL-encoded dir: {dname} -> {decoded}")
                # Auto-generate M3U if missing and multiple disc files exist
                self._maybe_generate_m3u(extract_dir, rom_detail)
                # Detect launch file: prefer M3U > CUE > largest file
                launch_file = self._detect_launch_file(extract_dir)
                final_path = launch_file
            else:
                tmp_path = target_path + ".tmp"
                await self.loop.run_in_executor(
                    None, self._romm_download, download_path, tmp_path, progress_callback
                )
                os.replace(tmp_path, target_path)
                final_path = target_path

            # Register as installed
            installed_entry = {
                "rom_id": rom_id,
                "file_name": file_name,
                "file_path": final_path,
                "system": system,
                "platform_slug": rom_detail.get("platform_slug", ""),
                "installed_at": datetime.now().isoformat(),
            }
            if has_multiple:
                installed_entry["rom_dir"] = extract_dir
            self._state["installed_roms"][str(rom_id)] = installed_entry
            self._save_state()

            self._download_queue[rom_id]["status"] = "completed"
            self._download_queue[rom_id]["progress"] = 1.0
            await decky.emit("download_complete", {
                "rom_id": rom_id,
                "rom_name": rom_name,
                "platform_name": platform_name,
                "file_path": final_path,
            })
            decky.logger.info(f"Download complete: {rom_name} -> {final_path}")

        except asyncio.CancelledError:
            self._download_queue[rom_id]["status"] = "cancelled"
            self._cleanup_partial_download(target_path, rom_detail.get("has_multiple_files", False), file_name)
            decky.logger.info(f"Download cancelled: {rom_name}")

        except Exception as e:
            self._download_queue[rom_id]["status"] = "failed"
            self._download_queue[rom_id]["error"] = str(e)
            self._cleanup_partial_download(target_path, rom_detail.get("has_multiple_files", False), file_name)
            decky.logger.error(f"Download failed for {rom_name}: {e}")

        finally:
            self._download_tasks.pop(rom_id, None)
            self._download_in_progress.discard(rom_id)

    def _maybe_generate_m3u(self, extract_dir, rom_detail):
        """Auto-generate an M3U playlist if none exists and multiple disc files are found."""
        # Check if an M3U already exists (search recursively)
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower().endswith(".m3u"):
                    return

        # Collect disc files: .cue, .chd, .iso (search recursively)
        disc_files = []
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower().endswith((".cue", ".chd", ".iso")):
                    # Store path relative to extract_dir for M3U entries
                    rel_path = os.path.relpath(os.path.join(root, f), extract_dir)
                    disc_files.append(rel_path)

        if len(disc_files) < 2:
            return

        # Sort naturally (Disc 1, Disc 2, etc.)
        disc_files.sort()

        rom_name = rom_detail.get("fs_name_no_ext", rom_detail.get("name", "playlist"))
        m3u_path = os.path.join(extract_dir, f"{rom_name}.m3u")
        with open(m3u_path, "w") as f:
            f.write("\n".join(disc_files) + "\n")
        decky.logger.info(f"Auto-generated M3U playlist: {m3u_path}")

    def _detect_launch_file(self, extract_dir):
        """Find the best launch file in an extracted multi-file ROM directory."""
        all_files = []
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                all_files.append(os.path.join(root, f))

        # Prefer M3U > CUE > largest file
        for ext in (".m3u", ".cue"):
            matches = [f for f in all_files if f.lower().endswith(ext)]
            if matches:
                return matches[0]

        if all_files:
            return max(all_files, key=os.path.getsize)
        return extract_dir

    def _cleanup_partial_download(self, target_path, has_multiple, file_name):
        """Clean up partial download files."""
        try:
            tmp_zip = target_path + ".zip.tmp"
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            tmp_single = target_path + ".tmp"
            if os.path.exists(tmp_single):
                os.remove(tmp_single)
            if os.path.exists(target_path):
                os.remove(target_path)
            if has_multiple:
                rom_dir_name = os.path.splitext(file_name)[0]
                extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
                if os.path.isdir(extract_dir):
                    shutil.rmtree(extract_dir)
        except Exception as e:
            decky.logger.warning(f"Cleanup error: {e}")

    async def cancel_download(self, rom_id):
        rom_id = int(rom_id)
        task = self._download_tasks.get(rom_id)
        if not task:
            return {"success": False, "message": "No active download for this ROM"}
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return {"success": True, "message": "Download cancelled"}

    async def get_download_queue(self):
        return {"downloads": list(self._download_queue.values())}

    async def get_installed_rom(self, rom_id):
        return self._state["installed_roms"].get(str(int(rom_id)))

    async def get_rom_by_steam_app_id(self, app_id):
        app_id = int(app_id)
        for rom_id, entry in self._state["shortcut_registry"].items():
            if entry.get("app_id") == app_id:
                installed = self._state["installed_roms"].get(rom_id)
                return {
                    "rom_id": int(rom_id),
                    "name": entry.get("name", ""),
                    "platform_name": entry.get("platform_name", ""),
                    "platform_slug": entry.get("platform_slug", ""),
                    "installed": installed,
                }
        return None

    def _is_safe_rom_path(self, path):
        """Check that a path is safely contained within the roms base directory."""
        roms_base = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "roms")
        resolved = os.path.realpath(path)
        real_base = os.path.realpath(roms_base)
        if not resolved.startswith(real_base + os.sep):
            return False
        # Must be at least 2 levels deep (e.g. roms/gb/file.zip, not roms/gb/)
        rel = os.path.relpath(resolved, real_base)
        parts = rel.split(os.sep)
        if len(parts) < 2:
            return False
        return True

    def _delete_rom_files(self, installed):
        """Delete ROM files for an installed entry. Handles both single-file and multi-file ROMs."""
        rom_dir = installed.get("rom_dir", "")
        file_path = installed.get("file_path", "")

        if rom_dir and os.path.isdir(rom_dir):
            if not self._is_safe_rom_path(rom_dir):
                decky.logger.error(f"Refusing to delete path outside roms directory: {rom_dir}")
                return
            shutil.rmtree(rom_dir)
        elif file_path:
            if not self._is_safe_rom_path(file_path):
                decky.logger.error(f"Refusing to delete path outside roms directory: {file_path}")
                return
            if os.path.isdir(file_path):
                shutil.rmtree(file_path)
            elif os.path.exists(file_path):
                os.remove(file_path)

    async def remove_rom(self, rom_id):
        rom_id_str = str(int(rom_id))
        installed = self._state["installed_roms"].get(rom_id_str)
        if not installed:
            return {"success": False, "message": "ROM not installed"}

        try:
            self._delete_rom_files(installed)
        except Exception as e:
            decky.logger.error(f"Failed to delete ROM files: {e}")
            return {"success": False, "message": f"Failed to delete files: {e}"}

        del self._state["installed_roms"][rom_id_str]
        self._download_queue.pop(int(rom_id), None)
        self._save_state()
        return {"success": True, "message": "ROM removed"}

    async def uninstall_all_roms(self):
        count = 0
        errors = []
        successfully_deleted = []
        for rom_id_str, installed in list(self._state["installed_roms"].items()):
            try:
                self._delete_rom_files(installed)
                count += 1
                successfully_deleted.append(rom_id_str)
            except Exception as e:
                errors.append(f"{rom_id_str}: {e}")
                decky.logger.error(f"Failed to delete ROM {rom_id_str}: {e}")

        for rom_id_str in successfully_deleted:
            self._state["installed_roms"].pop(rom_id_str, None)
        self._download_queue.clear()
        self._save_state()
        msg = f"Removed {count} ROMs"
        if errors:
            msg += f" ({len(errors)} errors)"
        return {"success": True, "message": msg, "removed_count": count}
