"""CoreService — RetroArch core selection and overrides per system/ROM.

Owns reads and writes of ES-DE's per-system and per-game RetroArch
core overrides. Enumerating the cores available for a platform,
toggling the system-wide default, and pinning a per-game core all
live here; the cross-service BIOS recheck that follows a write is
also scheduled from this service.

XML reads/writes happen via the injected ``GamelistXmlEditor``
and ``CoreInfoProvider`` adapters; the on-executor scheduling and the
cross-service BIOS recheck are this service's concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        BiosChecker,
        CoreInfoProvider,
        GamelistXmlEditor,
        RetroDeckPaths,
        SystemResolver,
    )


@dataclass(frozen=True)
class CoreServiceConfig:
    """Frozen wiring bundle handed to ``CoreService.__init__``.

    Carries the runtime infrastructure (event loop, logger), the
    ES-DE read/write seams, the platform-slug-to-system resolver, the
    bundled RetroDECK paths provider, and the cross-service BIOS
    checker. Bundled here so the ctor stays within the S107 parameter
    budget.
    """

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    core_info: CoreInfoProvider
    gamelist_editor: GamelistXmlEditor
    resolve_system: SystemResolver
    retrodeck_paths: RetroDeckPaths
    bios_checker: BiosChecker


class CoreService:
    """RetroArch core override reads and writes via ES-DE / gamelist.xml."""

    def __init__(self, *, config: CoreServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._core_info = config.core_info
        self._gamelist_editor = config.gamelist_editor
        self._resolve_system = config.resolve_system
        self._retrodeck_paths = config.retrodeck_paths
        self._bios_checker = config.bios_checker

    async def get_available_cores(self, platform_slug: str, rom_filename: str | None = None) -> dict[str, Any]:
        """Return available cores for a platform along with the active selection.

        The available-cores list is platform-wide (system-level). The active
        selection is read per-game when ``rom_filename`` is provided, so a
        ``<altemulator>`` per-game override surfaces instead of the system
        core; ``None`` reads the system-level active core.
        """
        system = self._resolve_system(platform_slug)
        cores = self._core_info.get_available_cores(system)
        active_so, active_label = self._core_info.get_active_core(system, rom_filename=rom_filename)
        return {
            "cores": cores,
            "active_core": active_so,
            "active_core_label": active_label,
        }

    def _set_system_core_io(
        self,
        retrodeck_home: str,
        system: str,
        core_label: str,
    ) -> None:
        self._gamelist_editor.set_system_override(retrodeck_home, system, core_label or None)
        self._core_info.reset_cache()

    async def set_system_core(self, platform_slug: str, core_label: str) -> dict[str, Any]:
        """Set or clear the system-wide core override for a platform.

        Empty ``core_label`` clears the override (reverts to the ES-DE
        default). Returns ``{"success": True, "bios_status": ...}`` on
        success, where ``bios_status`` is the BIOS payload re-checked
        against the newly chosen core. On any failure (missing
        RetroDECK home, XML write error, BIOS recheck error) returns
        ``{"success": False, "message": ...}``.
        """
        retrodeck_home = self._retrodeck_paths.retrodeck_home()
        if not retrodeck_home:
            return {"success": False, "message": "RetroDECK home not found"}
        system = self._resolve_system(platform_slug)
        try:
            await self._loop.run_in_executor(
                None,
                self._set_system_core_io,
                retrodeck_home,
                system,
                core_label,
            )
            bios = await self._bios_checker.check_platform_bios(platform_slug)
            return {"success": True, "bios_status": bios}
        except Exception as e:
            self._logger.error(f"Failed to set system core: {e}")
            return {"success": False, "message": str(e)}

    def _set_game_core_io(
        self,
        retrodeck_home: str,
        system: str,
        rom_path: str,
        core_label: str,
    ) -> None:
        self._gamelist_editor.set_game_override(retrodeck_home, system, rom_path, core_label or None)
        self._core_info.reset_cache()

    async def set_game_core(self, platform_slug: str, rom_path: str, core_label: str) -> dict[str, Any]:
        """Set or clear the per-game core override.

        Empty ``core_label`` clears the per-game override (reverts to
        the platform default). The BIOS recheck is narrowed by the ROM
        filename derived from ``rom_path`` (stripping leading ``./``)
        so the response reflects the per-game core selection. Returns
        the same success/error shape as ``set_system_core``.
        """
        retrodeck_home = self._retrodeck_paths.retrodeck_home()
        if not retrodeck_home:
            return {"success": False, "message": "RetroDECK home not found"}
        system = self._resolve_system(platform_slug)
        try:
            await self._loop.run_in_executor(
                None,
                self._set_game_core_io,
                retrodeck_home,
                system,
                rom_path,
                core_label,
            )
            rom_filename = rom_path.lstrip("./") if rom_path else None
            bios = await self._bios_checker.check_platform_bios(platform_slug, rom_filename=rom_filename)
            return {"success": True, "bios_status": bios}
        except Exception as e:
            self._logger.error(f"Failed to set game core: {e}")
            return {"success": False, "message": str(e)}
