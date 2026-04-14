"""RetroArch config adapter — reads retroarch.cfg for runtime settings.

Exposes only what the plugin currently needs from ``retroarch.cfg``:
the save-sorting flags that drive per-content and per-core save
directory layout. The adapter tries a small list of standard
``retroarch.cfg`` paths (RetroDECK Flatpak, standalone RetroArch
Flatpak, native install) and returns the first match.

No caching today — the cfg is read on each call. RetroDECK's default
call frequency is low (bootstrap + migration detection), so a TTL cache
isn't justified yet. It can be added later if more cfg fields are
needed.
"""

from __future__ import annotations

import logging
import os


class RetroArchConfigAdapter:
    """Adapter for reading RetroArch runtime settings from retroarch.cfg."""

    _RA_CFG = "retroarch.cfg"
    _RETROARCH_CFG_SUFFIXES = (
        os.path.join(".var", "app", "net.retrodeck.retrodeck", "config", "retroarch", _RA_CFG),
        os.path.join(".var", "app", "org.libretro.RetroArch", "config", "retroarch", _RA_CFG),
        os.path.join(".config", "retroarch", _RA_CFG),
    )

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None:
        self._user_home = user_home
        self._logger = logger

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
            except (OSError, UnicodeDecodeError) as exc:
                self._logger.warning(f"Failed to read {cfg_path}: {exc}")
                continue
        return (sort_by_content, sort_by_core)
