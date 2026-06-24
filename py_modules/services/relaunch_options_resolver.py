"""RelaunchOptionsResolver — the single installed+bound relaunch-items seam.

The one place that answers "what is the current Steam ``launch_options`` for
every installed and bound ROM?". Both the RetroDECK-home migration (which
re-bakes each relocated ROM's shortcut to its new path) and the startup
launch-options reconcile (#1043, which heals any drift to the empty
placeholder) draw their relaunch items from this seam, so the two never carry
a divergent build of the same list.

For every ROM that is both installed (has a ``rom_installs`` row) and bound
(its ``Rom.shortcut_app_id`` is set), the resolved item composes the full
Steam-shortcut launch command from the active core and the selected disc
through the shared ``active_core`` / ``disc_resolver`` seams every other bake
site uses. Uninstalled ROMs (no ``rom_installs`` row) and unbound ROMs
(``shortcut_app_id`` is ``None``) are skipped by construction — they carry no
installed launch command to reconcile.

The install/ROM rows are snapshotted inside one short read UoW which is closed
*before* the bake resolution runs: ``active_core_for_rom`` opens its own UoW,
and the per-connection ``BEGIN IMMEDIATE`` write lock is not re-entrant, so
resolving inside the iteration UoW would deadlock until ``busy_timeout`` then
raise ``database is locked`` (#1154). The disc scan is the resolver's I/O seam,
none at this layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.shortcut_data import build_launch_options, resolve_emulator_invocation

if TYPE_CHECKING:
    from domain.rom import Rom
    from domain.rom_install import RomInstall
    from services.protocols import (
        ActiveCoreReader,
        DiscResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class RelaunchOptionsResolverConfig:
    """Frozen wiring bundle handed to ``RelaunchOptionsResolver.__init__``.

    Carries the SQLite Unit-of-Work factory (to snapshot the installed+bound
    ``(rom, install)`` pairs in one short read UoW), the shared ``active_core``
    resolver (which ``.so`` each ROM launches with) and the shared
    ``disc_resolver`` (which file a multi-disc ROM launches given its persisted
    pick) — the same two seams every other launch-bake site resolves through.
    """

    uow_factory: UnitOfWorkFactory
    active_core: ActiveCoreReader
    disc_resolver: DiscResolver


class RelaunchOptionsResolver:
    """Build the relaunch items for every installed+bound ROM."""

    def __init__(self, *, config: RelaunchOptionsResolverConfig) -> None:
        self._uow_factory = config.uow_factory
        self._active_core = config.active_core
        self._disc_resolver = config.disc_resolver

    def _resolve_item(self, rom: Rom, install: RomInstall) -> dict[str, Any]:
        """Compose one ``{app_id, launch_options}`` item for an installed+bound ROM.

        The single resolve body shared by the batch and single-ROM entry points.
        Resolves the ROM's active core and selected disc — through the same
        ``active_core`` / ``disc_resolver`` seams every other launch-bake site
        uses — outside any open Unit of Work (``active_core_for_rom`` opens its
        own, and the per-connection write lock is not re-entrant; #1154). A
        multi-disc ROM bakes its selected disc's path, a single-disc ROM its own
        ``file_path``.
        """
        core_so, _label = self._active_core.active_core_for_rom(rom.rom_id)
        invocation = resolve_emulator_invocation({"id": rom.rom_id}, core_so)
        bake_path = self._disc_resolver.resolve_for_install(install, rom.selected_disc)
        return {
            "app_id": rom.shortcut_app_id,
            "launch_options": build_launch_options(invocation, bake_path),
        }

    def installed_relaunch_items(self) -> list[dict[str, Any]]:
        """Return one ``{app_id, launch_options}`` item per installed+bound ROM.

        Snapshots the installed+bound ``(rom, install)`` pairs in one short read
        UoW, closes it, then resolves each item outside any open UoW.
        Uninstalled or unbound ROMs are skipped by construction.

        The iteration UoW is closed before the resolve loop runs because
        ``active_core_for_rom`` opens its own UoW and the per-connection write
        lock is not re-entrant — resolving inside the iteration UoW deadlocks
        (#1154).
        """
        with self._uow_factory() as uow:
            bound_installs = [
                (rom, install)
                for install in uow.rom_installs.iter_all()
                if (rom := uow.roms.get(install.rom_id)) is not None and rom.shortcut_app_id is not None
            ]

        return [self._resolve_item(rom, install) for rom, install in bound_installs]

    def relaunch_item_for_rom(self, rom_id: int) -> dict[str, Any] | None:
        """Resolve the ``{app_id, launch_options}`` for one installed+bound ROM.

        Returns ``None`` when the ROM has no install row or no bound shortcut —
        there is no installed launch command to re-confirm. Snapshots the
        rom/install in one short read UoW, closes it, then resolves outside —
        same non-reentrant-write-lock reason as the batch path (#1154).

        The Play-button funnel re-confirms the shortcut's launch command from
        this just before launch, healing mid-session ``launch_options`` drift on
        the most common launch path (#1150).
        """
        with self._uow_factory() as uow:
            install = uow.rom_installs.get(rom_id)
            rom = uow.roms.get(rom_id) if install is not None else None
            if install is None or rom is None or rom.shortcut_app_id is None:
                return None
            pair = (rom, install)
        return self._resolve_item(*pair)
