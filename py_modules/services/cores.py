"""CoreService — RetroArch core selection and overrides per platform/ROM.

Owns the plugin's two core-selection deviations: the per-platform core (the
``settings.json`` ``platform_cores`` map) and the per-game emulator override (the
``roms.emulator_override`` pin). Enumerating the cores available for a ROM's
platform, toggling the per-platform default (with the fan-out that re-bakes every
affected shortcut), and pinning/clearing a per-game core all live here; the
cross-service BIOS recheck that follows a per-platform core write is also
scheduled from this service.

Neither selection is written to the retired ES-DE gamelist: the per-platform core
lands in ``settings.json`` via the injected ``SettingsPersister`` and the per-game
pin lands on the ``Rom`` aggregate via the Unit-of-Work. The launch command for an
installed+bound ROM is recomputed from the shared ``ActiveCoreReader`` resolver so
the read-path core never diverges from the launched core, and the frontend can
confirm-set the freshly-baked ``launch_options`` on the live Steam shortcut.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.shortcut_data import (
    build_launch_options,
    label_to_core_so,
    resolve_emulator_invocation,
)
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.rom import Rom
    from services.protocols import (
        ActiveCoreReader,
        BiosChecker,
        CoreInfoProvider,
        DiscResolver,
        SettingsPersister,
        SystemResolver,
        UnitOfWork,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class CoreServiceConfig:
    """Frozen wiring bundle handed to ``CoreService.__init__``.

    Carries the runtime infrastructure (event loop, logger), the ES-DE
    core-info read seam, the platform-slug-to-system resolver, the live
    ``settings`` dict + its persister (where the per-platform core lands), the
    cross-service BIOS checker, the SQLite Unit-of-Work factory (to read the ROM
    + its install and write the per-game pin), the shared per-ROM
    active-core resolver (the menu's active marker + the source of every
    re-baked launch command), and the shared per-ROM disc resolver (so a re-baked
    launch command keeps the ROM's pinned disc rather than reverting to disc 1 /
    the m3u). Bundled here so the ctor stays within the S107 parameter budget.
    """

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    settings: dict[str, Any]
    settings_persister: SettingsPersister
    bios_checker: BiosChecker
    uow_factory: UnitOfWorkFactory
    active_core: ActiveCoreReader
    disc_resolver: DiscResolver


class CoreService:
    """RetroArch core override reads and writes — per-platform (settings) + per-game (DB)."""

    def __init__(self, *, config: CoreServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._core_info = config.core_info
        self._resolve_system = config.resolve_system
        self._settings = config.settings
        self._settings_persister = config.settings_persister
        self._bios_checker = config.bios_checker
        self._uow_factory = config.uow_factory
        self._active_core = config.active_core
        self._disc_resolver = config.disc_resolver

    async def get_available_cores(self, rom_id: int) -> dict[str, Any]:
        """Return the cores available for ``rom_id``'s platform + the active one.

        The available-cores list is platform-wide (system-level); the active
        selection is the per-ROM resolution from :class:`ActiveCoreResolver`, so
        a pinned ``emulator_override`` (or per-platform core) surfaces over the
        system default and the menu can highlight the active core (or offer
        Reset). ``platform_core_label`` carries the per-platform override label
        (``settings.json`` ``platform_cores``) so the menu can mark the
        system-level selection distinctly from the active core.
        ``has_game_override`` reports whether a per-game pin is set — the menu
        can't infer this from the active core alone (pinning the same core as
        the per-platform override is indistinguishable), so the flag drives the
        "follow the system" reset item's checkmark. When ``rom_id`` is unknown
        the cores list is empty and the active core is ``(None, None)``.
        """
        return await self._loop.run_in_executor(None, self._available_cores_io, rom_id)

    def _available_cores_io(self, rom_id: int) -> dict[str, Any]:
        rom = self._read_rom(rom_id)
        if rom is None:
            return {
                "cores": [],
                "active_core": None,
                "active_core_label": None,
                "platform_core_label": None,
                "has_game_override": False,
            }
        system = self._resolve_system(rom.platform_slug)
        cores = self._core_info.get_available_cores(system)
        active_so, active_label = self._active_core.active_core_for_rom(rom_id)
        return {
            "cores": cores,
            "active_core": active_so,
            "active_core_label": active_label,
            "platform_core_label": self._settings.get("platform_cores", {}).get(rom.platform_slug),
            "has_game_override": rom.emulator_override is not None,
        }

    def _set_system_core_io(self, platform_slug: str, core_label: str) -> list[dict[str, Any]]:
        """Write the per-platform core selection and re-bake the affected shortcuts.

        Mutates ``settings["platform_cores"]`` — stores *core_label* under
        *platform_slug* when non-empty, pops the slug when blank (revert to the
        es_systems default) — and persists ``settings.json`` through the
        injected persister. The persister holds the same live dict, so the fan-out
        that follows resolves the freshly-written value.

        Returns one ``{"app_id", "launch_options"}`` entry per installed+bound ROM
        on the platform whose active core is the new per-platform selection. ROMs
        with a per-game ``emulator_override`` are skipped (the pin wins over the
        platform default), as are uninstalled or unbound ROMs (no live shortcut to
        rewrite). Each entry's ``launch_options`` is the FULL active core baked by
        the shared resolver — the ``-e`` override form, or the plain launch when
        the resolver yields ``(None, None)`` — over the disc-resolved bake path,
        so a multi-disc ROM keeps its persisted ``selected_disc`` rather than
        reverting to disc 1 / the m3u (a single-disc ROM bakes its ``file_path``
        unchanged).
        """
        if core_label:
            self._settings["platform_cores"][platform_slug] = core_label
        else:
            self._settings["platform_cores"].pop(platform_slug, None)
        self._settings_persister.save_settings()
        self._core_info.reset_cache()

        rebake_items: list[dict[str, Any]] = []
        with self._uow_factory() as uow:
            for rom in uow.roms.iter_by_platform(platform_slug):
                if rom.emulator_override is not None:
                    continue
                if rom.shortcut_app_id is None:
                    continue
                install = uow.rom_installs.get(rom.rom_id)
                if install is None:
                    continue
                core_so, _label = self._active_core.active_core_for_rom(rom.rom_id)
                invocation = resolve_emulator_invocation({}, core_so)
                # Fold the ROM's persisted disc pick over the install so a
                # per-platform core change re-bakes the pinned disc, not disc 1 /
                # the m3u. A single-disc ROM resolves to its own file_path. The
                # rom is already loaded in this UoW, so its selected_disc is read
                # without a nested read.
                bake_path = self._disc_resolver.resolve_for_install(install, rom.selected_disc)
                rebake_items.append(
                    {
                        "app_id": rom.shortcut_app_id,
                        "launch_options": build_launch_options(invocation, bake_path),
                    }
                )
        return rebake_items

    async def set_system_core(self, platform_slug: str, core_label: str) -> dict[str, Any]:
        """Set or clear the per-platform core selection for a platform.

        Empty ``core_label`` clears the selection (reverts to the es_systems
        default). On success the per-platform core is written to ``settings.json``
        and every installed+bound ROM on the platform (minus per-game-overridden
        ROMs) is re-baked: the response carries ``rebake_items`` (a list of
        ``{"app_id", "launch_options"}``) the frontend confirm-sets on the live
        Steam shortcuts, plus ``bios_status`` re-checked against the newly chosen
        core. On any failure (settings write error, fan-out error, BIOS recheck
        error) returns ``{"success": False, "message": ...}``.
        """
        try:
            rebake_items = await self._loop.run_in_executor(
                None,
                self._set_system_core_io,
                platform_slug,
                core_label,
            )
            bios = await self._bios_checker.check_platform_bios(platform_slug)
            return {"success": True, "bios_status": bios, "rebake_items": rebake_items}
        except Exception as e:
            self._logger.error(f"Failed to set system core: {e}")
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": str(e)}

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

        Drops the pin (stores SQL NULL) so the ROM follows the per-platform/system
        default, then returns the recomputed ``launch_options`` and ``app_id`` for
        an installed+bound ROM so the frontend confirm-sets the now-default launch
        on the live shortcut. Because the resolved default may itself be a
        per-platform core, the recomputed command bakes the ROM's FULL active core
        (the ``-e`` override form, or the plain launch when the platform resolves
        to ``(None, None)``) — never an unconditional plain launch. Clearing is
        always valid — there is no label to resolve. When the ROM is unknown the
        canonical failure shape is returned; when it is uninstalled or unbound the
        NULL still lands and ``launch_options``/``app_id`` are ``None``.
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
            # Cleared pin → follow the per-platform/system default. Resolve the
            # ROM's full active core AFTER the NULL lands so a per-platform core
            # still bakes its -e override (not a plain launch).
            core_so, _label = self._active_core.active_core_for_rom(rom_id)
            launch_options, app_id = self._launch_options_for(uow, rom, core_so)
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
        ``None`` — over the disc-resolved bake path (the ROM's persisted
        ``selected_disc`` for a multi-disc ROM, its ``file_path`` unchanged for a
        single-disc ROM), paired with its Steam ``app_id``. An uninstalled or
        unbound ROM has no live shortcut to update, so both are ``None`` and the
        stored pin/clear applies on the next download/sync.
        """
        app_id = rom.shortcut_app_id
        if app_id is None:
            return (None, None)
        install = uow.rom_installs.get(rom.rom_id)
        if install is None:
            return (None, None)
        invocation = resolve_emulator_invocation({}, active_core_so)
        # Fold the ROM's persisted disc pick over the install so a per-game core
        # pin/clear re-bakes the pinned disc, not disc 1 / the m3u. A single-disc
        # ROM resolves to its own file_path. *rom* is already loaded in the open
        # UoW, so its selected_disc is read without a nested read.
        bake_path = self._disc_resolver.resolve_for_install(install, rom.selected_disc)
        return (build_launch_options(invocation, bake_path), app_id)

    def _read_rom(self, rom_id: int) -> Rom | None:
        with self._uow_factory() as uow:
            return uow.roms.get(rom_id)
