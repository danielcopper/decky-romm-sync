"""RetroArch config adapter — reads retroarch.cfg for runtime settings.

Exposes only what the plugin currently needs from ``retroarch.cfg``: where
RetroArch puts a game's save files, and where it puts its savestates — each as
"under the corresponding RetroDECK root, with this subdir sorting" or "next to
the ROM in the content directory". The adapter tries a small list of standard
``retroarch.cfg`` paths (RetroDECK Flatpak, standalone RetroArch Flatpak, native
install) and returns the first match as a ``SaveLayout`` value object.

The two layouts are read **independently** because RetroArch sorts them
independently: on a stock RetroDECK install savefiles are content-sorted while
savestates are not sorted at all, so deriving one from the other puts a rename
in the wrong directory.

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


def _setting(line: str) -> tuple[str, str]:
    """Split one ``retroarch.cfg`` line into ``(key, lowercased unquoted value)``.

    RetroArch writes ``key = "value"`` and reads the quotes back off, so they are
    not part of the value. A line with no ``=`` — a comment, a blank, a truncated
    write — yields two empty strings, which matches no key.
    """
    key, separator, value = line.strip().partition("=")
    if not separator:
        return ("", "")
    return (key.strip(), value.strip().strip('"').lower())


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
        """Read the RetroArch save-**file** layout from retroarch.cfg.

        Returns ``ContentDir()`` when ``savefiles_in_content_dir=true`` —
        saves are written next to the ROM and plugin save sync is
        unsupported. Otherwise returns ``InSaveDir(sort_by_content,
        sort_by_core)`` from the two ``sort_savefiles_*`` flags. Defaults
        to ``InSaveDir(sort_by_content=True, sort_by_core=False)`` matching
        RetroDECK defaults when no readable cfg is found.
        """
        return self._read_layout(
            in_content_key="savefiles_in_content_dir",
            sort_by_content_key="sort_savefiles_by_content_enable",
            sort_by_core_key="sort_savefiles_enable",
            default_sort_by_content=True,
        )

    def get_savestate_layout(self) -> SaveLayout:
        """Read the RetroArch save**state** layout from retroarch.cfg.

        The same three questions as :meth:`get_save_layout`, asked of the
        ``savestate`` keys, because RetroArch answers them separately: a stock
        RetroDECK install has ``sort_savefiles_by_content_enable=true`` beside
        ``sort_savestates_by_content_enable=false``. The default when no readable
        cfg is found therefore differs too — ``InSaveDir(sort_by_content=False,
        sort_by_core=False)``, which is what RetroDECK ships.
        """
        return self._read_layout(
            in_content_key="savestates_in_content_dir",
            sort_by_content_key="sort_savestates_by_content_enable",
            sort_by_core_key="sort_savestates_enable",
            default_sort_by_content=False,
        )

    def _read_layout(
        self,
        *,
        in_content_key: str,
        sort_by_content_key: str,
        sort_by_core_key: str,
        default_sort_by_content: bool,
    ) -> SaveLayout:
        """Resolve one layout from the first readable ``retroarch.cfg``.

        The first cfg that opens decides, even when it states none of the three
        keys: a RetroArch that has never written them is running on its own
        defaults, and falling through to the next candidate path would answer
        from a config this machine does not use.
        """
        for suffix in self._RETROARCH_CFG_SUFFIXES:
            cfg_path = os.path.join(self._user_home, suffix)
            try:
                in_content_dir = False
                sort_by_content = default_sort_by_content
                sort_by_core = False
                with open(cfg_path) as f:
                    for line in f:
                        key, value = _setting(line)
                        if key == in_content_key:
                            in_content_dir = value == "true"
                        elif key == sort_by_content_key:
                            sort_by_content = value == "true"
                        elif key == sort_by_core_key:
                            sort_by_core = value == "true"
                if in_content_dir:
                    return ContentDir()
                return InSaveDir(sort_by_content=sort_by_content, sort_by_core=sort_by_core)
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError) as exc:
                self._logger.warning(f"Failed to read {cfg_path}: {exc}")
                continue
        return InSaveDir(sort_by_content=default_sort_by_content, sort_by_core=False)
