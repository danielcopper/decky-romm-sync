from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from domain.emulator_tag import build_emulator_tag
from services.saves._helpers import _local_save_target
from services.saves._messages import SAVE_SYNC_DISABLED

if TYPE_CHECKING:
    import logging

    from services.protocols import RetryStrategy, RommApiProtocol
    from services.saves import SaveService
    from services.saves.state import StateService
    from services.saves.sync_engine import SyncEngine


_NO_MIGRATION = object()  # sentinel: no slot migration requested


class SlotsService:
    """Slot lifecycle: list, set-active, switch, migrate, delete, first-sync wizard."""

    def __init__(
        self,
        *,
        save_service: SaveService,
        state_svc: StateService,
        sync_engine: SyncEngine,
        romm_api: RommApiProtocol,
        retry: RetryStrategy,
        logger: logging.Logger,
    ) -> None:
        self._save_service = save_service
        self._state_svc = state_svc
        self._sync_engine = sync_engine
        self._romm_api = romm_api
        self._retry = retry
        self._logger = logger

    # ------------------------------------------------------------------
    # Slot listing
    # ------------------------------------------------------------------

    async def get_save_slots(self, rom_id: int) -> dict:
        """List available save slots for a ROM.

        Merges server slots with locally-created slots. Persists the merged
        result so local slots survive restarts. Promotes local slots to server
        when they appear on the server. Removes server slots that no longer
        exist on the server (unless they are the active_slot).
        """
        rom_id = int(rom_id)
        if not self._save_service._is_save_sync_enabled():
            return {"success": False, "slots": [], "active_slot": "default"}

        rom_id_str = str(rom_id)
        device_id = self._save_service._get_server_device_id()
        rom_state = self._state_svc.data.get("saves", {}).get(rom_id_str, {})
        active_slot = rom_state.get(
            "active_slot",
            self._state_svc.data.get("settings", {}).get("default_slot", "default"),
        )
        persisted_slots: dict[str, dict] = rom_state.get("slots", {})

        # Fetch server slots
        server_slots_list: list[dict] = []
        try:
            summary = await self._save_service._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.get_save_summary(rom_id, device_id=device_id),
                ),
            )
            server_slots_list = summary.get("slots", [])
        except Exception as e:
            self._save_service._log_debug(f"Failed to fetch save slots for rom {rom_id}: {e}")

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
        game_entry = self._state_svc.data.setdefault("saves", {}).setdefault(rom_id_str, {})
        game_entry["slots"] = merged
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

        if not self._save_service._is_save_sync_enabled():
            return {"success": False, "slot": slot, "saves": [], "error": SAVE_SYNC_DISABLED}

        device_id = self._save_service._get_server_device_id()

        try:
            server_saves: list[dict] = await self._save_service._loop.run_in_executor(
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

    # ------------------------------------------------------------------
    # Active slot mutation
    # ------------------------------------------------------------------

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
        saves = self._state_svc.data.setdefault("saves", {})
        if rom_id_str not in saves:
            saves[rom_id_str] = {"files": {}, "active_slot": resolved_slot}
        else:
            saves[rom_id_str]["active_slot"] = resolved_slot

        # Ensure slot is in the persisted slots dict (use "" as key for legacy/None)
        slot_key = resolved_slot if resolved_slot is not None else ""
        slots_dict: dict[str, dict] = saves[rom_id_str].setdefault("slots", {})
        if slot_key not in slots_dict:
            slots_dict[slot_key] = {"source": "local", "count": 0, "latest_updated_at": None}

        self._state_svc.save_state()
        self._save_service._loop.create_task(self._save_service.check_save_status_background(rom_id))
        return {"success": True, "active_slot": resolved_slot}

    # ------------------------------------------------------------------
    # Slot switching
    # ------------------------------------------------------------------

    def _check_slot_switch_readiness(self, rom_id: int) -> dict:
        """Check whether it is safe to switch slots for this ROM.

        A switch is unsafe if local files have changed since the last sync
        to the current slot — those changes would be lost.
        Files that were never synced do not block (they'll be deleted on switch).

        Returns ``{"ready": True}`` or
        ``{"ready": False, "reason": str, "files": list[str]}``.
        """
        rom_id_str = str(rom_id)
        save_state = self._state_svc.data["saves"].get(rom_id_str, {})
        files_state = save_state.get("files", {})

        pending: list[str] = []
        local_files = self._save_service._find_save_files(rom_id)
        for lf in local_files:
            filename = lf["filename"]
            file_state = files_state.get(filename, {})
            last_sync_hash = file_state.get("last_sync_hash")
            if last_sync_hash:
                current_hash = self._save_service._file_md5(lf["path"])
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
        if not self._save_service._is_save_sync_enabled():
            return {"success": False, "reason": "sync_disabled"}

        # 2. Slot normalisation (empty → None for legacy mode)
        slot_str = str(new_slot).strip() if new_slot else ""
        resolved_slot: str | None = slot_str if slot_str else None

        # 3. ROM must be installed
        info = self._save_service._get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "reason": "not_installed"}

        saves_dir = info["saves_dir"]
        system = info["system"]

        # 4. Check for pending local changes (hashing — run in executor)
        readiness = await self._save_service._loop.run_in_executor(
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
        device_id = self._save_service._get_server_device_id()
        try:
            all_server_saves: list[dict] = await self._save_service._loop.run_in_executor(
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
            # rom_name is guaranteed by the earlier ``info`` check (line 1642).
            await self._save_service._loop.run_in_executor(
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
            await self._save_service._loop.run_in_executor(
                None,
                self._delete_local_saves_for_switch,
                rom_id,
                rom_id_str,
            )

        # 8. Update last_sync_check_at
        save_entry = self._state_svc.data["saves"].setdefault(rom_id_str, {})
        save_entry["last_sync_check_at"] = datetime.now(UTC).isoformat()
        self._state_svc.save_state()

        # 9. Return fresh status
        save_status = await self._save_service.get_save_status(rom_id)
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
        local_files = self._save_service._find_save_files(rom_id)
        for lf in local_files:
            try:
                os.remove(lf["path"])
                self._save_service._log_debug(f"Deleted local save for switch: {lf['filename']}")
            except Exception as e:
                self._save_service._log_debug(f"Failed to delete {lf['filename']} during switch: {e}")

        # Clear file tracking state (but keep slot config)
        self._state_svc.clear_files_state(rom_id_str)

    # ------------------------------------------------------------------
    # Save Setup Wizard
    # ------------------------------------------------------------------

    def is_save_tracking_configured(self, rom_id: int) -> dict:
        """Check if save slot tracking is configured for a game.

        Fast, synchronous check — reads only from local state.
        Returns {"configured": bool, "active_slot": str|None}
        """
        rom_id_str = str(int(rom_id))
        game_state = self._state_svc.data["saves"].get(rom_id_str, {})
        configured = game_state.get("slot_confirmed", False)
        active_slot = game_state.get("active_slot") if configured else None
        return {"configured": configured, "active_slot": active_slot}

    async def get_save_setup_info(self, rom_id: int) -> dict:
        """Get info needed for the first-sync setup wizard.

        Fetches server saves, checks local files, determines which
        scenario (A-E) applies so the frontend can display the right UI.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        # Local saves
        local_files = self._save_service._find_save_files(rom_id)
        local_file_info = []
        for lf in local_files:
            local_file_info.append(
                {
                    "filename": lf["filename"],
                    "size": os.path.getsize(lf["path"]) if os.path.isfile(lf["path"]) else 0,
                }
            )

        # Server saves
        server_saves: list[dict] = []
        device_id = self._save_service._get_server_device_id()
        try:
            server_saves = await self._save_service._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                ),
            )
        except Exception as e:
            self._save_service._log_debug(f"get_save_setup_info({rom_id}): failed to list saves: {e}")

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
        game_state = self._state_svc.data["saves"].get(rom_id_str, {})
        default_slot = self._state_svc.data.get("settings", {}).get("default_slot", "default")
        slot_confirmed = game_state.get("slot_confirmed", False)
        active_slot = game_state.get("active_slot") if slot_confirmed else None

        return {
            "has_local_saves": len(local_files) > 0,
            "local_files": local_file_info,
            "server_slots": server_slots,
            "default_slot": default_slot,
            "slot_confirmed": slot_confirmed,
            "active_slot": active_slot,
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
        saves = self._state_svc.data.setdefault("saves", {})
        if rom_id_str not in saves:
            saves[rom_id_str] = {"files": {}}
        saves[rom_id_str]["active_slot"] = chosen_slot
        saves[rom_id_str]["slot_confirmed"] = True

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
        device_id = self._save_service._get_server_device_id()

        # Find server saves in the old slot
        all_saves = await self._save_service._loop.run_in_executor(
            None,
            lambda: self._retry.with_retry(
                lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
            ),
        )
        old_slot_saves = [s for s in all_saves if s.get("slot") == migrate_from_slot]
        if not old_slot_saves:
            return

        # Get local files for re-upload
        local_files = self._save_service._find_save_files(rom_id)
        local_by_name = {lf["filename"]: lf for lf in local_files}

        # Resolve emulator tag
        info = self._save_service._get_rom_save_info(rom_id)
        system = info["system"] if info else ""
        installed = self._save_service._state["installed_roms"].get(rom_id_str, {})
        rom_filename = os.path.basename(installed.get("file_path", "")) or None
        core_so, _label = self._save_service._get_active_core(system, rom_filename)
        emulator = build_emulator_tag(core_so)

        ids_to_delete: list[int] = []

        for old_save in old_slot_saves:
            fname = old_save.get("file_name", "")
            local_file = local_by_name.get(fname)
            if local_file and os.path.isfile(local_file["path"]):
                # Upload to new slot
                await self._save_service._loop.run_in_executor(
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
            await self._save_service._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.delete_server_saves(ids_to_delete),
                ),
            )

    # ------------------------------------------------------------------
    # Slot deletion
    # ------------------------------------------------------------------

    def _validate_slot_operation(self, rom_id: int, slot: str) -> dict | tuple[str, dict, dict[str, dict]]:
        """Shared validation for slot delete operations.

        Returns an error dict on failure, or a (rom_id_str, save_state, slots_dict)
        tuple on success.
        """
        if not self._save_service._is_save_sync_enabled():
            return {"success": False, "reason": "disabled"}
        if not self._save_service._get_rom_save_info(rom_id):
            return {"success": False, "reason": "not_installed"}
        rom_id_str = str(rom_id)
        save_state = self._state_svc.data.get("saves", {}).get(rom_id_str, {})
        slots_dict: dict[str, dict] = save_state.get("slots", {})
        if slot not in slots_dict:
            return {"success": False, "reason": "not_found"}
        return rom_id_str, save_state, slots_dict

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict:
        """Return info about what deleting a slot would do, for the confirmation modal."""
        rom_id = int(rom_id)
        slot = str(slot).strip() if slot else ""

        result = self._validate_slot_operation(rom_id, slot)
        if isinstance(result, dict):
            return result
        _rom_id_str, save_state, slots_dict = result

        slot_info = slots_dict[slot]
        source = slot_info.get("source", "server")
        active_slot = save_state.get("active_slot")
        is_active = slot == (active_slot or "")

        # Server save count
        server_save_ids: list[int] = []
        if source == "server":
            device_id = self._save_service._get_server_device_id()
            try:
                server_saves: list[dict] = await self._save_service._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                    ),
                )
                server_save_ids = [s["id"] for s in server_saves]
            except Exception as e:
                self._save_service._log_debug(f"get_slot_delete_info: failed to list saves for slot '{slot}': {e}")

        # Local tracked files pointing to server saves in this slot
        files_state = save_state.get("files", {})
        local_filenames: list[str] = []
        if server_save_ids:
            id_set = set(server_save_ids)
            for filename, fstate in files_state.items():
                if fstate.get("tracked_save_id") in id_set:
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
        device_id = self._save_service._get_server_device_id()
        try:
            server_saves: list[dict] = await self._save_service._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot),
                ),
            )
            save_ids = [s["id"] for s in server_saves]
            if save_ids:
                await self._save_service._loop.run_in_executor(
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

        result = self._validate_slot_operation(rom_id, slot)
        if isinstance(result, dict):
            return result
        _rom_id_str, save_state, slots_dict = result

        effective_active = save_state.get("active_slot") or ""
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
            result = await self._delete_server_slot_saves(rom_id, slot)
            if not result["success"]:
                return result
            deleted_server_saves = result["count"]
            deleted_ids = result["ids"]

        # Clean up tracked file entries pointing to deleted saves
        files_state = save_state.get("files", {})
        if deleted_ids:
            to_remove = [fn for fn, fs in files_state.items() if fs.get("tracked_save_id") in deleted_ids]
            for fn in to_remove:
                del files_state[fn]
                cleaned_files += 1

        del slots_dict[slot]
        self._state_svc.save_state()

        return {
            "success": True,
            "deleted_server_saves": deleted_server_saves,
            "cleaned_files": cleaned_files,
        }
