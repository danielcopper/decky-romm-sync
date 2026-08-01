"""RelaunchOptionsResolver — the single installed+bound relaunch-items seam.

The one place that answers "what is the current Steam ``launch_options`` for
every installed and bound ROM?". Both the RetroDECK-home migration (which
re-bakes each relocated ROM's shortcut to its new path) and the startup
launch-options reconcile (#1043, which heals any drift to the empty
placeholder) draw their relaunch items from this seam, so the two never carry
a divergent build of the same list.

It is equally the one place that answers the narrower "what path does this ROM
launch?" — the bare launch target inside those options, which the stop-game
match compares a live sandbox instance's command line against. Asking here
rather than deriving it a second way is what keeps that comparison on the same
derivation the shortcut was written with.

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

    def _resolve_bake_path(self, rom: Rom, install: RomInstall) -> str:
        """Resolve the launch target *rom* bakes — the path the emulator receives.

        The one derivation of that path, shared by the launch-options build and
        the bare-path entry point, so a read-path consumer can never compare
        against a path that differs from the one actually launched. A multi-disc
        ROM resolves to its selected disc, a single-disc ROM to its own
        ``file_path``, through the same ``disc_resolver`` seam every other
        launch-bake site uses.
        """
        return self._disc_resolver.resolve_for_install(install, rom.selected_disc)

    def _resolve_item(self, rom: Rom, install: RomInstall) -> dict[str, Any]:
        """Compose one ``{app_id, launch_options}`` item for an installed+bound ROM.

        The single resolve body shared by the batch and single-ROM entry points.
        Resolves the ROM's active core and selected disc — through the same
        ``active_core`` / ``disc_resolver`` seams every other launch-bake site
        uses — outside any open Unit of Work (``active_core_for_rom`` opens its
        own, and the per-connection write lock is not re-entrant; #1154).
        """
        emulator = self._active_core.active_emulator_for_rom(rom.rom_id)
        invocation = resolve_emulator_invocation({"id": rom.rom_id}, emulator)
        return {
            "app_id": rom.shortcut_app_id,
            "launch_options": build_launch_options(invocation, self._resolve_bake_path(rom, install)),
        }

    def _bound_install(self, rom_id: int) -> tuple[Rom, RomInstall] | None:
        """Snapshot one installed+bound ROM's ``(rom, install)`` pair, or None.

        ``None`` when the ROM has no install row or no bound shortcut — there is
        no installed launch command for it. The UoW is closed before the caller
        resolves anything from the pair, because the resolve seams open their own
        and the per-connection write lock is not re-entrant (#1154).
        """
        with self._uow_factory() as uow:
            install = uow.rom_installs.get(rom_id)
            rom = uow.roms.get(rom_id) if install is not None else None
            if install is None or rom is None or rom.shortcut_app_id is None:
                return None
            return (rom, install)

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
        pair = self._bound_install(rom_id)
        return self._resolve_item(*pair) if pair is not None else None

    def launch_path_for_rom(self, rom_id: int) -> str | None:
        """Resolve the launch target of one installed+bound ROM, without the command.

        The bare path :meth:`relaunch_item_for_rom` bakes into its
        ``launch_options`` — same rows, same disc resolution — so a consumer
        comparing it against a live process's command line compares against the
        derivation the shortcut was written with rather than a second opinion.
        It resolves from the CURRENT rows, so a disc switch, version switch or
        reinstall since the launch yields a path that no longer matches what is
        running; Stop Game (the consumer) then refuses rather than signalling a
        tree it could not attribute. ``None`` when the ROM has no install row or
        no bound shortcut: nothing was ever launched from it.
        """
        pair = self._bound_install(rom_id)
        return self._resolve_bake_path(*pair) if pair is not None else None
