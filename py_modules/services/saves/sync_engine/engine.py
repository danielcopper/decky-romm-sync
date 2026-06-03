"""SyncEngine entry point: per-rom lock dispatch and public-callable orchestration.

Owns the rom-level concurrency seam (``_rom_sync_locks``) and the
sequencing rules every public save-sync callable must follow (save-sync
enabled check, retrodeck migration gate, save-sort detect, device-
registration fallback, dispatch into the matrix executor, persistence).
Each public callable owns a narrow Unit of Work (ADR-0006): it reads the
``RomSaveState`` aggregate + ``device_id`` at the start, performs all
server/file I/O outside any transaction, and writes the mutated
aggregate back in a short write UoW at the end. The implementation of
the actual file/server transfers lives in
:mod:`services.saves.sync_engine.matrix`; device registration lives in
:mod:`services.saves.sync_engine.devices`; conflict-resolution rollback
lives in :mod:`services.saves.sync_engine.rollback`. SyncEngine wires
those sub-modules together and exposes the surface peer save services
(status, versions, slots) consume.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.rom_save_state import RomSaveState
from services.saves._messages import DEVICE_NOT_REGISTERED, SAVE_SYNC_DISABLED
from services.saves._settings import resolve_default_slot, save_sync_enabled, sync_after_exit, sync_before_launch
from services.saves.sync_engine.devices import DeviceRegistry
from services.saves.sync_engine.matrix import MatrixExecutor, MatrixOutcome
from services.saves.sync_engine.rollback import RollbackOrchestrator

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterator

    from services.protocols import (
        Clock,
        CoreResolverFn,
        DebugLogger,
        HostnameReader,
        MachineIdReader,
        MigrationPendingFn,
        RetryStrategy,
        RommSyncApi,
        SaveFileStore,
        SaveSortChangeFn,
        SettingsPersister,
        UnitOfWorkFactory,
    )
    from services.saves.rom_info import RomInfoService


__all__ = ["MatrixOutcome", "SyncEngine", "SyncEngineConfig"]


@dataclass(frozen=True)
class SyncEngineConfig:
    """Frozen wiring bundle handed to ``SyncEngine.__init__``.

    Holds the live ``settings.json`` dict (home of the save-sync feature
    toggles), the Unit-of-Work factory (the transactional seam over the
    SQLite repositories), the peer save sub-service (rom_info), the
    Protocol-typed RomM adapter and retry strategy, runtime
    infrastructure (loop, logger, clock), the Protocol-typed filesystem
    adapter, the ``DebugLogger`` seam, the ES-DE core resolver, the
    hostname provider + machine-id provider + settings persister used for
    device registration,
    the plugin version string passed to the server on register/update,
    and the optional sort-change and migration-pending callbacks
    SyncEngine consults at the entry of every public flow.
    """

    settings: dict[str, Any]
    uow_factory: UnitOfWorkFactory
    rom_info: RomInfoService
    romm_api: RommSyncApi
    retry: RetryStrategy
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    save_file_store: SaveFileStore
    log_debug: DebugLogger
    get_active_core: CoreResolverFn
    hostname_provider: HostnameReader
    machine_id_provider: MachineIdReader
    settings_persister: SettingsPersister
    plugin_version: str
    detect_sort_change: SaveSortChangeFn
    is_retrodeck_migration_pending: MigrationPendingFn


class SyncEngine:
    """Newest-wins matrix executor, sync orchestration callables, and rom-level lock dispatch."""

    def __init__(self, *, config: SyncEngineConfig) -> None:
        self._config = config
        self._settings = config.settings
        self._uow_factory = config.uow_factory
        self._rom_info = config.rom_info
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._save_file_store = config.save_file_store
        self._log_debug = config.log_debug
        self._get_active_core = config.get_active_core
        self._hostname_provider = config.hostname_provider
        self._machine_id_provider = config.machine_id_provider
        self._settings_persister = config.settings_persister
        self._plugin_version = config.plugin_version
        self._detect_sort_change = config.detect_sort_change
        self._is_retrodeck_migration_pending = config.is_retrodeck_migration_pending
        # Per-rom lock dict — serializes concurrent sync operations on the
        # same rom_id (pre_launch_sync, post_exit_sync, manual sync, resolve).
        self._rom_sync_locks: dict[int, asyncio.Lock] = {}

        self._matrix = MatrixExecutor(
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            retry=config.retry,
            logger=config.logger,
            clock=config.clock,
            save_file_store=config.save_file_store,
            log_debug=config.log_debug,
            get_active_core=config.get_active_core,
        )
        self._devices = DeviceRegistry(
            uow_factory=config.uow_factory,
            settings=config.settings,
            romm_api=config.romm_api,
            retry=config.retry,
            logger=config.logger,
            log_debug=config.log_debug,
            settings_persister=config.settings_persister,
            plugin_version=config.plugin_version,
        )
        self._rollback = RollbackOrchestrator(
            uow_factory=config.uow_factory,
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            matrix=self._matrix,
            retry=config.retry,
            clock=config.clock,
            save_file_store=config.save_file_store,
            logger=config.logger,
            log_debug=config.log_debug,
            resolve_core=self.resolve_core,
        )

    def rom_lock(self, rom_id: int) -> asyncio.Lock:
        """Return the lock for this rom_id, creating it lazily."""
        if rom_id not in self._rom_sync_locks:
            self._rom_sync_locks[rom_id] = asyncio.Lock()
        return self._rom_sync_locks[rom_id]

    # ------------------------------------------------------------------
    # Settings / device-id / core helpers
    # ------------------------------------------------------------------

    def is_save_sync_enabled(self) -> bool:
        """Whether the save-sync feature toggle is on (settings.json)."""
        return save_sync_enabled(self._settings)

    def get_device_id(self) -> str | None:
        """Server-side device id from ``kv_config`` (None when unregistered)."""
        with self._uow_factory() as uow:
            return uow.kv_config.get("device_id")

    def resolve_core(self, rom_id: int) -> str | None:
        """Resolve the active RetroArch core for a ROM, or ``None``.

        Reads the install record for the ROM's launch filename so the ES-DE
        core resolver can answer per-game; system comes from the save-path
        resolver. Used to stamp the upload emulator tag.
        """
        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            return None
        system = info["system"]
        rom_filename = os.path.basename(info.get("file_path", "")) or None
        core_so, _label = self._get_active_core(system, rom_filename)
        return core_so

    # ------------------------------------------------------------------
    # Matrix-executor delegates — consumed by tests, peer services, and
    # internal orchestration. Kept on SyncEngine so monkey-patching
    # `svc._sync_engine.do_sync_rom_saves = stub` continues to short-circuit
    # the public callables that drive `do_sync_rom_saves` through
    # `self.do_sync_rom_saves`.
    # ------------------------------------------------------------------

    def do_sync_rom_saves(
        self,
        rom_id: int,
        save_state: RomSaveState,
        device_id: str | None,
        core_so: str | None,
        default_slot: str | None = None,
    ) -> tuple[int, list[str], list[dict[str, Any]]]:
        """Sync saves for a single ROM (delegate to :class:`MatrixExecutor`)."""
        return self._matrix.sync_rom_saves(rom_id, save_state, device_id, core_so, default_slot)

    def do_download_save(
        self,
        server_save: dict[str, Any],
        saves_dir: str,
        filename: str,
        save_state: RomSaveState,
        device_id: str | None,
        system: str,
        default_slot: str | None = None,
    ) -> None:
        """Download a save file from server (delegate to :class:`MatrixExecutor`)."""
        self._matrix.do_download_save(server_save, saves_dir, filename, save_state, device_id, system, default_slot)

    def do_upload_save(
        self,
        rom_id: int,
        file_path: str,
        filename: str,
        save_state: RomSaveState,
        device_id: str | None,
        system: str,
        core_so: str | None,
        server_save: dict[str, Any] | None = None,
        default_slot: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local save file to server (delegate to :class:`MatrixExecutor`)."""
        return self._matrix.do_upload_save(
            rom_id, file_path, filename, save_state, device_id, system, core_so, server_save, default_slot
        )

    def iter_matrix_outcomes(
        self,
        rom_id: int,
        server_in_slot: list[dict[str, Any]],
        *,
        save_state: RomSaveState | None,
        device_id: str | None,
        info: dict[str, Any],
    ) -> Iterator[MatrixOutcome]:
        """Yield one :class:`MatrixOutcome` per save file in the ROM's active slot."""
        return self._matrix.iter_matrix_outcomes(
            rom_id, server_in_slot, save_state=save_state, device_id=device_id, info=info
        )

    def adopt_baseline_hash(self, save_state: RomSaveState, filename: str, local_hash: str) -> None:
        """Record ``local_hash`` as the file's ``last_sync_hash`` baseline."""
        self._matrix.adopt_baseline_hash(save_state, filename, local_hash)

    @staticmethod
    def filter_server_saves_to_slot(
        server_saves: list[dict[str, Any]], active_slot: str | None
    ) -> list[dict[str, Any]]:
        """Filter server saves to the active slot."""
        return MatrixExecutor.filter_server_saves_to_slot(server_saves, active_slot)

    def build_sync_conflict_entry(
        self,
        rom_id: int,
        filename: str,
        server: dict[str, Any],
        local_path: str | None,
        local_hash: str | None,
    ) -> dict[str, Any]:
        """Build a Phase-2 ``sync_conflict`` descriptor for the frontend."""
        return self._matrix.build_sync_conflict_entry(rom_id, filename, server, local_path, local_hash)

    # ------------------------------------------------------------------
    # Device registration — entrypoint for every sync flow that needs
    # ``device_id``. Kept on SyncEngine because pre_launch_sync,
    # post_exit_sync, sync_rom_saves, and sync_all_saves all fall back
    # to this when ``device_id`` is missing; co-locating the fallback
    # with its callers avoids a constructor callback.
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict[str, Any]:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        return await self._devices.ensure_device_registered(
            loop=self._loop,
            hostname_provider=self._hostname_provider,
            machine_id_provider=self._machine_id_provider,
        )

    async def list_devices(self) -> dict[str, Any]:
        """List all devices registered with the RomM server for this user."""
        return await self._devices.list_devices(loop=self._loop)

    # ------------------------------------------------------------------
    # Narrow-UoW read/write helpers (ADR-0006)
    # ------------------------------------------------------------------

    def _read_sync_inputs(self, rom_id: int) -> tuple[RomSaveState, str | None]:
        """Short read UoW: load the ROM's save state + device id.

        Returns the loaded :class:`RomSaveState` (a fresh default when absent)
        and the server device id. The aggregate is mutated outside the
        transaction by the matrix worker; :meth:`_write_save_state` persists it.
        """
        with self._uow_factory() as uow:
            state = uow.rom_save_states.get(rom_id) or RomSaveState()
            device_id = uow.kv_config.get("device_id")
        return state, device_id

    def _write_save_state(self, rom_id: int, save_state: RomSaveState) -> None:
        """Short write UoW: persist the mutated save state for *rom_id*."""
        with self._uow_factory() as uow:
            uow.rom_save_states.save(rom_id, save_state)

    # ------------------------------------------------------------------
    # Public sync orchestration callables
    # ------------------------------------------------------------------

    async def _refresh_save_sort_state(self, where: str) -> None:
        """Refresh save-sort state from the live RetroArch config.

        Save-sync must observe fresh save-sort state before computing
        ``saves_dir``. This call ensures ``detect_save_sort_change`` has
        run at least once before we read state, closing the race where
        another frontend detect trigger arrives after our backend entry
        point. Without this, a direct-Steam-launch with no pre-detect
        would silently download stale server content to the wrong
        layout and destroy real user progress during the subsequent
        migration (#238).

        Graceful degradation: if detect fails (e.g. retroarch.cfg is
        temporarily unreadable) we log and continue with the
        previously-known state — save-sync must not abort because of a
        config read error.
        """
        try:
            await self._loop.run_in_executor(None, self._detect_sort_change)
        except Exception as e:
            self._logger.warning(
                "%s: detect_sort_change failed (%s) — proceeding with stale state",
                where,
                e,
            )

    async def _run_rom_sync(self, rom_id: int) -> tuple[int, list[str], list[dict[str, Any]]]:
        """Read inputs → sync in executor → persist, for one ROM under its lock.

        The narrow-UoW shape (ADR-0006): a short read UoW loads the aggregate +
        device id, the matrix transfer runs outside any transaction mutating the
        aggregate in memory, then a short write UoW persists it.

        A ROM with no install record has nothing to sync — and no ``roms`` row
        to anchor a ``rom_save_states`` write against (ADR-0007 FK) — so we
        short-circuit before touching the aggregate.
        """
        info = await self._loop.run_in_executor(None, self._rom_info.get_rom_save_info, rom_id)
        if not info:
            self._log_debug(f"_run_rom_sync({rom_id}): ROM not installed, skipping")
            return 0, [], []
        save_state, device_id = await self._loop.run_in_executor(None, self._read_sync_inputs, rom_id)
        core_so = await self._loop.run_in_executor(None, self.resolve_core, rom_id)
        default_slot = resolve_default_slot(self._settings)
        synced, errors, conflicts = await self._loop.run_in_executor(
            None, self.do_sync_rom_saves, rom_id, save_state, device_id, core_so, default_slot
        )
        await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
        return synced, errors, conflicts

    async def pre_launch_sync(self, rom_id: int) -> dict[str, Any]:
        """Download newer saves from server before game launch."""
        rom_id = int(rom_id)
        async with self.rom_lock(rom_id):
            if not self.is_save_sync_enabled():
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: block pre_launch_sync if a future caller bypasses
            # the @migration_blocked decorator at the public callable. saves_dir
            # would otherwise resolve under the new home and silently desync from
            # files still living at the old home. Internal do_sync_rom_saves callers
            # (sync_all_saves, rollback_to_version) are protected by the decorator
            # on their own public callables — this guard is for pre_launch_sync.
            if self._is_retrodeck_migration_pending():
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            # Refresh save-sort state before the migration gate — see #238.
            await self._refresh_save_sort_state("pre_launch_sync")

            if self._rom_info.is_save_sort_changed():
                return {
                    "success": False,
                    "message": "RetroArch save sorting changed — migrate saves in Settings first",
                    "synced": 0,
                    "save_sort_changed": True,
                }

            if not sync_before_launch(self._settings):
                return {"success": True, "message": "Pre-launch sync disabled", "synced": 0}

            if not self.get_device_id():
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._run_rom_sync(rom_id)

            msg = f"Downloaded {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": list(conflicts),
            }

    async def post_exit_sync(self, rom_id: int) -> dict[str, Any]:
        """Upload changed saves after game exit."""
        self._logger.info("post_exit_sync called for rom_id=%d", rom_id)
        rom_id = int(rom_id)

        async with self.rom_lock(rom_id):
            if not self.is_save_sync_enabled():
                self._logger.info("post_exit_sync skipped: save sync disabled")
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: same rationale as pre_launch_sync — internal
            # do_sync_rom_saves callers are protected by @migration_blocked on
            # their public callables; this guard covers post_exit_sync only.
            if self._is_retrodeck_migration_pending():
                self._logger.info("post_exit_sync skipped: retrodeck migration pending")
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            if not sync_after_exit(self._settings):
                self._logger.info("post_exit_sync skipped: sync_after_exit disabled")
                return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

            # Refresh save-sort state before do_sync_rom_saves reads saves_dir — see #238.
            await self._refresh_save_sort_state("post_exit_sync")

            try:
                await self._loop.run_in_executor(None, self._romm_api.heartbeat)
            except Exception:
                self._logger.info("post_exit_sync skipped: server offline")
                return {"success": False, "message": "Server offline", "synced": 0, "offline": True}

            if not self.get_device_id():
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._run_rom_sync(rom_id)

            self._logger.info(
                "post_exit_sync complete for rom_id=%d: synced=%d, errors=%d, conflicts=%d",
                rom_id,
                synced,
                len(errors),
                len(conflicts),
            )

            msg = f"Uploaded {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": list(conflicts),
            }

    async def sync_rom_saves(self, rom_id: int) -> dict[str, Any]:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        rom_id = int(rom_id)
        async with self.rom_lock(rom_id):
            if not self.is_save_sync_enabled():
                return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Refresh save-sort state before do_sync_rom_saves reads saves_dir — see #238.
            # Manual sync paths must observe fresh sort state too: a user could
            # edit retroarch.cfg outside of a session and then trigger a manual
            # sync before any detect has fired.
            await self._refresh_save_sort_state("sync_rom_saves")

            if not self.get_device_id():
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._run_rom_sync(rom_id)

            msg = f"Synced {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": list(conflicts),
            }

    def _installed_rom_ids(self) -> list[int]:
        """Read the installed-ROM ids from the rom_installs aggregate (WS3)."""
        with self._uow_factory() as uow:
            return sorted(install.rom_id for install in uow.rom_installs.iter_all())

    async def sync_all_saves(self) -> dict[str, Any]:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        if not self.is_save_sync_enabled():
            return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0, "conflicts": 0}

        # Refresh save-sort state before do_sync_rom_saves reads saves_dir — see #238.
        # Manual sync paths must observe fresh sort state too: a user could
        # edit retroarch.cfg outside of a session and then trigger a manual
        # sync before any detect has fired.
        await self._refresh_save_sort_state("sync_all_saves")

        if not self.get_device_id():
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": DEVICE_NOT_REGISTERED}

        total_synced = 0
        total_errors: list[str] = []
        all_conflicts: list[dict[str, Any]] = []
        rom_count = 0

        # Only iterate installed ROMs — non-installed ROMs have no save files
        rom_ids = await self._loop.run_in_executor(None, self._installed_rom_ids)
        self._log_debug(f"sync_all_saves: {len(rom_ids)} ROMs to check")

        for rom_id_int in rom_ids:
            rom_count += 1
            async with self.rom_lock(rom_id_int):
                synced, errors, conflicts = await self._run_rom_sync(rom_id_int)
            total_synced += synced
            total_errors.extend(errors)
            all_conflicts.extend(conflicts)

        conflicts_count = len(all_conflicts)
        msg = f"Synced {total_synced} save(s) across {rom_count} ROM(s)"
        if total_errors:
            msg += f", {len(total_errors)} error(s)"
        if conflicts_count:
            msg += f", {conflicts_count} conflict(s)"
        return {
            "success": len(total_errors) == 0,
            "message": msg,
            "synced": total_synced,
            "conflicts": conflicts_count,
            "conflicts_list": list(all_conflicts),
            "roms_checked": rom_count,
            "errors": total_errors,
        }

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        server_save_id: int,
        action: str,
    ) -> dict[str, Any]:
        """Resolve a pending sync conflict (true two-sided divergence).

        Reached when ``compute_sync_action`` returned ``Conflict`` — the
        server moved AND local diverged from baseline, so the user picked a
        side via the conflict UI.

        ``server_save_id`` is the id of the server save that was surfaced to
        the user in the conflict modal. The backend round-trips it: if a
        third device has uploaded a newer save into the slot since the modal
        opened, the picked server head won't match and we return
        ``error_code="stale_conflict"`` instead of silently overwriting the
        third device's work.

        ``action`` is one of:

        - ``"keep_local"`` — push local to the current server save (PUT). When
          the local content already matches the server's content hash we adopt
          it silently without re-uploading.
        - ``"use_server"`` — download the current server save, replacing local.
        """
        rom_id_int = int(rom_id)
        async with self.rom_lock(rom_id_int):
            return await self._rollback.resolve(
                rom_id_int,
                filename,
                server_save_id,
                action,
                loop=self._loop,
            )
