"""RetroDECK configuration adapter — reads retrodeck.json and retroarch.cfg.

Provides path resolution for saves, ROMs, BIOS, and RetroDECK home directories.
Also reads RetroArch save-sorting settings from retroarch.cfg.
"""

from __future__ import annotations

import json
import logging
import os
import time


class RetroDeckConfigAdapter:
    """Adapter for reading RetroDECK and RetroArch configuration files."""

    _CACHE_TTL = 30  # seconds

    _RA_CFG = "retroarch.cfg"
    _RETROARCH_CFG_SUFFIXES = (
        os.path.join(".var", "app", "net.retrodeck.retrodeck", "config", "retroarch", _RA_CFG),
        os.path.join(".var", "app", "org.libretro.RetroArch", "config", "retroarch", _RA_CFG),
        os.path.join(".config", "retroarch", _RA_CFG),
    )

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None:
        self._user_home = user_home
        self._logger = logger
        self._cached_config: dict | None = None
        self._cache_time = 0.0

    def _config_path(self) -> str:
        return os.path.join(
            self._user_home,
            ".var",
            "app",
            "net.retrodeck.retrodeck",
            "config",
            "retrodeck",
            "retrodeck.json",
        )

    def _load_config(self) -> dict | None:
        now = time.monotonic()
        if self._cached_config is not None and (now - self._cache_time) < self._CACHE_TTL:
            return self._cached_config
        config_path = self._config_path()
        try:
            with open(config_path) as f:
                config = json.load(f)
            self._cached_config = config
            self._cache_time = now
            return config
        except (OSError, json.JSONDecodeError):
            self._cached_config = None
            self._cache_time = now
            return None

    def _get_path(self, key: str, fallback_subdir: str) -> str:
        config = self._load_config()
        if config:
            path = config.get("paths", {}).get(key, "")
            if path:
                return path
        return os.path.join(self._user_home, "retrodeck", fallback_subdir)

    def get_bios_path(self) -> str:
        return self._get_path("bios_path", "bios")

    def get_roms_path(self) -> str:
        return self._get_path("roms_path", "roms")

    def get_saves_path(self) -> str:
        return self._get_path("saves_path", "saves")

    def get_retrodeck_home(self) -> str:
        return self._get_path("rd_home_path", "")

    def get_retroarch_save_sorting(self) -> tuple[bool, bool]:
        """Read save file sorting settings from retroarch.cfg.

        Returns (sort_by_content, sort_by_core) booleans.
        Defaults to (True, False) matching RetroDECK defaults.
        """
        sort_by_content = True  # RetroDECK default
        sort_by_core = False
        for suffix in self._RETROARCH_CFG_SUFFIXES:
            cfg_path = os.path.join(self._user_home, suffix)
            try:
                with open(cfg_path) as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped.startswith("sort_savefiles_by_content_enable"):
                            val = stripped.split("=", 1)[1].strip().strip('"').lower()
                            sort_by_content = val == "true"
                        elif stripped.startswith("sort_savefiles_enable"):
                            val = stripped.split("=", 1)[1].strip().strip('"').lower()
                            sort_by_core = val == "true"
                return (sort_by_content, sort_by_core)
            except FileNotFoundError:
                continue
        return (sort_by_content, sort_by_core)
