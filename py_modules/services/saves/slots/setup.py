"""First-sync setup wizard and slot migration.

Anything that drives the user-facing wizard for the very first time a
ROM is opened — surfacing the scenario the frontend renders, recording
the user's slot choice, and migrating server-side saves between slots
when requested — lives here. Slot listing, active-slot switching, and
slot deletion belong in their own sub-modules.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from domain.emulator_tag import build_emulator_tag

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        CoreResolverFn,
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        SaveFileAdapter,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.state import StateService


_NO_MIGRATION = object()  # sentinel: no slot migration requested


class SetupWizard:
    """First-sync slot configuration: setup-info fetch, confirm-choice, slot-migration."""

    def __init__(
        self,
        *,
        state: dict,
        state_svc: StateService,
        rom_info: RomInfoService,
        romm_api: RommSaveApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        save_file: SaveFileAdapter,
        log_debug: DebugLogger,
        get_active_core: CoreResolverFn,
    ) -> None:
        self._state = state
        self._state_svc = state_svc
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._logger = logger
        self._save_file = save_file
        self._log_debug = log_debug
        self._get_active_core = get_active_core

    def is_save_tracking_configured(self, rom_id: int) -> dict:
        """Check if save slot tracking is configured for a game.

        Fast, synchronous check — reads only from local state.
        Returns {"configured": bool, "active_slot": str|None}
        """
        rom_id_str = str(int(rom_id))
        game_state = self._state_svc.state.saves.get(rom_id_str)
        configured = bool(game_state.slot_confirmed) if game_state else False
        active_slot = game_state.active_slot if (game_state and configured) else None
        return {"configured": configured, "active_slot": active_slot}

    async def get_save_setup_info(self, rom_id: int) -> dict:
        """Get info needed for the first-sync setup wizard.

        Fetches server saves, checks local files, determines which
        scenario (A-E) applies so the frontend can display the right UI.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        # Local saves
        local_files = self._rom_info.find_save_files(rom_id)
        local_file_info = []
        for lf in local_files:
            local_file_info.append(
                {
                    "filename": lf["filename"],
                    "size": self._save_file.get_size(lf["path"]) if self._save_file.is_file(lf["path"]) else 0,
                }
            )

        # Server saves
        server_saves: list[dict] = []
        device_id = self._state_svc.get_server_device_id()
        try:
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                ),
            )
        except Exception as e:
            self._log_debug(f"get_save_setup_info({rom_id}): failed to list saves: {e}")

        # Group server saves by slot
        slots_map: dict[str | None, list[dict]] = {}
        for ss in server_saves:
            slot_key = ss.get("slot")
            slots_map.setdefault(slot_key, []).append(ss)

        server_slots = []
        for slot_key, saves in slots_map.items():
            latest = max((s.get("updated_at", "") for s in saves), default=None)
            server_slots.append(
                {
                    "slot": slot_key,
                    "saves": [
                        {
                            "id": s.get("id"),
                            "file_name": s.get("file_name", ""),
                            "emulator": s.get("emulator", ""),
                            "updated_at": s.get("updated_at", ""),
                            "file_size_bytes": s.get("file_size_bytes", 0),
                        }
                        for s in saves
                    ],
                    "count": len(saves),
                    "latest_updated_at": latest,
                }
            )

        # State info
        game_state = self._state_svc.state.saves.get(rom_id_str)
        default_slot = self._state_svc.state.settings.default_slot or "default"
        slot_confirmed = bool(game_state.slot_confirmed) if game_state else False
        active_slot = game_state.active_slot if (game_state and slot_confirmed) else None

        # Pre-computed wizard recommendation: auto-confirm the default slot only
        # when there are local saves and the server has no slots yet. Every other
        # combination needs the wizard so the user can choose.
        recommended_action = (
            "auto_confirm_default" if (len(local_files) > 0 and len(server_slots) == 0) else "show_wizard"
        )

        return {
            "has_local_saves": len(local_files) > 0,
            "local_files": local_file_info,
            "server_slots": server_slots,
            "default_slot": default_slot,
            "slot_confirmed": slot_confirmed,
            "active_slot": active_slot,
            "recommended_action": recommended_action,
        }

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None | object = _NO_MIGRATION,
    ) -> dict:
        """Confirm which slot to use for a game's save sync.

        Sets slot_confirmed=true and active_slot in state.

        If migrate_from_slot is provided (can be None for legacy no-slot saves),
        migrates saves: upload local files to chosen_slot, then delete old server saves.
        Pass _NO_MIGRATION sentinel (the default) to skip migration.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        chosen_slot = str(chosen_slot).strip()
        if not chosen_slot:
            return {"success": False, "needs_conflict_resolution": False, "message": "Slot name cannot be empty"}

        # Update state
        rom_state = self._state_svc.ensure_rom_state(rom_id_str)
        rom_state.active_slot = chosen_slot
        rom_state.slot_confirmed = True

        # Migration: re-upload local files to new slot, delete old server saves
        if migrate_from_slot is not _NO_MIGRATION:
            # migrate_from_slot can be None (legacy no-slot) or a string slot name
            from_slot: str | None = migrate_from_slot if isinstance(migrate_from_slot, str) else None
            try:
                await self._migrate_slot_saves(rom_id, rom_id_str, chosen_slot, from_slot)
            except Exception as e:
                self._logger.warning(f"confirm_slot_choice({rom_id}): migration failed: {e}")
                self._state_svc.save_state()
                return {
                    "success": True,
                    "needs_conflict_resolution": False,
                    "message": f"Slot confirmed but migration failed: {e}",
                }

        self._state_svc.save_state()
        return {"success": True, "needs_conflict_resolution": False, "message": "Slot confirmed"}

    async def _migrate_slot_saves(
        self,
        rom_id: int,
        rom_id_str: str,
        chosen_slot: str,
        migrate_from_slot: str | None,
    ) -> None:
        """Migrate server saves from one slot to another.

        For each local file: upload with new slot, then delete old server save.
        Safe order: POST first, DELETE after.
        """
        device_id = self._state_svc.get_server_device_id()

        # Find server saves in the old slot
        all_saves = await self._loop.run_in_executor(
            None,
            lambda: self._retry.with_retry(
                lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
            ),
        )
        old_slot_saves = [s for s in all_saves if s.get("slot") == migrate_from_slot]
        if not old_slot_saves:
            return

        # Get local files for re-upload
        local_files = self._rom_info.find_save_files(rom_id)
        local_by_name = {lf["filename"]: lf for lf in local_files}

        # Resolve emulator tag
        info = self._rom_info.get_rom_save_info(rom_id)
        system = info["system"] if info else ""
        installed = self._state["installed_roms"].get(rom_id_str, {})
        rom_filename = os.path.basename(installed.get("file_path", "")) or None
        core_so, _label = self._get_active_core(system, rom_filename)
        emulator = build_emulator_tag(core_so)

        ids_to_delete: list[int] = []

        for old_save in old_slot_saves:
            fname = old_save.get("file_name", "")
            local_file = local_by_name.get(fname)
            if local_file and self._save_file.is_file(local_file["path"]):
                # Upload to new slot
                await self._loop.run_in_executor(
                    None,
                    lambda lf=local_file, em=emulator: self._retry.with_retry(
                        lambda: self._romm_api.upload_save(
                            rom_id,
                            lf["path"],
                            em,
                            device_id=device_id,
                            slot=chosen_slot,
                        ),
                    ),
                )
            old_id = old_save.get("id")
            if old_id is not None:
                ids_to_delete.append(old_id)

        # Delete old saves
        if ids_to_delete:
            await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.delete_server_saves(ids_to_delete),
                ),
            )
