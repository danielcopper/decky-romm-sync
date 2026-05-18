"""SyncEngine entry point: per-rom lock dispatch and public-callable orchestration.

Owns the rom-level concurrency seam (``_rom_sync_locks``) and the
sequencing rules every public save-sync callable must follow (save-sync
enabled check, retrodeck migration gate, save-sort detect, device-
registration fallback, dispatch into the matrix executor, persistence).
The implementation of the actual file/server transfers lives in
:mod:`services.saves.sync_engine.matrix`; device registration lives in
:mod:`services.saves.sync_engine.devices`; conflict-resolution rollback
lives in :mod:`services.saves.sync_engine.rollback`. SyncEngine wires
those sub-modules together and exposes the surface peer save services
(status, versions, slots) consume.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from services.saves._messages import DEVICE_NOT_REGISTERED, SAVE_SYNC_DISABLED
from services.saves.sync_engine.devices import DeviceRegistry
from services.saves.sync_engine.matrix import MatrixExecutor, MatrixOutcome
from services.saves.sync_engine.rollback import RollbackOrchestrator

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        Clock,
        CoreResolverFn,
        DebugLogger,
        HostnameProvider,
        MigrationPendingFn,
        RetryStrategy,
        RommSyncApi,
        SaveFileStore,
        SaveSortChangeFn,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.state import StateService


__all__ = ["MatrixOutcome", "SyncEngine", "SyncEngineConfig"]


@dataclass(frozen=True)
class SyncEngineConfig:
    """Frozen wiring bundle handed to ``SyncEngine.__init__``.

    Holds the main plugin state dict, the peer save sub-services
    (state, rom_info), the Protocol-typed RomM adapter and retry
    strategy, runtime infrastructure (loop, logger, clock), the
    Protocol-typed filesystem adapter, the ``DebugLogger`` seam, the
    ES-DE core resolver, the hostname provider used for device
    registration, the plugin version string passed to the server on
    register/update, and the optional sort-change and migration-pending
    callbacks SyncEngine consults at the entry of every public flow.
    """

    state: dict
    state_svc: StateService
    rom_info: RomInfoService
    romm_api: RommSyncApi
    retry: RetryStrategy
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    save_file_store: SaveFileStore
    log_debug: DebugLogger
    get_active_core: CoreResolverFn
    hostname_provider: HostnameProvider
    plugin_version: str
    detect_sort_change: SaveSortChangeFn | None
    is_retrodeck_migration_pending: MigrationPendingFn | None


class SyncEngine:
    """Newest-wins matrix executor, sync orchestration callables, and rom-level lock dispatch."""

    def __init__(self, *, config: SyncEngineConfig) -> None:
        self._config = config
        self._state = config.state
        self._state_svc = config.state_svc
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
        self._plugin_version = config.plugin_version
        self._detect_sort_change = config.detect_sort_change
        self._is_retrodeck_migration_pending = config.is_retrodeck_migration_pending
        # Per-rom lock dict — serializes concurrent sync operations on the
        # same rom_id (pre_launch_sync, post_exit_sync, manual sync, resolve).
        self._rom_sync_locks: dict[int, asyncio.Lock] = {}

        self._matrix = MatrixExecutor(
            state=config.state,
            state_svc=config.state_svc,
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
            state_svc=config.state_svc,
            romm_api=config.romm_api,
            retry=config.retry,
            logger=config.logger,
            log_debug=config.log_debug,
            plugin_version=config.plugin_version,
        )
        self._rollback = RollbackOrchestrator(
            state_svc=config.state_svc,
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            matrix=self._matrix,
            retry=config.retry,
            clock=config.clock,
            save_file_store=config.save_file_store,
            logger=config.logger,
            log_debug=config.log_debug,
        )

    def _rom_lock(self, rom_id: int) -> asyncio.Lock:
        """Return the lock for this rom_id, creating it lazily."""
        if rom_id not in self._rom_sync_locks:
            self._rom_sync_locks[rom_id] = asyncio.Lock()
        return self._rom_sync_locks[rom_id]

    # ------------------------------------------------------------------
    # Matrix-executor delegates — consumed by tests, peer services, and
    # internal orchestration. Kept on SyncEngine so monkey-patching
    # `svc._sync_engine._sync_rom_saves = stub` continues to short-circuit
    # the public callables that drive `_sync_rom_saves` through
    # `self._sync_rom_saves`.
    # ------------------------------------------------------------------

    def _sync_rom_saves(self, rom_id: int) -> tuple[int, list[str], list[dict]]:
        """Sync saves for a single ROM (delegate to :class:`MatrixExecutor`)."""
        return self._matrix.sync_rom_saves(rom_id)

    def _do_download_save(self, server_save: dict, saves_dir: str, filename: str, rom_id_str: str, system: str) -> None:
        """Download a save file from server (delegate to :class:`MatrixExecutor`)."""
        self._matrix.do_download_save(server_save, saves_dir, filename, rom_id_str, system)

    def _do_upload_save(
        self,
        rom_id: int,
        file_path: str,
        filename: str,
        rom_id_str: str,
        system: str,
        server_save: dict | None = None,
    ) -> dict:
        """Upload a local save file to server (delegate to :class:`MatrixExecutor`)."""
        return self._matrix.do_upload_save(rom_id, file_path, filename, rom_id_str, system, server_save)

    def iter_matrix_outcomes(
        self,
        rom_id: int,
        server_in_slot: list[dict],
        *,
        info: dict,
    ) -> Iterator[MatrixOutcome]:
        """Yield one :class:`MatrixOutcome` per save file in the ROM's active slot."""
        return self._matrix.iter_matrix_outcomes(rom_id, server_in_slot, info=info)

    def _adopt_baseline_hash(self, rom_id_str: str, filename: str, local_hash: str) -> None:
        """Persist ``local_hash`` as the file's ``last_sync_hash`` baseline."""
        self._matrix.adopt_baseline_hash(rom_id_str, filename, local_hash)

    @staticmethod
    def _filter_server_saves_to_slot(server_saves: list[dict], active_slot: str | None) -> list[dict]:
        """Filter server saves to the active slot."""
        return MatrixExecutor.filter_server_saves_to_slot(server_saves, active_slot)

    def _build_sync_conflict_entry(
        self,
        rom_id: int,
        filename: str,
        server: dict,
        local_path: str | None,
        local_hash: str | None,
    ) -> dict:
        """Build a Phase-2 ``sync_conflict`` descriptor for the frontend."""
        return self._matrix.build_sync_conflict_entry(rom_id, filename, server, local_path, local_hash)

    # ------------------------------------------------------------------
    # Device registration — entrypoint for every sync flow that needs
    # ``device_id``. Kept on SyncEngine because pre_launch_sync,
    # post_exit_sync, sync_rom_saves, and sync_all_saves all fall back
    # to this when ``device_id`` is missing; co-locating the fallback
    # with its callers avoids a constructor callback.
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        return await self._devices.ensure_device_registered(
            loop=self._loop,
            hostname_provider=self._hostname_provider,
        )

    async def list_devices(self) -> dict:
        """List all devices registered with the RomM server for this user."""
        return await self._devices.list_devices(loop=self._loop)

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
        if self._detect_sort_change is None:
            return
        try:
            await self._loop.run_in_executor(None, self._detect_sort_change)
        except Exception as e:
            self._logger.warning(
                "%s: detect_sort_change failed (%s) — proceeding with stale state",
                where,
                e,
            )

    async def pre_launch_sync(self, rom_id: int) -> dict:
        """Download newer saves from server before game launch."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._state_svc.is_save_sync_enabled():
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: block pre_launch_sync if a future caller bypasses
            # the @migration_blocked decorator at the public callable. saves_dir
            # would otherwise resolve under the new home and silently desync from
            # files still living at the old home. Internal _sync_rom_saves callers
            # (sync_all_saves, rollback_to_version) are protected by the decorator
            # on their own public callables — this guard is for pre_launch_sync.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
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

            if not self._state_svc.state.settings.sync_before_launch:
                return {"success": True, "message": "Pre-launch sync disabled", "synced": 0}

            if not self._state_svc.state.device_id:
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            self._state_svc.save_state()

            msg = f"Downloaded {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def post_exit_sync(self, rom_id: int) -> dict:
        """Upload changed saves after game exit."""
        self._logger.info("post_exit_sync called for rom_id=%d", rom_id)
        rom_id = int(rom_id)

        async with self._rom_lock(rom_id):
            if not self._state_svc.is_save_sync_enabled():
                self._logger.info("post_exit_sync skipped: save sync disabled")
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: same rationale as pre_launch_sync — internal
            # _sync_rom_saves callers are protected by @migration_blocked on
            # their public callables; this guard covers post_exit_sync only.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                self._logger.info("post_exit_sync skipped: retrodeck migration pending")
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            if not self._state_svc.state.settings.sync_after_exit:
                self._logger.info("post_exit_sync skipped: sync_after_exit disabled")
                return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            await self._refresh_save_sort_state("post_exit_sync")

            try:
                await self._loop.run_in_executor(None, self._romm_api.heartbeat)
            except Exception:
                self._logger.info("post_exit_sync skipped: server offline")
                return {"success": False, "message": "Server offline", "synced": 0, "offline": True}

            if not self._state_svc.state.device_id:
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            self._state_svc.save_state()

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
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def sync_rom_saves(self, rom_id: int) -> dict:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._state_svc.is_save_sync_enabled():
                return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            # Manual sync paths must observe fresh sort state too: a user could
            # edit retroarch.cfg outside of a session and then trigger a manual
            # sync before any detect has fired.
            await self._refresh_save_sort_state("sync_rom_saves")

            if not self._state_svc.state.device_id:
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            self._state_svc.save_state()

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
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def sync_all_saves(self) -> dict:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0, "conflicts": 0}

        # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
        # Manual sync paths must observe fresh sort state too: a user could
        # edit retroarch.cfg outside of a session and then trigger a manual
        # sync before any detect has fired.
        await self._refresh_save_sort_state("sync_all_saves")

        if not self._state_svc.state.device_id:
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": DEVICE_NOT_REGISTERED}

        total_synced = 0
        total_errors: list[str] = []
        all_conflicts: list[dict] = []
        rom_count = 0

        # Only iterate installed ROMs — non-installed ROMs have no save files
        rom_ids = set(self._state["installed_roms"].keys())
        self._log_debug(f"sync_all_saves: {len(rom_ids)} ROMs to check")

        for rom_id_str in sorted(rom_ids):
            rom_count += 1
            rom_id_int = int(rom_id_str)
            async with self._rom_lock(rom_id_int):
                synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id_int)
            total_synced += synced
            total_errors.extend(errors)
            all_conflicts.extend(conflicts)

        self._state_svc.save_state()

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
            "conflicts_list": [c if isinstance(c, dict) else asdict(c) for c in all_conflicts],
            "roms_checked": rom_count,
            "errors": total_errors,
        }

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        server_save_id: int,
        action: str,
    ) -> dict:
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
        async with self._rom_lock(rom_id_int):
            return await self._rollback.resolve(
                rom_id_int,
                filename,
                server_save_id,
                action,
                loop=self._loop,
            )
