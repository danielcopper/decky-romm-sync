"""Active-slot mutation and the destructive slot-switch flow.

Anything that flips the active slot on a ROM lives here — the simple
state-only ``set_active_slot`` flip and the full ``switch_slot`` flow
that synchronises the local saves directory to the new slot's contents.
Slot listing, the setup wizard, and slot deletion belong in their own
sub-modules. Persistence is each operation's own narrow Unit of Work
(ADR-0006).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from domain.rom_save_state import RomSaveState
from domain.save_layout import SAVE_SYNC_CONTENT_DIR_REASON
from lib.list_result import ErrorCode
from services.saves._helpers import local_save_target
from services.saves._messages import SAVE_SYNC_IN_CONTENT_DIR
from services.saves._settings import resolve_default_slot, save_sync_enabled

if TYPE_CHECKING:
    import asyncio

    from services.protocols import (
        Clock,
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        SaveFileStore,
        UnitOfWorkFactory,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.status import StatusService
    from services.saves.sync_engine import SyncEngine


class SlotSwitcher:
    """Active-slot setter + the destructive slot-switch flow.

    Owns ``set_active_slot`` (the lightweight active-slot flip used
    elsewhere in the slots package and by the setup wizard) and
    ``switch_slot`` (the full pre-check + state-sync flow surfaced as a
    public callable).
    """

    def __init__(
        self,
        *,
        settings: dict[str, Any],
        uow_factory: UnitOfWorkFactory,
        sync_engine: SyncEngine,
        status_service: StatusService,
        rom_info: RomInfoService,
        romm_api: RommSaveApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        clock: Clock,
        save_file_store: SaveFileStore,
        log_debug: DebugLogger,
    ) -> None:
        self._settings = settings
        self._uow_factory = uow_factory
        self._sync_engine = sync_engine
        self._status_service = status_service
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._clock = clock
        self._save_file_store = save_file_store
        self._log_debug = log_debug

    def _read_inputs(self, rom_id: int) -> tuple[RomSaveState, str | None]:
        with self._uow_factory() as uow:
            state = uow.rom_save_states.get(rom_id) or RomSaveState()
            device_id = uow.kv_config.get("device_id")
        return state, device_id

    def _write_save_state(self, rom_id: int, save_state: RomSaveState) -> None:
        with self._uow_factory() as uow:
            uow.rom_save_states.save(rom_id, save_state)

    def set_active_slot(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Set the active save slot for a specific game.

        If the slot doesn't exist yet (not on server), it is persisted
        as a local slot. It will be promoted to server once a save is
        uploaded to it. Owns its own read→mutate→write Unit of Work.
        """
        rom_id = int(rom_id)
        slot_str = str(slot).strip() if slot else ""
        # Empty string = legacy mode (None slot)
        resolved_slot: str | None = slot_str if slot_str else None

        with self._uow_factory() as uow:
            rom_state = uow.rom_save_states.get(rom_id) or RomSaveState()
            rom_state.switch_active_slot(resolved_slot)
            uow.rom_save_states.save(rom_id, rom_state)

        self._loop.create_task(self._status_service.check_save_status_background(rom_id))
        return {"success": True, "active_slot": resolved_slot}

    def _check_slot_switch_readiness(self, rom_id: int, save_state: RomSaveState) -> dict[str, Any]:
        """Check whether it is safe to switch slots for this ROM.

        A switch is unsafe if local files have changed since the last sync
        to the current slot — those changes would be lost.
        Files that were never synced do not block (they'll be deleted on switch).

        Returns ``{"ready": True}`` or
        ``{"ready": False, "reason": str, "files": list[str]}``.
        """
        files_state = save_state.files

        pending: list[str] = []
        local_files = self._rom_info.find_save_files(rom_id)
        for lf in local_files:
            filename = lf["filename"]
            file_state = files_state.get(filename)
            last_sync_hash = file_state.last_sync_hash if file_state else None
            if last_sync_hash:
                current_hash = self._save_file_store.checksum_md5(lf["path"])
                if current_hash != last_sync_hash:
                    pending.append(filename)

        if pending:
            return {"ready": False, "reason": "pending_uploads", "files": pending}

        return {"ready": True}

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict[str, Any]:
        """Switch the active save slot with immediate state sync.

        Pre-checks (all must pass):
        1. Save sync must be enabled.
        2. ROM must be installed.
        3. RetroArch must not write saves to the content dir — otherwise the
           switch's ``saves_dir`` writes are ignored by RetroArch (#239). The
           refusal carries ``reason="savefiles_in_content_dir"``.
        4. No local files with pending changes (changed since last sync to current slot).
        5. Server must be reachable.

        On success:
        - If the new slot has server saves: downloads them, replacing local files.
        - If the new slot is empty: deletes local save files (fresh start).
        - Never uploads — saves are not carried between slots.
        """
        rom_id = int(rom_id)

        # 1. Save sync must be enabled
        if not save_sync_enabled(self._settings):
            return {"success": False, "reason": "sync_disabled"}

        # 2. Slot normalisation (empty → None for legacy mode)
        slot_str = str(new_slot).strip() if new_slot else ""
        resolved_slot: str | None = slot_str if slot_str else None

        # 3. ROM must be installed
        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "reason": "not_installed"}

        # #239: RetroArch writes saves to the content dir — switching slots
        # would download/delete files under ``saves_dir``, which RetroArch
        # ignores, so the switch could not take effect. Refuse before any
        # file write or server fetch.
        if await self._sync_engine.content_dir_blocked("switch_slot"):
            self._log_debug(f"switch_slot: content-dir layout for rom {rom_id}; refusing")
            return {
                "success": False,
                "reason": SAVE_SYNC_CONTENT_DIR_REASON,
                "message": SAVE_SYNC_IN_CONTENT_DIR,
            }

        saves_dir = info["saves_dir"]
        system = info["system"]

        save_state, device_id = await self._loop.run_in_executor(None, self._read_inputs, rom_id)

        # 4. Check for pending local changes (hashing — run in executor)
        readiness = await self._loop.run_in_executor(
            None,
            self._check_slot_switch_readiness,
            rom_id,
            save_state,
        )
        if not readiness.get("ready"):
            return {
                "success": False,
                "reason": readiness.get("reason", "pending_uploads"),
                "files": readiness.get("files", []),
            }

        # 5. Fetch server saves for the new slot (also proves server is reachable)
        try:
            all_server_saves: list[dict[str, Any]] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                ),
            )
        except Exception as e:
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE,
                "message": str(e),
            }

        # Filter to the target slot (FakeSaveApi doesn't filter, real API may not either)
        # Normalize "" and None both to None before comparing (legacy saves may use either)
        slot_saves = [s for s in all_server_saves if (s.get("slot") or None) == resolved_slot]

        # 6. Update active slot in state (in memory; persisted once at the end)
        save_state.switch_active_slot(resolved_slot)

        # 7. Sync local state to match the new slot
        default_slot = resolve_default_slot(self._settings)
        if slot_saves:
            # New slot has server saves — download them, replacing local files.
            # rom_name is guaranteed by the earlier ``info`` check.
            await self._loop.run_in_executor(
                None,
                self._do_switch_downloads,
                slot_saves,
                saves_dir,
                save_state,
                device_id,
                system,
                info["rom_name"],
                default_slot,
            )
        else:
            # New slot is empty — delete local save files for a fresh start
            await self._loop.run_in_executor(
                None,
                self._delete_local_saves_for_switch,
                rom_id,
                save_state,
            )

        # 8. Update last_sync_check_at and persist the accumulated mutations once.
        save_state.mark_sync_evaluated(self._clock.now().isoformat())
        await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)

        # 9. Return fresh status
        save_status = await self._status_service.get_save_status(rom_id)
        return {"success": True, "save_status": save_status}

    def _do_switch_downloads(
        self,
        slot_saves: list[dict[str, Any]],
        saves_dir: str,
        save_state: RomSaveState,
        device_id: str | None,
        system: str,
        rom_name: str,
        default_slot: str | None,
    ) -> None:
        """Download all saves from *slot_saves* into *saves_dir*.

        Each save lands at ``<saves_dir>/<rom_name>.<server.file_extension>`` —
        the canonical RetroArch path. Mutates *save_state* in memory; the caller
        owns the write Unit of Work. Runs synchronously; call via
        ``run_in_executor``.
        """
        for server_save in slot_saves:
            target = local_save_target(server_save, rom_name)
            self._sync_engine.do_download_save(
                server_save, saves_dir, target, save_state, device_id, system, default_slot
            )

    def _delete_local_saves_for_switch(self, rom_id: int, save_state: RomSaveState) -> None:
        """Delete local save files and clear file tracking state for a slot switch.

        Unlike delete_local_saves (the callable), this preserves slot config
        (active_slot, slot_confirmed, slots dict) and only clears files + tracking.
        Mutates *save_state* in memory; the caller owns the write Unit of Work.
        Runs synchronously — call via run_in_executor.
        """
        local_files = self._rom_info.find_save_files(rom_id)
        for lf in local_files:
            try:
                self._save_file_store.remove_file(lf["path"])
                self._log_debug(f"Deleted local save for switch: {lf['filename']}")
            except Exception as e:
                self._log_debug(f"Failed to delete {lf['filename']} during switch: {e}")

        # Clear file tracking state (but keep slot config)
        save_state.clear_baselines()
