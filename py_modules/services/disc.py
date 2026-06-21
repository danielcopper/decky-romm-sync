"""DiscService — the multi-disc picker's read + write callables.

Owns the two frontend callables behind the disc picker: ``get_disc_selection``
reports whether a ROM is multi-disc and, if so, the launchable discs plus the
current and default targets; ``select_disc`` pins a disc (or clears the pin back
to the default) and returns the freshly-baked launch command for the frontend to
confirm-set on the live Steam shortcut.

The disc pick lands on the ``Rom`` aggregate via the Unit-of-Work (the pin-only
``set_selected_disc`` write path, never the sync UPSERT), mirroring how the
per-game emulator override is written in :class:`CoreService`. Enumeration and
launch-path resolution both go through the shared :class:`DiscLaunchResolver`
seam so the list the picker shows is the list the bake resolves over, and the
baked launch command never diverges from the picker's selection. The picker only
ever applies to a folder-backed (multi-file) install — a single-file ROM owns no
disc folder and reports ``multi_disc: False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.disc_selection import default_descriptor
from domain.shortcut_data import build_launch_options, resolve_emulator_invocation
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.disc_selection import Disc
    from domain.rom_install import RomInstall
    from services.protocols import (
        ActiveCoreReader,
        DiscResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class DiscServiceConfig:
    """Frozen wiring bundle handed to ``DiscService.__init__``.

    Carries the runtime infrastructure (event loop, logger), the SQLite
    Unit-of-Work factory (to read the ROM + its install and write the disc pin),
    the shared per-ROM disc resolver (enumeration + launch-path resolution), and
    the shared per-ROM active-core resolver (so a re-baked launch command keeps
    the ROM's full active core, not a plain launch).
    """

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    uow_factory: UnitOfWorkFactory
    disc_resolver: DiscResolver
    active_core: ActiveCoreReader


class DiscService:
    """Disc-picker reads (``get_disc_selection``) and writes (``select_disc``)."""

    def __init__(self, *, config: DiscServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._uow_factory = config.uow_factory
        self._disc_resolver = config.disc_resolver
        self._active_core = config.active_core

    async def get_disc_selection(self, rom_id: int) -> dict[str, Any]:
        """Report the disc picker's state for ``rom_id``.

        Returns ``{"multi_disc": False}`` when the ROM is unknown, not installed,
        owns no disc folder (single-file install), or enumerates fewer than two
        discs — the frontend renders no picker in any of those cases. For a
        multi-disc ROM returns ``{"multi_disc": True, "discs": [{"filename",
        "label", "index"}, ...], "selected": <roms.selected_disc | None>,
        "default": {"kind", "label", "filename"}}``. ``selected`` is
        down-validated: a stale pin whose file is no longer enumerated reports as
        ``None`` so the badge matches what the bake launches (the bake degrades
        the same stale pin to the default). Read-only over the local filesystem;
        the not-multi-disc answers are the normal "no picker" response, not
        failures.
        """
        return await self._loop.run_in_executor(None, self._get_disc_selection_io, rom_id)

    def _get_disc_selection_io(self, rom_id: int) -> dict[str, Any]:
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            install = uow.rom_installs.get(rom_id)
            if rom is None or install is None or install.rom_dir is None:
                return {"multi_disc": False}
            discs = self._disc_resolver.enumerate_discs(install)
            selected = rom.selected_disc
            file_path = install.file_path
        if len(discs) < 2:
            return {"multi_disc": False}
        # Down-validate the pin: a stale pin (the file is no longer enumerated)
        # surfaces as ``None`` so the UI badge matches what the bake will actually
        # launch (the bake degrades the same stale pin to the default). A live pin
        # is returned verbatim.
        if selected is not None and selected not in {disc.filename for disc in discs}:
            selected = None
        return {
            "multi_disc": True,
            "discs": [{"filename": d.filename, "label": d.label, "index": d.index} for d in discs],
            "selected": selected,
            "default": default_descriptor(file_path, discs),
        }

    async def select_disc(self, rom_id: int, filename: str | None) -> dict[str, Any]:
        """Pin (or clear with ``None``) the disc selection for ``rom_id``.

        ``filename is None`` clears the pin so the ROM follows the default (the
        ``.m3u`` when ``file_path`` is one, else disc 1). A non-``None``
        *filename* must name one of the enumerated discs — an unknown filename is
        a hard ``not_found`` failure and **nothing is written**. The ROM must be a
        multi-disc install: an unknown/uninstalled ROM, a single-file install,
        or a folder with fewer than two discs returns the canonical failure shape
        (``not_installed`` / ``unsupported``) and writes nothing. On success the
        pick is persisted via the pin-only ``set_selected_disc`` write path and
        the response carries the freshly-baked ``launch_options`` (the disc's path
        folded over the ROM's full active core) for the frontend to confirm-set on
        the live Steam shortcut, plus the now-effective ``selected`` value.
        """
        return await self._loop.run_in_executor(None, self._select_disc_io, rom_id, filename)

    def _select_disc_io(self, rom_id: int, filename: str | None) -> dict[str, Any]:
        # The validate + write run inside one UoW; the bake — which calls
        # ``active_core_for_rom`` (it opens its OWN UoW) — runs AFTER this UoW
        # closes, so the two never nest on the same SQLite connection (BEGIN
        # IMMEDIATE would otherwise self-deadlock).
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            install = uow.rom_installs.get(rom_id)
            if rom is None or install is None or install.rom_dir is None:
                return {
                    "success": False,
                    "reason": "not_installed",
                    "message": f"ROM {rom_id} is not installed as a multi-disc ROM",
                }
            discs = self._disc_resolver.enumerate_discs(install)
            if len(discs) < 2:
                return {
                    "success": False,
                    "reason": ErrorCode.UNSUPPORTED.value,
                    "message": f"ROM {rom_id} is not a multi-disc ROM",
                }
            if filename is not None and filename not in {disc.filename for disc in discs}:
                # B4: hard-fail BEFORE any write — never pin a disc no
                # enumeration can resolve to a launchable path.
                return {
                    "success": False,
                    "reason": ErrorCode.NOT_FOUND.value,
                    "message": f"'{filename}' is not a disc of ROM {rom_id}",
                }
            if filename is None:
                rom.clear_selected_disc()
            else:
                rom.pin_selected_disc(filename)
            uow.roms.set_selected_disc(rom_id, rom.selected_disc)
            selected = rom.selected_disc
        launch_options = self._bake_launch_options(rom_id, install, discs, selected)
        return {"success": True, "launch_options": launch_options, "selected": selected}

    def _bake_launch_options(
        self, rom_id: int, install: RomInstall, discs: list[Disc], selected_disc: str | None
    ) -> str:
        """Bake the launch command for the now-selected disc + the ROM's active core.

        Resolves the disc-aware bake path over the already-enumerated *discs*
        (the pin just written, or the default when cleared), then folds it over
        the ROM's FULL active core so a per-game/per-platform core still bakes its
        ``-e`` override form rather than a plain launch. Runs after the write UoW
        has closed: ``active_core_for_rom`` opens its own UoW, so resolving here
        keeps it from nesting on the same SQLite connection.
        """
        bake_path = self._disc_resolver.resolve_bake_path(install, discs, selected_disc)
        core_so, _label = self._active_core.active_core_for_rom(rom_id)
        return build_launch_options(resolve_emulator_invocation({"id": rom_id}, core_so), bake_path)
