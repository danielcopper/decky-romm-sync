"""Slot listing reads against the live server + persisted state.

Anything that reads slot inventory for the QAM (merging persisted local
slots with the server view and projecting the result back to SQLite)
lives here. Mutating writes for the active slot, the setup wizard, and
slot deletion belong in their own sub-modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from domain.rom_save_state import RomSaveState
from lib.list_result import ErrorCode
from services.saves._messages import SAVE_SYNC_DISABLED
from services.saves._settings import resolve_default_slot, save_sync_enabled

if TYPE_CHECKING:
    import asyncio

    from services.protocols import (
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        UnitOfWorkFactory,
    )


class SlotListing:
    """Slot inventory reader: merges server slot summaries with persisted local slots."""

    def __init__(
        self,
        *,
        settings: dict[str, Any],
        uow_factory: UnitOfWorkFactory,
        romm_api: RommSaveApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        log_debug: DebugLogger,
    ) -> None:
        self._settings = settings
        self._uow_factory = uow_factory
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._log_debug = log_debug

    def _read_inputs(self, rom_id: int) -> tuple[RomSaveState | None, str | None]:
        with self._uow_factory() as uow:
            state = uow.rom_save_states.get(rom_id)
            device_id = uow.kv_config.get("device_id")
        return state, device_id

    async def get_save_slots(self, rom_id: int) -> dict[str, Any]:
        """List available save slots for a ROM.

        Merges server slots with locally-created slots. Persists the merged
        result so local slots survive restarts. Promotes local slots to server
        when they appear on the server. Removes server slots that no longer
        exist on the server (unless they are the active_slot).
        """
        rom_id = int(rom_id)
        if not save_sync_enabled(self._settings):
            return {
                "success": False,
                "reason": "sync_disabled",
                "message": SAVE_SYNC_DISABLED,
                "slots": [],
                "active_slot": "default",
            }

        rom_state, device_id = await self._loop.run_in_executor(None, self._read_inputs, rom_id)
        default_slot = resolve_default_slot(self._settings) or "default"
        # ROM not tracked → fall back to the global default slot. ROM
        # tracked with ``active_slot=None`` → preserve legacy mode (None
        # means "no slots"; the persisted slots dict will contain ``""``).
        if rom_state is None:
            active_slot: str | None = default_slot
            persisted_slots: dict[str, dict[str, Any]] = {}
        else:
            active_slot = rom_state.active_slot
            persisted_slots = rom_state.slots

        # Fetch server slots. On failure we MUST NOT persist a merged map —
        # an empty server_slots_list would drop every persisted server slot
        # except the active one and the user would see their slot inventory
        # vanish on a transient network blip.
        try:
            summary = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.get_save_summary(rom_id, device_id=device_id),
                ),
            )
        except Exception as e:
            self._log_debug(f"Failed to fetch save slots for rom {rom_id}: {e}")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE,
                "message": str(e),
                "slots": [],
                "active_slot": active_slot,
            }
        server_slots_list: list[dict[str, Any]] = summary.get("slots", [])

        # Merge: update persisted slots with server data, promote local→server
        merged: dict[str, dict[str, Any]] = {}
        for s in server_slots_list:
            raw = s.get("slot") or s.get("slot_name")
            name = raw if raw else ""
            merged[name] = {
                "source": "server",
                "count": s.get("count", 0),
                "latest_updated_at": (s.get("latest") or {}).get("updated_at"),
            }

        self._merge_persisted_slots(persisted_slots, merged, active_slot)

        # Persist merged slots in state. The aggregate may not exist yet (ROM
        # never synced) — start a fresh default and seed its active slot.
        if rom_state is None:
            game_entry = RomSaveState()
            game_entry.switch_active_slot(active_slot)
        else:
            game_entry = rom_state
        game_entry.refresh_slot_listing(merged)
        await self._loop.run_in_executor(None, self._write_save_state, rom_id, game_entry)

        # Build response list
        result_slots = [
            {
                "slot": name,
                "source": info.get("source", "server"),
                "count": info.get("count", 0),
                "latest_updated_at": info.get("latest_updated_at"),
            }
            for name, info in sorted(merged.items())
        ]

        return {"success": True, "slots": result_slots, "active_slot": active_slot}

    def _write_save_state(self, rom_id: int, save_state: RomSaveState) -> None:
        with self._uow_factory() as uow:
            uow.rom_save_states.save(rom_id, save_state)

    @staticmethod
    def _merge_persisted_slots(
        persisted: dict[str, dict[str, Any]],
        merged: dict[str, dict[str, Any]],
        active_slot: str | None,
    ) -> None:
        """Add persisted local slots (or the active slot) that aren't on the server.

        Mutates ``merged`` in place. Local slots are always kept. A persisted
        server slot that's gone from the server is dropped unless it's the
        active slot — we want to keep the UI functional until the user
        explicitly switches away.
        """
        for name, info in persisted.items():
            if name in merged:
                continue
            if info.get("source") == "local":
                merged[name] = {"source": "local", "count": 0, "latest_updated_at": None}
            elif info.get("source") == "server" and name == (active_slot or ""):
                merged[name] = {"source": "server", "count": 0, "latest_updated_at": None}

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Fetch server save files for a specific slot.

        Used by the frontend to show save files when expanding an inactive slot panel.
        Lightweight — no local file scanning or conflict detection.
        """
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        if not save_sync_enabled(self._settings):
            return {
                "success": False,
                "reason": "sync_disabled",
                "message": SAVE_SYNC_DISABLED,
                "slot": slot,
                "saves": [],
            }

        device_id = await self._loop.run_in_executor(None, self._read_device_id)

        try:
            server_saves: list[dict[str, Any]] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                ),
            )
            saves = [
                {
                    "filename": s["file_name"],
                    "id": s["id"],
                    "size": s.get("file_size_bytes"),
                    "updated_at": s.get("updated_at", ""),
                    "emulator": s.get("emulator", ""),
                }
                for s in server_saves
            ]
            return {"success": True, "slot": slot, "saves": saves}
        except Exception as e:
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE,
                "message": str(e),
                "slot": slot,
                "saves": [],
            }

    def _read_device_id(self) -> str | None:
        with self._uow_factory() as uow:
            return uow.kv_config.get("device_id")
