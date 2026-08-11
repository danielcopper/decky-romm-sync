"""RomInstallRecorder — the one writer of an install record and its shortcut bake.

Owns everything between "the ROM's files are in place" and "Steam can launch
them": the launchable verdict, the ``rom_installs`` upsert, the launch command
resolved from the ROM's persisted core and disc pick, and the applied-state memo
the next sync's delta apply reads back.

Both routes to an installed ROM go through here — a completed download and an
adoption of content the plugin did not download — so the row and the launch
command an adoption produces are derived by exactly the rules a download's are
(ADR-0028), not by a second implementation free to drift.

Every method is synchronous and opens its own short write Unit of Work
(ADR-0006); callers on the event loop offload via ``loop.run_in_executor``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.rom_files import is_launchable_target
from domain.rom_install import RomInstall
from domain.shortcut_data import build_launch_options, resolve_emulator_invocation

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from services.protocols import (
        ActiveCoreReader,
        Clock,
        DiscResolver,
        SystemSupportedExtensionsFn,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class RomInstallRecorderConfig:
    """Frozen wiring bundle handed to ``RomInstallRecorder.__init__``.

    The shared ``active_core`` resolver answers which emulator the ROM will
    launch with and ``disc_resolver`` which disc of a multi-disc set, so the
    per-game core override and the persisted disc pick survive
    uninstall → reinstall and are honoured on adoption too. ``system_extensions``
    is the live per-system ES-DE accept-list the launchable verdict reads.
    """

    logger: logging.Logger
    clock: Clock
    uow_factory: UnitOfWorkFactory
    system_extensions: SystemSupportedExtensionsFn
    active_core: ActiveCoreReader
    disc_resolver: DiscResolver


class RomInstallRecorder:
    """Writes the ``rom_installs`` row an install is, and the bake behind it."""

    def __init__(self, *, config: RomInstallRecorderConfig) -> None:
        self._logger = config.logger
        self._clock = config.clock
        self._uow_factory = config.uow_factory
        self._system_extensions = config.system_extensions
        self._active_core = config.active_core
        self._disc_resolver = config.disc_resolver

    def do_record_install(
        self,
        *,
        rom_id: int,
        rom_detail: dict[str, Any],
        file_path: str,
        rom_dir: str | None,
        system: str,
        cleanup: Callable[[], None],
    ) -> tuple[str | None, str | None]:
        """Build the ``RomInstall`` aggregate and persist it in a short write UoW.

        The filesystem work (rename, extraction, launch-file detection) has
        already run outside any transaction; only the upsert is wrapped here
        (ADR-0006). ``rom_dir`` is the ROM's own directory for a multi-file ROM,
        or ``None`` for a single-file ROM (which owns no folder). If the RomM
        data fails the aggregate's invariant (non-positive ``rom_id``), nothing
        is persisted, *cleanup* removes the just-placed artifact, and a failure
        message is returned.

        Returns ``(file_path, None)`` on success or ``(None, error)`` when the
        invariant rejects the data.
        """
        # Recorded, never acted on: an unlaunchable install keeps its files and
        # its row, and only the shortcut's launch command is withheld. Refusing
        # the install instead would delete a package the user's remaining option
        # is to install by hand in the emulator (#1582, #1652).
        launchable = is_launchable_target(file_path, rom_dir, self._system_extensions(system))
        if not launchable:
            self._logger.warning(f"No launch target for rom_id={rom_id}: {system} cannot launch '{file_path}'")
        try:
            install = RomInstall.mark_installed(
                rom_id=int(rom_id),
                file_path=file_path,
                rom_dir=rom_dir,
                platform_slug=rom_detail.get("platform_slug", ""),
                system=system,
                installed_at=self._clock.now().isoformat(),
                launchable=launchable,
            )
        except ValueError as e:
            cleanup()
            return None, f"Invalid install metadata: {e}"

        with self._uow_factory() as uow:
            uow.rom_installs.save(install)
            # Download write-back (#1395): top up the ROM's size from the detail
            # we already fetched, so the game-detail UI shows it without waiting
            # for the next sync. Guarded on truthiness — a missing/zero size must
            # never overwrite a good persisted value.
            size = rom_detail.get("fs_size_bytes")
            if size:
                uow.roms.set_fs_size_bytes(int(rom_id), size)
        return file_path, None

    def do_resolve_launch_bake(self, rom_id: int, rom_detail: dict[str, Any], file_path: str) -> tuple[int | None, str]:
        """Return the ``(shortcut_app_id, launch_options)`` the frontend must write.

        Reads the ROM + its fresh install record in a short read UoW, then
        resolves the ROM's FULL active emulator through the shared ``active_core``
        resolver and the multi-disc launch path through the shared
        ``disc_resolver``. ``app_id`` is ``None`` when the ROM has no Steam
        shortcut yet (not synced) — the frontend no-ops and the next sync writes
        the launch command. The resolver already warns + degrades on a stale
        label/pin, so no bogus invocation or missing-disc path reaches the bake.

        This is the load-bearing site: the per-game core override and the disc
        pin both live on ``roms`` so they survive uninstall → reinstall, and
        every route back to an installed ROM goes through here.
        """
        with self._uow_factory() as uow:
            rom = uow.roms.get(int(rom_id))
            install = uow.rom_installs.get(int(rom_id))
            selected_disc = rom.selected_disc if rom is not None else None
        if rom is None:
            return (None, build_launch_options(resolve_emulator_invocation(rom_detail, None), file_path))
        emulator = self._active_core.active_emulator_for_rom(int(rom_id))
        # The install record was committed just before this read, so it is
        # present in the normal flow; guard for the rare race where it is not and
        # fall back to the raw path (no multi-disc resolution possible).
        bake_path = (
            self._disc_resolver.resolve_for_install(install, selected_disc) if install is not None else file_path
        )
        launch_options = build_launch_options(resolve_emulator_invocation(rom_detail, emulator), bake_path)
        return (rom.shortcut_app_id, launch_options)

    def do_record_applied_launch_options(self, rom_id: int, launch_options: str) -> None:
        """Record *launch_options* as ``rom_id``'s applied shortcut state in a short write UoW.

        The delta-restricted apply reads this back to skip a shortcut that already
        carries the correct launch command (#1383). A no-op when the row is gone.
        """
        with self._uow_factory() as uow:
            rom = uow.roms.get(int(rom_id))
            if rom is None:
                return
            rom.record_applied_launch_options(launch_options)
            uow.roms.set_applied_launch_options(int(rom_id), rom.applied_launch_options)
