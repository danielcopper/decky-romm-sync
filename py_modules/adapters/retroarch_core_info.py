"""RetroArch core info adapter — reads per-core .info files.

RetroArch ships a ``<core>.info`` file alongside every ``<core>.so`` in
its cores directory. These files carry the authoritative metadata for
each core: the internal ``corename`` (used for save sub-directories
under ``sort_savefiles_enable``), ``supported_extensions``,
``firmware_count``, ``database``, ``display_name``, and more.

The adapter resolves the file by probing a small list of candidate
directories under the RetroDECK Flatpak install tree (system-wide and
per-user), opens the file, and delegates parsing to
:func:`domain.retroarch_core_info.parse_core_info`. Results (including
``None`` for missing files) are cached per-instance; no TTL — ``.info``
files only change when the Flatpak is updated, which in practice tears
down the plugin process anyway.

Only RetroDECK paths are probed today; standalone RetroArch installs
and other launchers are out of scope for now. See the decisions log in
the ``Config Source Parsers`` wiki page for the rationale.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from domain.retroarch_core_info import parse_core_info

if TYPE_CHECKING:
    import logging


class RetroArchCoreInfoAdapter:
    """Adapter for reading RetroArch per-core .info metadata files."""

    _SYSTEM_CORES_DIR = (
        "/var/lib/flatpak/app/net.retrodeck.retrodeck/current/active"
        "/files/retrodeck/components/retroarch/rd_extras/cores"
    )
    _USER_CORES_SUFFIX = os.path.join(
        ".local",
        "share",
        "flatpak",
        "app",
        "net.retrodeck.retrodeck",
        "current",
        "active",
        "files",
        "retrodeck",
        "components",
        "retroarch",
        "rd_extras",
        "cores",
    )

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None:
        self._user_home = user_home
        self._logger = logger
        self._cache: dict[str, dict[str, str] | None] = {}

    def _candidate_dirs(self) -> list[str]:
        return [
            self._SYSTEM_CORES_DIR,
            os.path.join(self._user_home, self._USER_CORES_SUFFIX),
        ]

    def get_core_info(self, core_so: str) -> dict[str, str] | None:
        """Return the parsed .info dict for the given core, or ``None``.

        ``core_so`` is the full ``.so`` basename **including** the
        ``_libretro`` suffix (e.g. ``"snes9x_libretro"``). The adapter
        looks for ``{core_so}.info`` in each candidate cores directory,
        returns the parsed dict for the first file it finds, and caches
        the result (including ``None`` for "file not found anywhere").
        """
        if core_so in self._cache:
            return self._cache[core_so]

        filename = f"{core_so}.info"
        for candidate_dir in self._candidate_dirs():
            info_path = os.path.join(candidate_dir, filename)
            try:
                with open(info_path, encoding="utf-8") as f:
                    text = f.read()
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError) as exc:
                self._logger.warning(f"Failed to read {info_path}: {exc}")
                continue
            parsed = parse_core_info(text)
            self._cache[core_so] = parsed
            return parsed

        self._cache[core_so] = None
        return None

    def get_corename(self, core_so: str) -> str | None:
        """Return the RetroArch ``corename`` field for the given core.

        Convenience wrapper around :meth:`get_core_info`. Returns
        ``None`` when the ``.info`` file can't be found or when the
        file exists but has no ``corename`` field (or an empty one).
        """
        info = self.get_core_info(core_so)
        if info is None:
            return None
        return info.get("corename") or None
