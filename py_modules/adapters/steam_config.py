"""Adapter wrapping Steam VDF file access and shortcut ID generation.

Mostly stateless helpers — the only external dependency is the user's
Steam ``userdata`` directory (resolved from ``DECKY_USER_HOME``).
"""

from __future__ import annotations

import contextlib
import logging
import os

import vdf

from lib.errors import SteamGridDirMissingError


class SteamConfigAdapter:
    """Thin wrapper around Steam's on-disk config files."""

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None:
        self._user_home = user_home
        self._logger = logger

    # -- Steam user directory -------------------------------------------------

    def find_steam_user_dir(self) -> str | None:
        """Find the active Steam user's userdata directory."""
        steam_paths = [
            os.path.join(self._user_home, ".local", "share", "Steam", "userdata"),
            os.path.join(self._user_home, ".steam", "steam", "userdata"),
        ]
        for base in steam_paths:
            if os.path.isdir(base):
                users = [d for d in os.listdir(base) if d.isdigit()]
                if len(users) == 1:
                    return os.path.join(base, users[0])
                if len(users) > 1:
                    users.sort(
                        key=lambda u, base=base: os.path.getmtime(os.path.join(base, u)),
                        reverse=True,
                    )
                    return os.path.join(base, users[0])
        return None

    def shortcuts_vdf_path(self) -> str | None:
        user_dir = self.find_steam_user_dir()
        if not user_dir:
            return None
        return os.path.join(user_dir, "config", "shortcuts.vdf")

    def grid_dir(self) -> str | None:
        user_dir = self.find_steam_user_dir()
        if not user_dir:
            return None
        grid = os.path.join(user_dir, "config", "grid")
        os.makedirs(grid, exist_ok=True)
        return grid

    # -- VDF read/write (deprecated — frontend uses SteamClient API) ----------

    def read_shortcuts(self) -> dict:
        path = self.shortcuts_vdf_path()
        if not path or not os.path.exists(path):
            return {"shortcuts": {}}
        with open(path, "rb") as f:
            return vdf.binary_loads(f.read())

    def write_shortcuts(self, data: dict) -> None:
        path = self.shortcuts_vdf_path()
        if not path:
            raise RuntimeError("Cannot find Steam shortcuts.vdf path")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(vdf.binary_dumps(data))
        os.replace(tmp_path, path)

    def write_shortcut_icon(self, app_id: int, icon_bytes: bytes) -> str:
        """Write an icon PNG into Steam's grid dir and return its path.

        Uses a temp file + ``os.replace`` for atomicity; the temp file is
        cleaned up on any failure before the exception propagates.

        Raises ``lib.errors.SteamGridDirMissingError`` when the Steam grid
        directory cannot be located.
        """
        grid_dir = self.grid_dir()
        if not grid_dir:
            raise SteamGridDirMissingError("Cannot find Steam grid directory")
        icon_path = os.path.join(grid_dir, f"{app_id}_icon.png")
        tmp_path = icon_path + ".tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(icon_bytes)
            os.replace(tmp_path, icon_path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            raise
        return icon_path

    # -- Steam Input config ---------------------------------------------------

    def set_steam_input_config(self, app_ids: list, mode: str = "default") -> None:
        """Set UseSteamControllerConfig for given app_ids in localconfig.vdf.

        mode: "default" (remove key / "1"), "force_on" ("2"), "force_off" ("0")
        """
        data, localconfig_path = self._load_localconfig()
        if data is None:
            return

        apps = self._navigate_to_apps_section(data, create=mode != "default")
        if apps is None:
            return

        changed = self._apply_steam_input_mode(apps, app_ids, mode)
        if changed:
            self._write_localconfig(data, localconfig_path, mode, len(app_ids))

    def _load_localconfig(self) -> tuple:
        """Load and parse localconfig.vdf. Returns (data, path) or (None, None)."""
        user_dir = self.find_steam_user_dir()
        if not user_dir:
            self._logger.warning("Cannot find Steam user dir, skipping Steam Input config")
            return None, None

        path = os.path.join(user_dir, "config", "localconfig.vdf")
        if not os.path.exists(path):
            self._logger.warning(f"localconfig.vdf not found at {path}")
            return None, None

        try:
            with open(path, encoding="utf-8") as f:
                return vdf.load(f), path
        except Exception as e:
            self._logger.error(f"Failed to parse localconfig.vdf: {e}")
            return None, None

    def _navigate_to_apps_section(self, data: dict, *, create: bool) -> dict | None:
        """Navigate to UserLocalConfigStore.Apps, optionally creating missing keys."""
        node = data
        for key in ("UserLocalConfigStore", "Apps"):
            if key not in node:
                if create:
                    node[key] = {}
                else:
                    return None
            node = node[key]
        return node

    @staticmethod
    def _apply_steam_input_mode(apps: dict, app_ids: list, mode: str) -> bool:
        """Apply or remove UseSteamControllerConfig for each app_id. Returns True if changed."""
        value_map = {"force_on": "2", "force_off": "0"}
        changed = False
        for app_id in app_ids:
            app_key = str(app_id)
            if mode in value_map:
                if app_key not in apps:
                    apps[app_key] = {}
                apps[app_key]["UseSteamControllerConfig"] = value_map[mode]
                changed = True
            elif app_key in apps and "UseSteamControllerConfig" in apps[app_key]:
                del apps[app_key]["UseSteamControllerConfig"]
                if not apps[app_key]:
                    del apps[app_key]
                changed = True
        return changed

    def _write_localconfig(self, data: dict, path: str, mode: str, count: int) -> None:
        """Atomically write localconfig.vdf back to disk."""
        try:
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                vdf.dump(data, f, pretty=True)
            os.replace(tmp_path, path)
            self._logger.info(f"Steam Input mode '{mode}' applied for {count} app(s)")
        except Exception as e:
            self._logger.error(f"Failed to write localconfig.vdf: {e}")

    # -- RetroArch input driver check -----------------------------------------

    def check_retroarch_input_driver(self) -> dict | None:
        """Check if RetroArch input_driver is set to a problematic value."""
        candidates = [
            "~/.var/app/net.retrodeck.retrodeck/config/retroarch/retroarch.cfg",
            "~/.var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg",
            "~/.config/retroarch/retroarch.cfg",
        ]
        for candidate in candidates:
            cfg_path = os.path.expanduser(candidate)
            try:
                with open(cfg_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("input_driver"):
                            parts = line.split("=", 1)
                            if len(parts) == 2:
                                val = parts[1].strip().strip('"').strip("'")
                                return {
                                    "warning": val == "x",
                                    "current": val,
                                    "config_path": cfg_path,
                                }
            except FileNotFoundError:
                continue
        return None

    def fix_retroarch_input_driver(self) -> dict:
        """Change RetroArch input_driver from 'x' to 'sdl2'."""
        check = self.check_retroarch_input_driver()
        if not check or not check.get("warning"):
            return {"success": False, "message": "No fix needed"}
        cfg_path = check["config_path"]
        try:
            with open(cfg_path) as f:
                lines = f.readlines()
            with open(cfg_path, "w") as f:
                for line in lines:
                    if line.strip().startswith("input_driver"):
                        f.write('input_driver = "sdl2"\n')
                    else:
                        f.write(line)
            return {"success": True, "message": "Changed input_driver to sdl2"}
        except Exception as e:
            self._logger.error(f"Failed to fix RetroArch input_driver: {e}")
            return {"success": False, "message": "Operation failed"}
