"""Slot deletion: local state cleanup + server-side save removal.

Anything that tears down an existing slot — surfacing what the delete
will do to the confirmation modal, deleting the slot's server-side
saves, and cleaning the local file-tracking entries that point at them
— lives here. Slot listing, active-slot switching, and the first-sync
setup wizard belong in their own sub-modules. Persistence is the
operation's own narrow Unit of Work (ADR-0006).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib.list_result import ErrorCode
from services.saves._settings import save_sync_enabled

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.rom_save_state import RomSaveState
    from services.protocols import (
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        UnitOfWorkFactory,
    )
    from services.saves.rom_info import RomInfoService


class SlotDeleter:
    """Slot deletion: validates the request, deletes server saves, cleans local state."""

    def __init__(
        self,
        *,
        settings: dict,
        uow_factory: UnitOfWorkFactory,
        rom_info: RomInfoService,
        romm_api: RommSaveApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        log_debug: DebugLogger,
    ) -> None:
        self._settings = settings
        self._uow_factory = uow_factory
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._logger = logger
        self._log_debug = log_debug

    def _read_save_state(self, rom_id: int) -> RomSaveState | None:
        with self._uow_factory() as uow:
            return uow.rom_save_states.get(rom_id)

    def _read_device_id(self) -> str | None:
        with self._uow_factory() as uow:
            return uow.kv_config.get("device_id")

    def _write_save_state(self, rom_id: int, save_state: RomSaveState) -> None:
        with self._uow_factory() as uow:
            uow.rom_save_states.save(rom_id, save_state)

    def _validate_slot_operation(self, rom_id: int, slot: str) -> dict | tuple[RomSaveState, dict[str, dict]]:
        """Shared validation for slot delete operations.

        Returns an error dict on failure, or a (rom_state, slots_dict) tuple on
        success. The returned ``rom_state`` is the loaded aggregate; callers that
        mutate it must persist via :meth:`_write_save_state`.
        """
        if not save_sync_enabled(self._settings):
            return {"success": False, "reason": "disabled"}
        if not self._rom_info.get_rom_save_info(rom_id):
            return {"success": False, "reason": "not_installed"}
        save_state = self._read_save_state(rom_id)
        if save_state is None:
            return {"success": False, "reason": "not_found"}
        slots_dict: dict[str, dict] = save_state.slots
        if slot not in slots_dict:
            return {"success": False, "reason": "not_found"}
        return save_state, slots_dict

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict:
        """Return info about what deleting a slot would do, for the confirmation modal."""
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        result = await self._loop.run_in_executor(None, self._validate_slot_operation, rom_id, slot)
        if isinstance(result, dict):
            return result
        save_state, slots_dict = result

        slot_info = slots_dict[slot]
        source = slot_info.get("source", "server")
        active_slot = save_state.active_slot
        is_active = slot == (active_slot or "")

        # Server save count
        server_save_ids: list[int] = []
        if source == "server":
            device_id = await self._loop.run_in_executor(None, self._read_device_id)
            try:
                server_saves: list[dict] = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                    ),
                )
                server_save_ids = [s["id"] for s in server_saves]
            except Exception as e:
                # Don't return a fake "0 server saves" — the confirmation modal
                # would render "delete 0 saves" and the user would confirm a
                # destructive wipe of a slot we never actually inspected. Surface
                # the failure so the frontend can refuse to open the confirm.
                self._logger.warning(
                    f"get_slot_delete_info: failed to list saves for slot '{slot}': {e}",
                )
                return {
                    "success": False,
                    "reason": ErrorCode.SERVER_UNREACHABLE,
                    "message": "Cannot inspect slot — server unreachable",
                }

        # Local tracked files pointing to server saves in this slot
        files_state = save_state.files
        local_filenames: list[str] = []
        if server_save_ids:
            id_set = set(server_save_ids)
            for filename, fstate in files_state.items():
                if fstate.tracked_save_id in id_set:
                    local_filenames.append(filename)

        return {
            "success": True,
            "slot": slot,
            "source": source,
            "server_save_count": len(server_save_ids),
            "server_save_ids": server_save_ids,
            "local_file_count": len(local_filenames),
            "local_filenames": local_filenames,
            "is_active": is_active,
        }

    async def _delete_server_slot_saves(self, rom_id: int, slot: str) -> dict:
        """Delete all server saves in a slot. Returns result dict with count and IDs."""
        device_id = await self._loop.run_in_executor(None, self._read_device_id)
        try:
            server_saves: list[dict] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                ),
            )
            save_ids = [s["id"] for s in server_saves]
            if save_ids:
                await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.delete_server_saves(save_ids),
                    ),
                )
            return {"success": True, "count": len(save_ids), "ids": set(save_ids)}
        except Exception as e:
            self._logger.warning(f"delete_slot: server delete failed for slot '{slot}': {e}")
            return {
                "success": False,
                "reason": "server_error",
                "message": f"Failed to delete server saves: {e}",
            }

    async def delete_slot(self, rom_id: int, slot: str) -> dict:
        """Delete a save slot and all its saves (local state + server if applicable)."""
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        result = await self._loop.run_in_executor(None, self._validate_slot_operation, rom_id, slot)
        if isinstance(result, dict):
            return result
        save_state, slots_dict = result

        effective_active = save_state.active_slot or ""
        if slot == effective_active:
            return {
                "success": False,
                "reason": "active_slot",
                "message": "Cannot delete the active slot. Switch to a different slot first.",
            }

        slot_info = slots_dict[slot]
        source = slot_info.get("source", "server")

        deleted_server_saves = 0
        cleaned_files = 0
        deleted_ids: set[int] = set()

        if source == "server":
            delete_result = await self._delete_server_slot_saves(rom_id, slot)
            if not delete_result["success"]:
                return delete_result
            deleted_server_saves = delete_result["count"]
            deleted_ids = delete_result["ids"]

        # Clean up tracked file entries pointing to deleted saves
        files_state = save_state.files
        if deleted_ids:
            to_remove = [fn for fn, fs in files_state.items() if fs.tracked_save_id in deleted_ids]
            for fn in to_remove:
                save_state.delete_file_tracking(fn)
                cleaned_files += 1

        save_state.delete_slot_tracking(slot)
        await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)

        return {
            "success": True,
            "deleted_server_saves": deleted_server_saves,
            "cleaned_files": cleaned_files,
        }
