"""CoreService — RetroArch core selection and overrides per system/ROM.

Owns the system-wide core override (ES-DE ``<alternativeEmulator>`` via the
gamelist editor) and the per-game emulator override (the ``roms.emulator_override``
pin). Enumerating the cores available for a ROM's platform, toggling the
system-wide default, and pinning/clearing a per-game core all live here; the
cross-service BIOS recheck that follows a system-core write is also scheduled
from this service.

The per-game pin is stored on the ``Rom`` aggregate via the Unit-of-Work — never
on the ES-DE gamelist — and the launch command for an installed+bound ROM is
recomputed from the pinned ``.so`` so the frontend can confirm-set it on the live
Steam shortcut. System reads/writes happen via the injected ``CoreInfoProvider``
and ``GamelistXmlEditor`` adapters; the per-ROM active core comes from the shared
``ActiveCoreReader`` resolver so the menu's active marker never diverges from the
launched core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.shortcut_data import (
    build_launch_options,
    label_to_core_so,
    resolve_emulator_invocation,
)

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.rom import Rom
    from services.protocols import (
        ActiveCoreReader,
        BiosChecker,
        CoreInfoProvider,
        GamelistXmlEditor,
        RetroDeckPaths,
        SystemResolver,
        UnitOfWork,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class CoreServiceConfig:
    """Frozen wiring bundle handed to ``CoreService.__init__``.

    Carries the runtime infrastructure (event loop, logger), the ES-DE
    read/write seams, the platform-slug-to-system resolver, the bundled
    RetroDECK paths provider, the cross-service BIOS checker, the SQLite
    Unit-of-Work factory (to read the ROM + its install and write the pin), and
    the shared per-ROM active-core resolver (the menu's active marker). Bundled
    here so the ctor stays within the S107 parameter budget.
    """

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    core_info: CoreInfoProvider
    gamelist_editor: GamelistXmlEditor
    resolve_system: SystemResolver
    retrodeck_paths: RetroDeckPaths
    bios_checker: BiosChecker
    uow_factory: UnitOfWorkFactory
    active_core: ActiveCoreReader


class CoreService:
    """RetroArch core override reads and writes — system (ES-DE) + per-game (DB)."""

    def __init__(self, *, config: CoreServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._core_info = config.core_info
        self._gamelist_editor = config.gamelist_editor
        self._resolve_system = config.resolve_system
        self._retrodeck_paths = config.retrodeck_paths
        self._bios_checker = config.bios_checker
        self._uow_factory = config.uow_factory
        self._active_core = config.active_core

    async def get_available_cores(self, rom_id: int) -> dict[str, Any]:
        """Return the cores available for ``rom_id``'s platform + the active one.

        The available-cores list is platform-wide (system-level); the active
        selection is the per-ROM resolution from :class:`ActiveCoreResolver`, so
        a pinned ``emulator_override`` surfaces over the system default and the
        menu can highlight the active core (or offer Reset). When ``rom_id`` is
        unknown the cores list is empty and the active core is ``(None, None)``.
        """
        return await self._loop.run_in_executor(None, self._available_cores_io, rom_id)

    def _available_cores_io(self, rom_id: int) -> dict[str, Any]:
        rom = self._read_rom(rom_id)
        if rom is None:
            return {"cores": [], "active_core": None, "active_core_label": None}
        system = self._resolve_system(rom.platform_slug)
        cores = self._core_info.get_available_cores(system)
        active_so, active_label = self._active_core.active_core_for_rom(rom_id)
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

    async def set_game_core(self, rom_id: int, label: str) -> dict[str, Any]:
        """Pin the per-game emulator/core override for ``rom_id`` to *label*.

        The picked LABEL is resolved to its ``.so`` FIRST (against the cores
        available for the ROM's platform). An unresolvable label is a hard
        failure — the canonical ``{"success": False, "reason": ..., "message":
        ...}`` shape is returned and **nothing is written**, so the DB never
        holds a label that no consumer can resolve. On success the pin is
        written via the Unit-of-Work and the response carries the freshly-baked
        ``launch_options`` (the ``-e`` override form) and ``app_id`` for the
        frontend to confirm-set on the live Steam shortcut. When the ROM is not
        installed or not bound to a shortcut there is nothing to update live:
        the pin still lands and ``launch_options``/``app_id`` are ``None`` (the
        override applies on the next download).
        """
        return await self._loop.run_in_executor(None, self._set_game_core_io, rom_id, label)

    def _set_game_core_io(self, rom_id: int, label: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None:
                return {
                    "success": False,
                    "reason": "not_found",
                    "message": f"ROM {rom_id} is not tracked",
                }
            system = self._resolve_system(rom.platform_slug)
            core_so = label_to_core_so(self._core_info.get_available_cores(system), label)
            if core_so is None:
                # B4: hard-fail BEFORE any write — never persist a label no
                # consumer can resolve to a .so.
                return {
                    "success": False,
                    "reason": "core_unavailable",
                    "message": f"Core '{label}' is not available for {rom.platform_slug}",
                }
            # Enforce the aggregate invariant (strip / reject blank) via the
            # verb method, then persist the resulting label through the pin-only
            # write path (never the sync UPSERT).
            rom.pin_emulator_override(label)
            uow.roms.set_emulator_override(rom_id, rom.emulator_override)
            launch_options, app_id = self._launch_options_for(uow, rom, core_so)
        return {"success": True, "launch_options": launch_options, "app_id": app_id}

    async def clear_game_core(self, rom_id: int) -> dict[str, Any]:
        """Clear the per-game override for ``rom_id`` (Follow default / Reset).

        Drops the pin (stores SQL NULL) so the ROM follows the system default,
        then returns the recomputed PLAIN ``launch_options`` (no ``-e``) and
        ``app_id`` for an installed+bound ROM so the frontend confirm-sets the
        default launch on the live shortcut. Clearing is always valid — there is
        no label to resolve. When the ROM is unknown the canonical failure shape
        is returned; when it is uninstalled or unbound the NULL still lands and
        ``launch_options``/``app_id`` are ``None``.
        """
        return await self._loop.run_in_executor(None, self._clear_game_core_io, rom_id)

    def _clear_game_core_io(self, rom_id: int) -> dict[str, Any]:
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None:
                return {
                    "success": False,
                    "reason": "not_found",
                    "message": f"ROM {rom_id} is not tracked",
                }
            rom.clear_emulator_override()
            uow.roms.set_emulator_override(rom_id, rom.emulator_override)
            # No override → plain launch (active_core_so=None → no -e).
            launch_options, app_id = self._launch_options_for(uow, rom, None)
        return {"success": True, "launch_options": launch_options, "app_id": app_id}

    def _launch_options_for(
        self,
        uow: UnitOfWork,
        rom: Rom,
        active_core_so: str | None,
    ) -> tuple[str | None, int | None]:
        """Bake the launch command for *rom* with *active_core_so*, or ``(None, None)``.

        An installed (``RomInstall`` with a ``file_path``) **and** bound
        (``shortcut_app_id`` set) ROM gets the full launch command — the ``-e``
        override form when ``active_core_so`` is set, the plain form when it is
        ``None`` — paired with its Steam ``app_id``. An uninstalled or unbound
        ROM has no live shortcut to update, so both are ``None`` and the stored
        pin/clear applies on the next download/sync.
        """
        app_id = rom.shortcut_app_id
        if app_id is None:
            return (None, None)
        install = uow.rom_installs.get(rom.rom_id)
        if install is None:
            return (None, None)
        invocation = resolve_emulator_invocation({}, active_core_so)
        return (build_launch_options(invocation, install.file_path), app_id)

    def _read_rom(self, rom_id: int) -> Rom | None:
        with self._uow_factory() as uow:
            return uow.roms.get(rom_id)
