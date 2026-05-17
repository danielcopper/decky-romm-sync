"""Slot listing reads against the live server + persisted state.

Anything that reads slot inventory for the QAM (merging persisted local
slots with the server view and projecting the result back to disk) lives
here. Mutating writes for the active slot, the setup wizard, and slot
deletion belong in their own sub-modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.saves._messages import SAVE_SYNC_DISABLED

if TYPE_CHECKING:
    import asyncio

    from services.protocols import (
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
    )
    from services.saves.state import StateService


class SlotListing:
    """Slot inventory reader: merges server slot summaries with persisted local slots."""

    def __init__(
        self,
        *,
        state_svc: StateService,
        romm_api: RommSaveApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        log_debug: DebugLogger,
    ) -> None:
        self._state_svc = state_svc
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._log_debug = log_debug

    async def get_save_slots(self, rom_id: int) -> dict:
        """List available save slots for a ROM.

        Merges server slots with locally-created slots. Persists the merged
        result so local slots survive restarts. Promotes local slots to server
        when they appear on the server. Removes server slots that no longer
        exist on the server (unless they are the active_slot).
        """
        rom_id = int(rom_id)
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "slots": [], "active_slot": "default"}

        rom_id_str = str(rom_id)
        device_id = self._state_svc.get_server_device_id()
        rom_state = self._state_svc.state.saves.get(rom_id_str)
        default_slot = self._state_svc.state.settings.default_slot or "default"
        # ROM not tracked → fall back to the global default slot. ROM
        # tracked with ``active_slot=None`` → preserve legacy mode (None
        # means "no slots"; the persisted slots dict will contain ``""``).
        if rom_state is None:
            active_slot: str | None = default_slot
            persisted_slots: dict[str, dict] = {}
        else:
            active_slot = rom_state.active_slot
            persisted_slots = rom_state.slots

        # Fetch server slots
        server_slots_list: list[dict] = []
        try:
            summary = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.get_save_summary(rom_id, device_id=device_id),
                ),
            )
            server_slots_list = summary.get("slots", [])
        except Exception as e:
            self._log_debug(f"Failed to fetch save slots for rom {rom_id}: {e}")

        # Merge: update persisted slots with server data, promote local→server
        merged: dict[str, dict] = {}
        for s in server_slots_list:
            raw = s.get("slot") or s.get("slot_name")
            name = raw if raw else ""
            merged[name] = {
                "source": "server",
                "count": s.get("count", 0),
                "latest_updated_at": (s.get("latest") or {}).get("updated_at"),
            }

        self._merge_persisted_slots(persisted_slots, merged, active_slot)

        # Persist merged slots in state
        game_entry = self._state_svc.ensure_rom_state(rom_id_str)
        game_entry.slots = merged
        self._state_svc.save_state()

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

    @staticmethod
    def _merge_persisted_slots(
        persisted: dict[str, dict],
        merged: dict[str, dict],
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

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict:
        """Fetch server save files for a specific slot.

        Used by the frontend to show save files when expanding an inactive slot panel.
        Lightweight — no local file scanning or conflict detection.
        """
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "slot": slot, "saves": [], "error": SAVE_SYNC_DISABLED}

        device_id = self._state_svc.get_server_device_id()

        try:
            server_saves: list[dict] = await self._loop.run_in_executor(
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
            return {"success": False, "slot": slot, "saves": [], "error": str(e)}
