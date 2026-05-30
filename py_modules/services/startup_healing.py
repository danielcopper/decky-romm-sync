"""StartupHealingService — startup-time state reconciliation.

Owns the reconciliation steps that run after state is loaded and
adapters are wired: drops persisted ``installed_roms`` entries that no
longer reflect what's on disk, and transitions any ``running``
``SyncRun`` left behind by a crash mid-sync into ``errored``. The
``installed_roms`` prune is skipped when the RetroDECK home is missing
on disk (boot-time SD-card mount race) so legitimate entries on a card
that hasn't finished mounting don't get wiped on the next reload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.state import PluginState

from domain.installed_roms import is_pending_migration_path

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        Clock,
        PathExistsReader,
        RetroDeckPaths,
        StatePersister,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class StartupHealingServiceConfig:
    """Frozen wiring bundle handed to ``StartupHealingService.__init__``.

    Carries the live state dict, the runtime logger, the clock, the
    state persister, the bundled RetroDECK paths provider, the generic
    path-exists probe, and the SQLite Unit-of-Work factory (the
    transactional seam over the ``sync_runs`` repository). Bundled here
    so the ctor stays within the S107 parameter budget and the service
    stays free of raw filesystem I/O.
    """

    state: PluginState
    logger: logging.Logger
    clock: Clock
    state_persister: StatePersister
    retrodeck_paths: RetroDeckPaths
    path_probe: PathExistsReader
    uow_factory: UnitOfWorkFactory


class StartupHealingService:
    """Reconciles persisted ``installed_roms`` against disk and heals orphaned ``SyncRun``s."""

    def __init__(self, *, config: StartupHealingServiceConfig) -> None:
        self._state = config.state
        self._logger = config.logger
        self._clock = config.clock
        self._state_persister = config.state_persister
        self._retrodeck_paths = config.retrodeck_paths
        self._path_probe = config.path_probe
        self._uow_factory = config.uow_factory

    def prune_stale_installed_roms(self) -> None:
        """Remove installed_roms entries whose files no longer exist on disk.

        Skipped when the RetroDECK home is not yet available on disk —
        almost always a boot-time SD-card-mount race; the next plugin
        reload, with the filesystem ready, will run the prune normally.
        Entries living under a pending migration's previous home are
        also preserved because RetroDECK has moved away from that path
        but the user hasn't migrated yet, so the entries must survive
        until they do.
        """
        retrodeck_home = self._retrodeck_paths.retrodeck_home()
        if not retrodeck_home or not self._path_probe.exists(retrodeck_home):
            self._logger.info(
                f"Skipping installed_roms prune: retrodeck home unavailable ({retrodeck_home or 'unset'})"
            )
            return

        pending_home = self._state.get("retrodeck_home_path_previous", "")
        pruned: list[str] = []
        for rom_id, entry in self._state["installed_roms"].items():
            file_path = entry.get("file_path", "")
            rom_dir = entry.get("rom_dir", "")
            if is_pending_migration_path(file_path, rom_dir, pending_home):
                self._logger.info(f"Skipping prune of {rom_id} ({file_path}): pending migration")
                continue
            if (file_path and self._path_probe.exists(file_path)) or (rom_dir and self._path_probe.exists(rom_dir)):
                continue
            self._logger.info(f"Pruned stale installed_roms entry: {rom_id} ({file_path})")
            pruned.append(rom_id)
        for rom_id in pruned:
            del self._state["installed_roms"][rom_id]
        if pruned:
            self._state_persister.save_state()

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
