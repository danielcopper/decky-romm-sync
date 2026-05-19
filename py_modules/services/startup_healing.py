"""StartupHealingService — startup-time state reconciliation.

Owns the reconciliation step that runs after state is loaded and
adapters are wired: drops persisted ``installed_roms`` and
``shortcut_registry`` entries that no longer reflect what's on disk.
Skipped when the RetroDECK home is missing on disk (boot-time SD-card
mount race) so legitimate entries on a card that hasn't finished
mounting don't get wiped on the next reload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.registry_patches import RegistryDeletePatch
from models.state import PluginState

from domain.installed_roms import is_pending_migration_path

if TYPE_CHECKING:
    import logging

    from services.protocols import PathExistsReader, RetroDeckPaths, ShortcutRegistryStore, StatePersister


@dataclass(frozen=True)
class StartupHealingServiceConfig:
    """Frozen wiring bundle handed to ``StartupHealingService.__init__``.

    Carries the live state dict, the runtime logger, the state
    persister, the bundled RetroDECK paths provider, and the generic
    path-exists probe. Bundled here so the ctor stays within the S107
    parameter budget and the service stays free of raw filesystem I/O.
    """

    state: PluginState
    logger: logging.Logger
    state_persister: StatePersister
    registry_store: ShortcutRegistryStore
    retrodeck_paths: RetroDeckPaths
    path_probe: PathExistsReader


class StartupHealingService:
    """Reconciles persisted ``installed_roms`` / ``shortcut_registry`` against disk."""

    def __init__(self, *, config: StartupHealingServiceConfig) -> None:
        self._state = config.state
        self._logger = config.logger
        self._state_persister = config.state_persister
        self._registry_store = config.registry_store
        self._retrodeck_paths = config.retrodeck_paths
        self._path_probe = config.path_probe

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

    def prune_stale_registry(self) -> None:
        """Remove shortcut_registry entries with missing or invalid ``app_id``.

        A registry entry is considered stale when ``app_id`` is falsy or
        not an ``int``; both shapes indicate a half-written shortcut
        that Steam will not honour, so the entry is dropped from the
        registry to keep state honest.
        """
        pruned: list[str] = []
        for rom_id, entry in self._state["shortcut_registry"].items():
            app_id = entry.get("app_id")
            if not app_id or not isinstance(app_id, int):
                self._logger.info(f"Pruned stale registry entry: rom_id={rom_id} (invalid app_id={app_id})")
                pruned.append(rom_id)
        for rom_id in pruned:
            self._registry_store.delete(RegistryDeletePatch(rom_id_str=rom_id))
        if pruned:
            self._state_persister.save_state()
