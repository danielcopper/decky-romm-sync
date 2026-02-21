import os
import json
import asyncio
import base64
import time
import urllib.parse
from datetime import datetime
from typing import TYPE_CHECKING, Any

import decky

if TYPE_CHECKING:
    from typing import Callable, Optional, Protocol

    class _SyncDeps(Protocol):
        settings: dict
        _state: dict
        _sync_running: bool
        _sync_cancel: bool
        _sync_progress: dict
        _pending_sync: dict
        _metadata_cache: dict
        loop: asyncio.AbstractEventLoop
        def _romm_request(self, path: str) -> Any: ...
        def _romm_download(self, path: str, dest: str, progress_callback: Optional[Callable] = None) -> None: ...
        def _save_settings_to_disk(self) -> None: ...
        def _save_state(self) -> None: ...
        def _save_metadata_cache(self) -> None: ...
        def _log_debug(self, msg: str) -> None: ...
        def _extract_metadata(self, rom: dict) -> dict: ...
        def _grid_dir(self) -> Optional[str]: ...
        def _set_steam_input_config(self, app_ids: list, mode: str = "default") -> None: ...
        def _generate_app_id(self, exe: str, appname: str) -> int: ...
        def _generate_artwork_id(self, exe: str, appname: str) -> int: ...
        def _read_shortcuts(self) -> dict: ...
        def _write_shortcuts(self, data: dict) -> None: ...


class SyncMixin:
    async def get_platforms(self):
        try:
            platforms = await self.loop.run_in_executor(
                None, self._romm_request, "/api/platforms"
            )
        except Exception as e:
            decky.logger.error(f"Failed to fetch platforms: {e}")
            return {"success": False, "message": "Could not connect to RomM server"}

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
            return {"success": False, "message": "Could not connect to RomM server"}

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
                await self._emit_progress("error", message="Could not connect to RomM server", running=False)
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
                    "igdb_id": rom.get("igdb_id"),
                    "sgdb_id": rom.get("sgdb_id"),
                    "cover_path": "",  # Filled after artwork download
                })
                # No need to emit per-item here, this loop is fast

            if self._sync_cancel:
                await self._finish_sync("Sync cancelled")
                return

            # Cache metadata from sync response
            for rom in all_roms:
                rom_id_str = str(rom["id"])
                self._metadata_cache[rom_id_str] = self._extract_metadata(rom)
            self._save_metadata_cache()
            self._log_debug(f"Metadata cached for {len(all_roms)} ROMs")

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
                "message": "Sync failed — could not connect to RomM server",
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

            registry_entry = {
                "app_id": app_id,
                "name": pending.get("name", ""),
                "platform_name": pending.get("platform_name", ""),
                "platform_slug": pending.get("platform_slug", ""),
                "cover_path": cover_path,
            }
            for meta_key in ("igdb_id", "sgdb_id"):
                if pending.get(meta_key):
                    registry_entry[meta_key] = pending[meta_key]
            self._state["shortcut_registry"][rom_id_str] = registry_entry

        # Remove stale entries
        for rom_id in removed_rom_ids:
            self._state["shortcut_registry"].pop(str(rom_id), None)

        # Apply Steam Input mode for new shortcuts
        steam_input_mode = self.settings.get("steam_input_mode", "default")
        if steam_input_mode != "default" and rom_id_to_app_id:
            try:
                self._set_steam_input_config(
                    [int(aid) for aid in rom_id_to_app_id.values()], mode=steam_input_mode
                )
            except Exception as e:
                decky.logger.error(f"Failed to set Steam Input config: {e}")

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

            # sgdb_id is now stored directly from RomM during sync (no SGDB API call needed)

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
        # Clean up Steam Input config for removed shortcuts (always reset to default)
        removed_app_ids = []
        for rom_id in removed_rom_ids:
            entry = self._state["shortcut_registry"].get(str(rom_id))
            if entry and entry.get("app_id"):
                removed_app_ids.append(entry["app_id"])
        if removed_app_ids:
            try:
                self._set_steam_input_config(removed_app_ids, mode="default")
            except Exception as e:
                decky.logger.error(f"Failed to clean up Steam Input config: {e}")

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
