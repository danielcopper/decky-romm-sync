"""RetroArch config adapter — reads retroarch.cfg for runtime settings.

Exposes only what the plugin currently needs from ``retroarch.cfg``:
the save-file layout — whether saves go under the RetroDECK saves root
(and with which subdir sorting) or next to the ROM in the content
directory. The adapter tries a small list of standard ``retroarch.cfg``
paths (RetroDECK Flatpak, standalone RetroArch Flatpak, native install)
and returns the first match as a ``SaveLayout`` value object.

No caching today — the cfg is read on each call. RetroDECK's default
call frequency is low (bootstrap + migration detection), so a TTL cache
isn't justified yet. It can be added later if more cfg fields are
needed.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from domain.save_layout import ContentDir, InSaveDir, SaveLayout

if TYPE_CHECKING:
    import logging


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

    def get_save_layout(self) -> SaveLayout:
        """Read the RetroArch save-file layout from retroarch.cfg.

        Returns ``ContentDir()`` when ``savefiles_in_content_dir=true`` —
        saves are written next to the ROM and plugin save sync is
        unsupported. Otherwise returns ``InSaveDir(sort_by_content,
        sort_by_core)`` from the two ``sort_savefiles_*`` flags. Defaults
        to ``InSaveDir(sort_by_content=True, sort_by_core=False)`` matching
        RetroDECK defaults when no readable cfg is found.
        """
        for suffix in self._RETROARCH_CFG_SUFFIXES:
            cfg_path = os.path.join(self._user_home, suffix)
            try:
                in_content_dir = False
                sort_by_content = True  # RetroDECK default
                sort_by_core = False
                with open(cfg_path) as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped.startswith("savefiles_in_content_dir"):
                            val = stripped.split("=", 1)[1].strip().strip('"').lower()
                            in_content_dir = val == "true"
                        elif stripped.startswith("sort_savefiles_by_content_enable"):
                            val = stripped.split("=", 1)[1].strip().strip('"').lower()
                            sort_by_content = val == "true"
                        elif stripped.startswith("sort_savefiles_enable"):
                            val = stripped.split("=", 1)[1].strip().strip('"').lower()
                            sort_by_core = val == "true"
                if in_content_dir:
                    return ContentDir()
                return InSaveDir(sort_by_content=sort_by_content, sort_by_core=sort_by_core)
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError) as exc:
                self._logger.warning(f"Failed to read {cfg_path}: {exc}")
                continue
        return InSaveDir(sort_by_content=True, sort_by_core=False)
