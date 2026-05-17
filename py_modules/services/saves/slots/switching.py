"""Active-slot mutation and the destructive slot-switch flow.

Anything that flips the active slot on a ROM lives here — the simple
state-only ``_set_active_slot`` flip and the full ``switch_slot`` flow
that synchronises the local saves directory to the new slot's contents.
Slot listing, the setup wizard, and slot deletion belong in their own
sub-modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.saves._helpers import _local_save_target

if TYPE_CHECKING:
    import asyncio

    from services.protocols import (
        Clock,
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        SaveFileAdapter,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.state import StateService
    from services.saves.status import StatusService
    from services.saves.sync_engine import SyncEngine


class SlotSwitcher:
    """Active-slot setter + the destructive slot-switch flow.

    Owns ``_set_active_slot`` (the lightweight active-slot flip used
    elsewhere in the slots package and by the setup wizard) and
    ``switch_slot`` (the full pre-check + state-sync flow surfaced as a
    public callable).
    """

    def __init__(
        self,
        *,
        state_svc: StateService,
        sync_engine: SyncEngine,
        status_service: StatusService,
        rom_info: RomInfoService,
        romm_api: RommSaveApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        clock: Clock,
        save_file: SaveFileAdapter,
        log_debug: DebugLogger,
    ) -> None:
        self._state_svc = state_svc
        self._sync_engine = sync_engine
        self._status_service = status_service
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._clock = clock
        self._save_file = save_file
        self._log_debug = log_debug

    def _set_active_slot(self, rom_id: int, slot: str) -> dict:
        """Set the active save slot for a specific game.

        If the slot doesn't exist yet (not on server), it is persisted
        as a local slot. It will be promoted to server once a save is
        uploaded to it.
        """
        rom_id = int(rom_id)
        slot_str = str(slot).strip() if slot else ""
        # Empty string = legacy mode (None slot)
        resolved_slot: str | None = slot_str if slot_str else None

        rom_id_str = str(rom_id)
        rom_state = self._state_svc.ensure_rom_state(rom_id_str)
        rom_state.active_slot = resolved_slot

        # Ensure slot is in the persisted slots dict (use "" as key for legacy/None)
        slot_key = resolved_slot if resolved_slot is not None else ""
        if slot_key not in rom_state.slots:
            rom_state.slots[slot_key] = {"source": "local", "count": 0, "latest_updated_at": None}

        self._state_svc.save_state()
        self._loop.create_task(self._status_service.check_save_status_background(rom_id))
        return {"success": True, "active_slot": resolved_slot}

    def _check_slot_switch_readiness(self, rom_id: int) -> dict:
        """Check whether it is safe to switch slots for this ROM.

        A switch is unsafe if local files have changed since the last sync
        to the current slot — those changes would be lost.
        Files that were never synced do not block (they'll be deleted on switch).

        Returns ``{"ready": True}`` or
        ``{"ready": False, "reason": str, "files": list[str]}``.
        """
        rom_id_str = str(rom_id)
        save_state = self._state_svc.state.saves.get(rom_id_str)
        files_state = save_state.files if save_state else {}

        pending: list[str] = []
        local_files = self._rom_info.find_save_files(rom_id)
        for lf in local_files:
            filename = lf["filename"]
            file_state = files_state.get(filename)
            last_sync_hash = file_state.last_sync_hash if file_state else None
            if last_sync_hash:
                current_hash = self._save_file.checksum_md5(lf["path"])
                if current_hash != last_sync_hash:
                    pending.append(filename)

        if pending:
            return {"ready": False, "reason": "pending_uploads", "files": pending}

        return {"ready": True}

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict:
        """Switch the active save slot with immediate state sync.

        Pre-checks (all must pass):
        1. Save sync must be enabled.
        2. ROM must be installed.
        3. No local files with pending changes (changed since last sync to current slot).
        4. Server must be reachable.

        On success:
        - If the new slot has server saves: downloads them, replacing local files.
        - If the new slot is empty: deletes local save files (fresh start).
        - Never uploads — saves are not carried between slots.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        # 1. Save sync must be enabled
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "reason": "sync_disabled"}

        # 2. Slot normalisation (empty → None for legacy mode)
        slot_str = str(new_slot).strip() if new_slot else ""
        resolved_slot: str | None = slot_str if slot_str else None

        # 3. ROM must be installed
        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "reason": "not_installed"}

        saves_dir = info["saves_dir"]
        system = info["system"]

        # 4. Check for pending local changes (hashing — run in executor)
        readiness = await self._loop.run_in_executor(
            None,
            self._check_slot_switch_readiness,
            rom_id,
        )
        if not readiness.get("ready"):
            return {
                "success": False,
                "reason": readiness.get("reason", "pending_uploads"),
                "files": readiness.get("files", []),
            }

        # 5. Fetch server saves for the new slot (also proves server is reachable)
        device_id = self._state_svc.get_server_device_id()
        try:
            all_server_saves: list[dict] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                ),
            )
        except Exception:
            return {"success": False, "reason": "server_unreachable"}

        # Filter to the target slot (FakeSaveApi doesn't filter, real API may not either)
        # Normalize "" and None both to None before comparing (legacy saves may use either)
        slot_saves = [s for s in all_server_saves if (s.get("slot") or None) == resolved_slot]

        # 6. Update active slot in state
        self._set_active_slot(rom_id, new_slot)

        # 7. Sync local state to match the new slot
        if slot_saves:
            # New slot has server saves — download them, replacing local files.
            # rom_name is guaranteed by the earlier ``info`` check.
            await self._loop.run_in_executor(
                None,
                self._do_switch_downloads,
                slot_saves,
                saves_dir,
                rom_id_str,
                system,
                info["rom_name"],
            )
        else:
            # New slot is empty — delete local save files for a fresh start
            await self._loop.run_in_executor(
                None,
                self._delete_local_saves_for_switch,
                rom_id,
                rom_id_str,
            )

        # 8. Update last_sync_check_at
        save_entry = self._state_svc.ensure_rom_state(rom_id_str)
        save_entry.last_sync_check_at = self._clock.now().isoformat()
        self._state_svc.save_state()

        # 9. Return fresh status
        save_status = await self._status_service.get_save_status(rom_id)
        return {"success": True, "save_status": save_status}

    def _do_switch_downloads(
        self,
        slot_saves: list[dict],
        saves_dir: str,
        rom_id_str: str,
        system: str,
        rom_name: str,
    ) -> None:
        """Download all saves from *slot_saves* into *saves_dir*.

        Each save lands at ``<saves_dir>/<rom_name>.<server.file_extension>`` —
        the canonical RetroArch path. Runs synchronously; call via
        ``run_in_executor``.
        """
        for server_save in slot_saves:
            target = _local_save_target(server_save, rom_name)
            self._sync_engine._do_download_save(server_save, saves_dir, target, rom_id_str, system)

    def _delete_local_saves_for_switch(self, rom_id: int, rom_id_str: str) -> None:
        """Delete local save files and clear file tracking state for a slot switch.

        Unlike delete_local_saves (the callable), this preserves slot config
        (active_slot, slot_confirmed, slots dict) and only clears files + tracking.
        Runs synchronously — call via run_in_executor.
        """
        local_files = self._rom_info.find_save_files(rom_id)
        for lf in local_files:
            try:
                self._save_file.remove(lf["path"])
                self._log_debug(f"Deleted local save for switch: {lf['filename']}")
            except Exception as e:
                self._log_debug(f"Failed to delete {lf['filename']} during switch: {e}")

        # Clear file tracking state (but keep slot config)
        self._state_svc.clear_files_state(rom_id_str)
