"""Per-ROM save path resolution and local save-file discovery.

Resolves the on-disk save directory for an installed ROM (honouring
RetroArch's ``sort_savefiles_*`` settings and the optional per-core
subdirectory) and enumerates the matching local save files. Pure
filesystem + path-algebra responsibility — no RomM I/O, no state
mutation. Shared by SlotsService, SyncEngine, and StatusService; reads
about whether a save-sort migration is pending live here too because
they share ``_get_rom_save_info``'s decision to honour the previous
layout while a migration is in flight.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.save_extensions import get_save_extensions
from domain.save_path import resolve_save_dir

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        CoreNameProviderFn,
        CoreResolverFn,
        RetroDeckPaths,
        SaveFileAdapter,
    )


@dataclass(frozen=True)
class RomInfoServiceConfig:
    """Frozen wiring bundle handed to ``RomInfoService.__init__``.

    Holds the main plugin state dict (for ``installed_roms`` reads and
    save-sort state), the Protocol-typed filesystem adapter, the
    RetroDECK runtime-path accessor, the ES-DE core resolver, the
    optional RetroArch core-name provider, and the standard-library
    logger.
    """

    state: dict
    save_file: SaveFileAdapter
    retrodeck_paths: RetroDeckPaths
    get_active_core: CoreResolverFn
    get_core_name: CoreNameProviderFn | None
    logger: logging.Logger


class RomInfoService:
    """Resolves per-ROM save paths and discovers local save files on disk."""

    def __init__(self, *, config: RomInfoServiceConfig) -> None:
        self._config = config
        self._state = config.state
        self._save_file = config.save_file
        self._retrodeck_paths = config.retrodeck_paths
        self._get_active_core = config.get_active_core
        self._get_core_name = config.get_core_name
        self._logger = config.logger

    def get_rom_save_info(self, rom_id: int) -> dict | None:
        """Get save-related info for an installed ROM.

        Returns dict with keys: system, rom_name, saves_dir, platform_slug, file_path
        or None if not installed.
        """
        rom_id_str = str(int(rom_id))
        installed = self._state["installed_roms"].get(rom_id_str)
        if not installed:
            return None
        system = installed.get("system", "")
        file_path = installed.get("file_path", "")
        platform_slug = installed.get("platform_slug", "")
        if not system or not file_path:
            return None
        rom_name = os.path.splitext(os.path.basename(file_path))[0]

        # Use domain save path resolution.
        # Read sort settings from state (populated by MigrationService at startup).
        # When a save-sort migration is pending, prefer the *previous* layout:
        # RetroArch caches its runtime save-path at game-load time, so the
        # session that just ended still wrote to the old directory. Reading
        # the current settings here would point sync at the wrong location
        # and risk downloading stale server content to the new layout (#238).
        saves_base = self._retrodeck_paths.saves_path()
        roms_base = self._retrodeck_paths.roms_path()
        sort_state = self.pending_sort_settings() or self._state.get("save_sort_settings")
        if sort_state:
            sort_by_content = sort_state.get("sort_by_content", True)
            sort_by_core = sort_state.get("sort_by_core", False)
        else:
            sort_by_content, sort_by_core = True, False  # RetroDECK defaults

        # When sort-by-core is active, RetroArch writes per-core subdirs named
        # by the .info ``corename`` field. Resolve it via the dedicated parser.
        # See docs: Config-Source-Parsers wiki page ("one parser per source").
        # Decision: warn-and-fallback (not fail-loud like MigrationService).
        # SaveService is the critical-path sync flow — every game launch
        # depends on it. Fail-loud would take down save sync entirely on any
        # .info hiccup. MigrationService can afford strictness (one-shot),
        # SaveService cannot (continuous). See issue #232 for history.
        core_name: str | None = None
        if sort_by_core:
            rom_filename = os.path.basename(file_path)
            core_name, core_so = self.resolve_retroarch_corename(system, rom_filename)
            if core_name is None:
                self._logger.warning(
                    "SaveService: unable to resolve RetroArch corename for "
                    "%s/%s (core_so=%s) while sort_by_core is enabled. "
                    "Falling back to the parent save directory, which will "
                    "not match what RetroArch reads at runtime. Check that "
                    "the core's .info file is readable under the RetroDECK "
                    "Flatpak cores directory.",
                    system,
                    rom_filename,
                    core_so if core_so else "unresolved",
                )

        saves_dir = resolve_save_dir(
            file_path,
            saves_base,
            system,
            roms_base=roms_base,
            sort_by_content=sort_by_content,
            sort_by_core=sort_by_core,
            core_name=core_name,
        )

        return {
            "system": system,
            "rom_name": rom_name,
            "saves_dir": saves_dir,
            "platform_slug": platform_slug,
            "file_path": file_path,
        }

    def resolve_retroarch_corename(self, system: str, rom_filename: str) -> tuple[str | None, str | None]:
        """Resolve the RetroArch ``corename`` for a system/ROM.

        Asks ES-DE (via ``get_active_core``) **which** core is active for
        this ROM, then asks the RetroArch ``.info`` parser (via
        ``get_core_name``) **what** RetroArch calls that core in its own
        subsystem — which is the authoritative name used for per-core save
        subdirectories when ``sort_savefiles_enable`` is active.

        One parser per source: the ES-DE label (second element of the
        ``get_active_core`` tuple) is NOT a valid substitute for the
        RetroArch corename. See the Config-Source-Parsers wiki page and
        the reference implementation in ``MigrationService``.

        Returns ``(corename, core_so)``. Either element may be ``None``
        when resolution fails at that step: ``core_so`` is ``None`` when
        ES-DE cannot determine the active core, ``corename`` is ``None``
        when ``.info`` parsing returns nothing (or when ``get_core_name``
        is not injected). Returning the tuple — rather than just
        ``corename`` — lets callers include ``core_so`` in diagnostic
        logs so users can identify which ``.info`` file is at fault.
        Callers choose their own fallback strategy (e.g. warn and fall
        back for critical-path SaveService flows; skip and warn for
        one-shot migrations).
        """
        if self._get_core_name is None:
            return (None, None)
        core_so, _label = self._get_active_core(system, rom_filename)
        if not core_so:
            return (None, None)
        corename = self._get_core_name(core_so)
        return (corename or None, core_so)

    def find_save_files(self, rom_id: int) -> list[dict]:
        """Find local save files for a ROM.

        Returns list of ``{"path": str, "filename": str}``.
        """
        info = self.get_rom_save_info(rom_id)
        if not info:
            return []
        rom_name = info["rom_name"]
        saves_dir = info["saves_dir"]
        platform_slug = info["platform_slug"]
        if not self._save_file.is_dir(saves_dir):
            return []
        results = []
        for ext in get_save_extensions(platform_slug):
            save_path = os.path.join(saves_dir, rom_name + ext)
            if self._save_file.is_file(save_path):
                results.append({"path": save_path, "filename": rom_name + ext})
        return results

    def pending_sort_settings(self) -> dict | None:
        """Return previous save-sort settings if a migration is pending, else None.

        Rejects empty dicts to avoid the half-state where ``get_rom_save_info``'s
        ``or`` fallback would treat ``{}`` as "no pending migration" (and read
        current settings) while ``is_save_sort_changed`` would treat the same
        ``{}`` as "pending" (and gate sync). Both call sites must agree on
        what counts as pending — see #238 review finding 3.
        """
        prev = self._state.get("save_sort_settings_previous")
        return prev if prev else None

    def is_save_sort_changed(self) -> bool:
        """Check if a save sort migration is pending (detected by MigrationService)."""
        return self.pending_sort_settings() is not None
