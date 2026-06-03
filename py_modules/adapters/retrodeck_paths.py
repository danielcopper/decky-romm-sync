"""RetroDECK paths adapter — reads retrodeck.json for path resolution.

Provides path resolution for saves, ROMs, BIOS, and the RetroDECK home
directory. The adapter reads ``retrodeck.json`` (RetroDECK's user-facing
configurator output) once and caches the result for 30 seconds — long
enough to amortize repeated reads during a sync run, short enough to
pick up edits made via the RetroDECK configurator within a single
plugin session.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging


class RetroDeckPathsAdapter:
    """Adapter for reading RetroDECK path configuration from retrodeck.json."""

    _CACHE_TTL = 30  # seconds

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
        except FileNotFoundError:
            # Missing file is the expected fallback path (fresh install,
            # no RetroDECK yet) — don't spam the log on every read.
            self._cached_config = None
            return None
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning(f"Failed to load RetroDECK config at {config_path}: {exc}")
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

    def bios_path(self) -> str:
        return self._get_path("bios_path", "bios")

    def roms_path(self) -> str:
        return self._get_path("roms_path", "roms")

    def saves_path(self) -> str:
        return self._get_path("saves_path", "saves")

    def retrodeck_home(self) -> str:
        return self._get_path("rd_home_path", "")
