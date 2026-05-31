"""StartupHealingService — startup-time state reconciliation.

Owns the reconciliation steps that run after state is loaded and
adapters are wired: drops ``rom_installs`` rows that no longer reflect
what's on disk, and transitions any ``running`` ``SyncRun`` left behind
by a crash into ``errored``. The install prune is skipped when the
RetroDECK home is missing on disk (boot-time SD-card mount race) so
legitimate installs on a card that hasn't finished mounting don't get
wiped on the next reload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.installed_roms import is_pending_migration_path

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        Clock,
        PathExistsReader,
        RetroDeckPaths,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class StartupHealingServiceConfig:
    """Frozen wiring bundle handed to ``StartupHealingService.__init__``.

    Carries the runtime logger, the clock, the bundled RetroDECK paths
    provider, the generic path-exists probe, and the SQLite Unit-of-Work
    factory (the transactional seam over the ``rom_installs``, ``sync_runs``,
    and ``kv_config`` repositories — the last holding the pending-migration
    previous home marker). Bundled here so the ctor stays within the S107
    parameter budget and the service stays free of raw filesystem I/O.
    """

    logger: logging.Logger
    clock: Clock
    retrodeck_paths: RetroDeckPaths
    path_probe: PathExistsReader
    uow_factory: UnitOfWorkFactory


class StartupHealingService:
    """Reconciles persisted ``rom_installs`` against disk and heals orphaned ``SyncRun``s."""

    def __init__(self, *, config: StartupHealingServiceConfig) -> None:
        self._logger = config.logger
        self._clock = config.clock
        self._retrodeck_paths = config.retrodeck_paths
        self._path_probe = config.path_probe
        self._uow_factory = config.uow_factory

    def prune_stale_installed_roms(self) -> None:
        """Remove ``rom_installs`` rows whose files no longer exist on disk.

        Skipped when the RetroDECK home is not yet available on disk —
        almost always a boot-time SD-card-mount race; the next plugin
        reload, with the filesystem ready, will run the prune normally.
        Installs living under a pending migration's previous home are
        also preserved because RetroDECK has moved away from that path
        but the user hasn't migrated yet, so the records must survive
        until they do.
        """
        retrodeck_home = self._retrodeck_paths.retrodeck_home()
        if not retrodeck_home or not self._path_probe.exists(retrodeck_home):
            self._logger.info(
                f"Skipping installed_roms prune: retrodeck home unavailable ({retrodeck_home or 'unset'})"
            )
            return

        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
            pending_home = uow.kv_config.get("retrodeck_home_path_previous") or ""
        stale: list[int] = []
        for install in installs:
            file_path = install.file_path
            rom_dir = install.rom_dir
            if is_pending_migration_path(file_path, rom_dir, pending_home):
                self._logger.info(f"Skipping prune of {install.rom_id} ({file_path}): pending migration")
                continue
            if (file_path and self._path_probe.exists(file_path)) or (rom_dir and self._path_probe.exists(rom_dir)):
                continue
            self._logger.info(f"Pruned stale installed_roms entry: {install.rom_id} ({file_path})")
            stale.append(install.rom_id)

        if stale:
            with self._uow_factory() as uow:
                for rom_id in stale:
                    uow.rom_installs.delete(rom_id)

    def reconcile_orphaned_sync_runs(self) -> None:
        """Transition a ``running`` ``SyncRun`` left by a crash into ``errored``.

        A hard crash (process kill, true ``asyncio.CancelledError``) mid-sync
        leaves the run record stuck in ``running`` because no terminal
        transition fired. On the next startup that orphaned run is marked
        ``errored`` in a short write UoW so the sync-run history reflects what
        actually happened rather than an eternally-in-flight sync.
        """
        with self._uow_factory() as uow:
            run = uow.sync_runs.get_running()
            if run is None:
                return
            self._logger.info(f"Healing orphaned sync run {run.id}: marking errored (interrupted by restart)")
            run.mark_errored(at=self._clock.now().isoformat(), error="interrupted by restart")
            uow.sync_runs.save(run)
