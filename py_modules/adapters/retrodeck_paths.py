"""RetroDECK paths adapter — reads retrodeck.json for path resolution.

Provides path resolution for saves, ROMs, BIOS, and the RetroDECK home
directory. The adapter reads ``retrodeck.json`` (RetroDECK's user-facing
configurator output) once and caches the result for 30 seconds — long
enough to amortize repeated reads during a sync run, short enough to
pick up edits made via the RetroDECK configurator within a single
plugin session.

Path getters are best-effort and never raise: a missing, unreadable, or
malformed ``retrodeck.json`` falls back to ``<user_home>/retrodeck/*``.
On an SD-card install that fallback root is wrong, so the silent
fallback is paired with :meth:`RetroDeckPathsAdapter.config_health`,
the loud signal ``main.py`` surfaces to the frontend banner.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from lib.retrodeck_health import RetroDeckConfigHealth

if TYPE_CHECKING:
    import logging


class RetroDeckPathsAdapter:
    """Adapter for reading RetroDECK path configuration from retrodeck.json."""

    _CACHE_TTL = 30  # seconds

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None:
        self._user_home = user_home
        self._logger = logger
        self._cached_config: dict[str, Any] | None = None
        self._cache_time = 0.0
        # Load outcome that distinguishes "no file" (ABSENT, quiet) from
        # "file present but unreadable" (UNREADABLE, loud). The getters
        # only need the dict-or-None; ``config_health`` needs the reason.
        self._last_load_health: RetroDeckConfigHealth = RetroDeckConfigHealth.ABSENT

    def config_path(self) -> str:
        """Absolute path to ``retrodeck.json`` that this adapter probes."""
        return os.path.join(
            self._user_home,
            ".var",
            "app",
            "net.retrodeck.retrodeck",
            "config",
            "retrodeck",
            "retrodeck.json",
        )

    def _load_config(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._cached_config is not None and (now - self._cache_time) < self._CACHE_TTL:
            return self._cached_config
        config_path = self.config_path()
        try:
            with open(config_path) as f:
                config = json.load(f)
            self._cached_config = config
            self._cache_time = now
            self._last_load_health = RetroDeckConfigHealth.OK
            return config
        except FileNotFoundError:
            # Missing file is the expected fallback path (fresh install,
            # no RetroDECK yet) — don't spam the log on every read, and
            # don't cache the time so the next read picks up a created
            # file immediately.
            self._cached_config = None
            self._last_load_health = RetroDeckConfigHealth.ABSENT
            return None
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning(f"Failed to load RetroDECK config at {config_path}: {exc}")
            self._cached_config = None
            self._cache_time = now
            self._last_load_health = RetroDeckConfigHealth.UNREADABLE
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

    def config_health(self) -> RetroDeckConfigHealth:
        """Classify how trustworthy the resolved RetroDECK roots are.

        Reuses the 30-second TTL cache via :meth:`_load_config` — no
        second independent file read within the TTL. Four outcomes:

        - ``ABSENT``: ``retrodeck.json`` not found — the legitimate
          fresh-install case. Wins over ``ROOT_MISSING`` even when the
          ``~/retrodeck`` fallback does not exist on disk, so it stays
          quiet.
        - ``UNREADABLE``: the file exists but could not be read/parsed.
        - ``ROOT_MISSING``: the file read OK but the resolved RetroDECK
          home directory does not exist on disk (e.g. SD card ejected).
        - ``OK``: read OK and the resolved home exists.
        """
        self._load_config()
        # ABSENT wins over the disk probe: ``~/retrodeck`` not existing on
        # a fresh install is expected, not a failure.
        if self._last_load_health in (
            RetroDeckConfigHealth.ABSENT,
            RetroDeckConfigHealth.UNREADABLE,
        ):
            return self._last_load_health
        # Config read OK — probe the resolved home directory on disk.
        if not os.path.isdir(self.retrodeck_home()):
            return RetroDeckConfigHealth.ROOT_MISSING
        return RetroDeckConfigHealth.OK
