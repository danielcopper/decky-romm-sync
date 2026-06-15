"""First-sync setup wizard and slot migration.

Anything that drives the user-facing wizard for the very first time a
ROM is opened — surfacing the scenario the frontend renders, recording
the user's slot choice, and migrating server-side saves between slots
when requested — lives here. Slot listing, active-slot switching, and
slot deletion belong in their own sub-modules. Persistence is each
operation's own narrow Unit of Work (ADR-0006).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from domain.emulator_tag import build_emulator_tag
from domain.rom_save_state import RomSaveState
from domain.save_layout import SAVE_SYNC_CONTENT_DIR_REASON
from services.saves._messages import SAVE_SYNC_IN_CONTENT_DIR
from services.saves._settings import resolve_default_slot

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import (
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        SaveFileStore,
        UnitOfWorkFactory,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.sync_engine import SyncEngine


NO_MIGRATION = object()  # sentinel: no slot migration requested


class SetupWizard:
    """First-sync slot configuration: setup-info fetch, confirm-choice, slot-migration."""

    def __init__(
        self,
        *,
        settings: dict[str, Any],
        uow_factory: UnitOfWorkFactory,
        rom_info: RomInfoService,
        resolve_core: Callable[[int], str | None],
        romm_api: RommSaveApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        save_file_store: SaveFileStore,
        log_debug: DebugLogger,
        sync_engine: SyncEngine,
    ) -> None:
        self._settings = settings
        self._uow_factory = uow_factory
        self._rom_info = rom_info
        self._resolve_core = resolve_core
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._logger = logger
        self._save_file_store = save_file_store
        self._log_debug = log_debug
        self._sync_engine = sync_engine

    def _read_save_state(self, rom_id: int) -> RomSaveState | None:
        with self._uow_factory() as uow:
            return uow.rom_save_states.get(rom_id)

    def _read_device_id(self) -> str | None:
        with self._uow_factory() as uow:
            return uow.kv_config.get("device_id")

    def _write_save_state(self, rom_id: int, save_state: RomSaveState) -> None:
        with self._uow_factory() as uow:
            uow.rom_save_states.save(rom_id, save_state)

    def is_save_tracking_configured(self, rom_id: int) -> dict[str, Any]:
        """Check if save slot tracking is configured for a game.

        Fast, synchronous check — reads only from local state.
        Returns {"configured": bool, "active_slot": str|None}
        """
        rom_id = int(rom_id)
        game_state = self._read_save_state(rom_id)
        configured = bool(game_state.slot_confirmed) if game_state else False
        active_slot = game_state.active_slot if (game_state and configured) else None
        return {"configured": configured, "active_slot": active_slot}

    async def get_save_setup_info(self, rom_id: int) -> dict[str, Any]:
        """Get info needed for the first-sync setup wizard.

        Fetches server saves, checks local files, determines which
        scenario (A-E) applies so the frontend can display the right UI.
        """
        rom_id = int(rom_id)

        # Local saves
        local_files = self._rom_info.find_save_files(rom_id)
        local_file_info = []
        for lf in local_files:
            path = lf["path"]
            size = self._save_file_store.get_size(path) if self._save_file_store.is_file(path) else 0
            local_file_info.append({"filename": lf["filename"], "size": size})

        # Server saves. On failure we MUST NOT treat the empty list as
        # "server has no saves" — that path auto-confirms the default slot
        # and the first post-confirmation sync could clobber real server
        # saves the user already had. Surface a distinct recommendation so
        # the frontend can hold the wizard and offer a retry instead.
        game_state, device_id = await self._loop.run_in_executor(None, self._read_setup_inputs, rom_id)
        default_slot = resolve_default_slot(self._settings) or "default"
        try:
            server_saves: list[dict[str, Any]] = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                ),
            )
        except Exception as e:
            self._logger.warning(
                f"get_save_setup_info({rom_id}): failed to list server saves: {e}",
            )
            slot_confirmed = bool(game_state.slot_confirmed) if game_state else False
            active_slot = game_state.active_slot if (game_state and slot_confirmed) else None
            return {
                "has_local_saves": len(local_files) > 0,
                "local_files": local_file_info,
                "server_slots": [],
                "default_slot": default_slot,
                "slot_confirmed": slot_confirmed,
                "active_slot": active_slot,
                "recommended_action": "server_unreachable",
                "server_query_failed": True,
            }

        # Group server saves by slot
        slots_map: dict[str | None, list[dict[str, Any]]] = {}
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
        slot_confirmed = bool(game_state.slot_confirmed) if game_state else False
        active_slot = game_state.active_slot if (game_state and slot_confirmed) else None

        # Pre-computed wizard recommendation: auto-confirm the default slot only
        # when there are local saves and the server has no slots yet. Every other
        # combination needs the wizard so the user can choose. The
        # ``server_unreachable`` branch returns early above — reaching this point
        # means the server answered, so an empty ``server_slots`` is authoritative.
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
            "server_query_failed": False,
        }

    def _read_setup_inputs(self, rom_id: int) -> tuple[RomSaveState | None, str | None]:
        with self._uow_factory() as uow:
            state = uow.rom_save_states.get(rom_id)
            device_id = uow.kv_config.get("device_id")
        return state, device_id

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None | object = NO_MIGRATION,
    ) -> dict[str, Any]:
        """Confirm which slot to use for a game's save sync.

        Sets slot_confirmed=true and active_slot in state.

        If migrate_from_slot is provided (can be None for legacy no-slot saves),
        migrates saves: upload local files to chosen_slot, then delete old server saves.
        Pass NO_MIGRATION sentinel (the default) to skip migration.

        When a migration is requested but RetroArch writes saves to the content
        dir (#239), the migration is refused before any upload/delete (the local
        files it would carry are not under ``saves_dir``); the response carries
        ``success=False`` with ``reason="savefiles_in_content_dir"``. The slot
        confirmation itself — a non-destructive metadata flip — is still
        persisted. The non-migration path is never gated (no file write).
        """
        rom_id = int(rom_id)
        chosen_slot = str(chosen_slot).strip()
        if not chosen_slot:
            return {
                "success": False,
                "reason": "invalid_slot_name",
                "needs_conflict_resolution": False,
                "message": "Slot name cannot be empty",
            }

        # The read→confirm→(migrate)→write of the RomSaveState aggregate must
        # serialise against every other path that touches this ROM's state.
        # content_dir_blocked and _migrate_slot_saves do NOT acquire rom_lock,
        # so calling them inside the held lock is safe (no re-entry).
        async with self._sync_engine.rom_lock(rom_id):
            # Load → confirm in memory; migration I/O runs outside the txn.
            save_state = await self._loop.run_in_executor(None, self._read_save_state, rom_id) or RomSaveState()
            save_state.confirm_slot(chosen_slot)

            # Migration: re-upload local files to new slot, delete old server saves
            if migrate_from_slot is not NO_MIGRATION:
                # #239: RetroArch writes saves to the content dir — the migration
                # uploads local files read from ``saves_dir``, which holds nothing
                # in content-dir mode, so the migration could not carry real saves.
                # Refuse the migration before any upload/delete; the slot itself is
                # still confirmed in state (a non-destructive metadata flip).
                if await self._sync_engine.content_dir_blocked("confirm_slot_choice"):
                    self._log_debug(f"confirm_slot_choice: content-dir layout for rom {rom_id}; skipping migration")
                    await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                    return {
                        "success": False,
                        "reason": SAVE_SYNC_CONTENT_DIR_REASON,
                        "needs_conflict_resolution": False,
                        "message": SAVE_SYNC_IN_CONTENT_DIR,
                    }
                # migrate_from_slot can be None (legacy no-slot) or a string slot name
                from_slot: str | None = migrate_from_slot if isinstance(migrate_from_slot, str) else None
                try:
                    await self._migrate_slot_saves(rom_id, chosen_slot, from_slot)
                except Exception as e:
                    self._logger.warning(f"confirm_slot_choice({rom_id}): migration failed: {e}")
                    await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                    return {
                        "success": True,
                        "needs_conflict_resolution": False,
                        "message": f"Slot confirmed but migration failed: {e}",
                    }

            await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
            return {"success": True, "needs_conflict_resolution": False, "message": "Slot confirmed"}

    async def _migrate_slot_saves(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None,
    ) -> None:
        """Migrate server saves from one slot to another.

        For each local file: upload with new slot, then delete old server save.
        Safe order: POST first, DELETE after.
        """
        device_id = await self._loop.run_in_executor(None, self._read_device_id)

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
        core_so = await self._loop.run_in_executor(None, self._resolve_core, rom_id)
        emulator = build_emulator_tag(core_so)

        ids_to_delete: list[int] = []

        for old_save in old_slot_saves:
            fname = old_save.get("file_name", "")
            local_file = local_by_name.get(fname)
            if local_file and self._save_file_store.is_file(local_file["path"]):
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
