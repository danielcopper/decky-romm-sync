"""Each ROM's launch facts — where its file is, and what runs it.

Answers the two questions :func:`domain.shortcut_data.build_shortcuts_data`
asks about every ROM it bakes into a Steam shortcut: the disc-resolved path of
the installed file (``{rom_id: bake_path}``) and the emulator that should run it
(``{rom_id: EmulatorInvocation}``). Both preview and the per-unit apply resolve
their maps here and hand them straight to the bake. Anything that is not a
launch fact of a single ROM belongs elsewhere: the shortcut's *shape* is the
domain builder's, and what the run does with the built shortcuts is
:class:`~services.library.sync_orchestrator.SyncOrchestrator`'s.

The install-path readers own their own read Unit of Work; the core resolution
opens none, because the injected ``active_core`` seam opens one per ROM.
**Those two must never share a transaction.** A Unit of Work takes SQLite's
non-reentrant ``BEGIN IMMEDIATE``, so resolving a core inside an open UoW
blocks until ``busy_timeout`` and then raises ``database is locked`` — on a
real device only, since ``FakeUnitOfWork`` shares no connection and every test
stays green. Holding all three methods on one class is what makes folding the
path read and the core read into one pass a one-liner, and only half of that
one-liner is gated: ``scripts/check_uow_seam_nesting.py`` fires on the inline
form (naming ``active_emulator_for_rom`` inside the ``with`` block) and is
silent on the peer-call form (``self.do_build_core_overrides(...)`` inside it),
a blind spot its own docstring records. So the reason not to write the fold is
the deadlock, not a check that would stop you — nothing will.

One rule this module does not keep, inherited with the code rather than
introduced by it: both install-path readers hold their UoW open across the disc
resolver's directory listing, once per installed ROM. CONTEXT.md's Unit of Work
entry keeps a transaction narrow — database reads and writes, never file I/O —
so this file is where that fix lands when it comes, and a reader should not
take the boundary above as evidence the rest is clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from domain.shortcut_data import EmulatorInvocation
    from services.protocols import ActiveCoreReader, DiscResolver, UnitOfWorkFactory


@dataclass(frozen=True)
class ShortcutBakeInputsConfig:
    """Frozen wiring bundle handed to ``ShortcutBakeInputs.__init__``.

    Holds the SQLite Unit-of-Work factory the install lookups read through and
    the two per-ROM resolvers the bake's inputs are drawn from: ``active_core``
    folds the per-game ``emulator_override`` and the per-platform
    ``settings.json`` core over the standalone-aware es_systems default, and
    ``disc_resolver`` resolves a multi-disc ROM's persisted ``selected_disc``
    pin against its install directory.
    """

    uow_factory: UnitOfWorkFactory
    active_core: ActiveCoreReader
    disc_resolver: DiscResolver


class ShortcutBakeInputs:
    """The per-ROM launch facts a shortcut bake is built from."""

    def __init__(self, *, config: ShortcutBakeInputsConfig) -> None:
        self._uow_factory = config.uow_factory
        self._active_core = config.active_core
        self._disc_resolver = config.disc_resolver

    def do_build_core_overrides(self, roms: list[dict[str, Any]]) -> dict[int, EmulatorInvocation]:
        """Resolve each ROM's FULL active emulator for the bake.

        Runs every ROM in *roms* through the shared per-ROM ``active_core``
        resolver (the single seam that folds the per-game ``emulator_override``
        and per-platform ``settings.json`` core over the standalone-aware
        es_systems default). Only ROMs that resolve to an emulator (libretro core
        or standalone) appear in the returned ``{rom_id: EmulatorInvocation}``
        map, so :func:`build_shortcuts_data` bakes their ``-e`` form; a ROM that
        resolves to nothing (a genuinely unresolvable platform) is absent and
        falls back to the plain launch. The resolver already warns + degrades on
        a stale label, so no bogus invocation ever reaches the bake.
        """
        resolved: dict[int, EmulatorInvocation] = {}
        for rom in roms:
            emulator = self._active_core.active_emulator_for_rom(rom["id"])
            if emulator is not None:
                resolved[rom["id"]] = emulator
        return resolved

    def do_scan_installed_paths(self) -> dict[int, str]:
        """Read ``{rom_id: bake_path}`` for the whole installed library in one scan.

        Used by the preview path, which already operates over every ROM in the
        library — a single ``iter_all()`` is the cheapest way to cover them all.
        Each path is the disc-resolved launch path: a multi-disc ROM resolves its
        persisted ``selected_disc`` pin against its install directory (a
        single-disc ROM resolves to its own ``file_path``, unchanged), or ``""``
        when the install has no launch target. Only ROMs with a current install
        record appear in the map; a ROM not downloaded is absent, and both cases
        reach :func:`build_shortcuts_data` as the same empty launch command.
        """
        with self._uow_factory() as uow:
            paths: dict[int, str] = {}
            for install in uow.rom_installs.iter_all():
                rom = uow.roms.get(install.rom_id)
                selected_disc = rom.selected_disc if rom is not None else None
                paths[install.rom_id] = self._disc_resolver.resolve_for_install(install, selected_disc)
            return paths

    def do_read_installed_paths(self, rom_ids: set[int]) -> dict[int, str]:
        """Read ``{rom_id: bake_path}`` for *rom_ids* via targeted point-lookups.

        Used by the per-unit apply path: scanning the whole ``rom_installs``
        table once per unit is O(units * all-installs) (#797), so this resolves
        only the unit's ROMs via ``get(rom_id)``. Each path is the disc-resolved
        launch path — a multi-disc ROM resolves its persisted ``selected_disc``
        pin against its install directory (a single-disc ROM resolves to its own
        ``file_path``, unchanged), or ``""`` when the install has no launch
        target. A ROM with no install record is absent; both cases reach
        :func:`build_shortcuts_data` as the same empty launch command.
        """
        with self._uow_factory() as uow:
            paths: dict[int, str] = {}
            for rom_id in rom_ids:
                install = uow.rom_installs.get(rom_id)
                if install is not None:
                    rom = uow.roms.get(rom_id)
                    selected_disc = rom.selected_disc if rom is not None else None
                    paths[rom_id] = self._disc_resolver.resolve_for_install(install, selected_disc)
            return paths
