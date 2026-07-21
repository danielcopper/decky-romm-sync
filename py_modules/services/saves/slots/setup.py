"""First-sync setup wizard and slot migration.

Anything that drives the user-facing wizard for the very first time a
ROM is opened — surfacing the scenario the frontend renders, recording
the user's slot choice, and migrating server-side saves between slots
when requested — lives here. Slot listing, active-slot switching, and
slot deletion belong in their own sub-modules. Persistence is each
operation's own narrow Unit of Work (ADR-0006).
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any

from domain.iso_time import epoch_to_iso, parse_iso_to_epoch
from domain.rom_save_sync_state import RomSaveSyncState
from domain.save_layout import SAVE_SYNC_CONTENT_DIR_REASON
from domain.save_slot import save_in_slot
from lib.errors import classify_error
from services.saves._helpers import newest_server_saves_by_target
from services.saves._messages import (
    DEVICE_NOT_REGISTERED_REASON,
    MIGRATION_DEVICE_NOT_REGISTERED,
    SAVE_SYNC_IN_CONTENT_DIR,
)
from services.saves._settings import autocleanup_limit, resolve_default_slot

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
    from services.saves.sync_engine.devices import DeviceRegistry


class SetupWizard:
    """First-sync slot configuration: setup-info fetch, confirm-choice, slot-migration."""

    def __init__(
        self,
        *,
        settings: dict[str, Any],
        uow_factory: UnitOfWorkFactory,
        device_registry: DeviceRegistry,
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
        self._device_registry = device_registry
        self._rom_info = rom_info
        self._resolve_core = resolve_core
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._logger = logger
        self._save_file_store = save_file_store
        self._log_debug = log_debug
        self._sync_engine = sync_engine

    def _read_save_state(self, rom_id: int) -> RomSaveSyncState | None:
        with self._uow_factory() as uow:
            return uow.rom_save_sync_states.get(rom_id)

    def _write_save_state(self, rom_id: int, save_state: RomSaveSyncState) -> None:
        with self._uow_factory() as uow:
            uow.rom_save_sync_states.save(rom_id, save_state)

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
        default_slot = resolve_default_slot(self._settings)
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
            latest = max(
                (s.get("updated_at", "") for s in saves),
                key=lambda u: parse_iso_to_epoch(u) or 0.0,
                default=None,
            )
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

    def _read_setup_inputs(self, rom_id: int) -> tuple[RomSaveSyncState | None, str | None]:
        with self._uow_factory() as uow:
            state = uow.rom_save_sync_states.get(rom_id)
        return state, self._device_registry.get_device_id()

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str | None,
        migrate: bool = False,
        migrate_from_slot: str | None = None,
        use_server_on_conflict: bool = False,
    ) -> dict[str, Any]:
        """Confirm which slot to use for a game's save sync.

        Sets slot_confirmed=true and active_slot in state.

        ``chosen_slot`` must be a non-empty named slot. ``None`` and a string
        that strips to ``""`` are both rejected as an invalid slot name: the
        legacy no-slot mode can no longer be confirmed as a target (#1276) — it
        survives only as a migration *source*.

        When ``migrate`` is true, the newest legacy (``migrate_from_slot``,
        ``None`` = the legacy no-slot source) server save per canonical local
        target is downloaded and copied into ``chosen_slot`` under the canonical
        name — content-based, independent of the legacy row's own filename or of
        any local file (#1498). The per-target collision matrix:

        - **No local file** or a **byte-identical** local file → migrated
          silently (content copied into the slot, baseline adopted).
        - A **differing** local file → held for the user's decision unless
          ``use_server_on_conflict`` is set. Without it, the slot is *not*
          confirmed and the response carries ``needs_conflict_resolution=True`` +
          a ``conflicts`` list (both sides' timestamp/size) so the wizard can ask.
          With it, the differing local file is quarantined into ``.romm-backup``
          (never deleted, #965) before the server content replaces it.

        The legacy source saves are never deleted — a migration copies their
        content into the slot and leaves the sources in the read-only legacy
        bucket (#1478).

        When a migration is requested but RetroArch writes saves to the content
        dir (#239), the migration is refused before any download; the slot
        confirmation itself — a non-destructive metadata flip — is still
        persisted (``reason="savefiles_in_content_dir"``). The non-migration path
        is never gated (no file write).
        """
        rom_id = int(rom_id)
        # Legacy ``slot:null`` confirmation is retired (#1276): a slot must carry
        # a non-empty name. ``None`` is rejected outright — the aggregate's
        # ``confirm_slot`` would raise on it, so we guard before the call — and a
        # string that strips to ``""`` is rejected the same way. The legacy
        # no-slot mode can no longer be confirmed as a target.
        if chosen_slot is None:
            return {
                "success": False,
                "reason": "invalid_slot_name",
                "needs_conflict_resolution": False,
                "message": "Slot name cannot be empty",
            }
        normalized_slot = str(chosen_slot).strip()
        if not normalized_slot:
            return {
                "success": False,
                "reason": "invalid_slot_name",
                "needs_conflict_resolution": False,
                "message": "Slot name cannot be empty",
            }

        # The read→confirm→(migrate)→write of the RomSaveSyncState aggregate must
        # serialise against every other path that touches this ROM's state.
        # content_dir_blocked and _migrate_slot_saves_io do NOT acquire rom_lock,
        # so calling them inside the held lock is safe (no re-entry).
        async with self._sync_engine.rom_lock(rom_id):
            save_state = await self._loop.run_in_executor(None, self._read_save_state, rom_id) or RomSaveSyncState()

            # Non-migration path: a plain, non-destructive metadata flip.
            if not migrate:
                save_state.confirm_slot(normalized_slot)
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {"success": True, "needs_conflict_resolution": False, "message": "Slot confirmed"}

            # #239: RetroArch writes saves to the content dir — the migration
            # would write into ``saves_dir``, which RetroArch ignores in that
            # layout. Refuse before any download; the slot itself is still
            # confirmed (a non-destructive metadata flip).
            if await self._sync_engine.content_dir_blocked("confirm_slot_choice"):
                self._log_debug(f"confirm_slot_choice: content-dir layout for rom {rom_id}; skipping migration")
                save_state.confirm_slot(normalized_slot)
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {
                    "success": False,
                    "reason": SAVE_SYNC_CONTENT_DIR_REASON,
                    "needs_conflict_resolution": False,
                    "message": SAVE_SYNC_IN_CONTENT_DIR,
                }

            # Migration preconditions, checked BEFORE any confirm or mutation so
            # a precondition failure holds the wizard open (no half-state) and the
            # user can retry Track (#1498 review). A missing device would only
            # surface as the #1478 upload guard AFTER local files were already
            # touched, so it must be caught here first.
            device_id = await self._loop.run_in_executor(None, self._device_registry.get_device_id)
            if not device_id:
                return {
                    "success": False,
                    "reason": DEVICE_NOT_REGISTERED_REASON,
                    "needs_conflict_resolution": False,
                    "message": MIGRATION_DEVICE_NOT_REGISTERED,
                }
            info = await self._loop.run_in_executor(None, self._rom_info.get_rom_save_info, rom_id)
            if not info:
                return {
                    "success": False,
                    "reason": "not_installed",
                    "needs_conflict_resolution": False,
                    "message": "ROM is not installed",
                }

            # Confirm in memory so the migration uploads resolve to the chosen
            # slot, but persist only once the migration reaches the apply phase
            # without an unanswered local-file conflict.
            save_state.confirm_slot(normalized_slot)
            try:
                outcome = await self._loop.run_in_executor(
                    None,
                    self._migrate_slot_saves_io,
                    rom_id,
                    migrate_from_slot,
                    use_server_on_conflict,
                    save_state,
                    device_id,
                    info,
                )
            except Exception as e:
                # Wholesale failure BEFORE the apply phase (list_saves or a phase-1
                # download threw) — nothing durable was mutated (only scratch
                # temps, cleaned up). Do NOT confirm: return the canonical failure
                # so the wizard stays open on the message and Track can be retried.
                reason, message = classify_error(e)
                self._logger.warning(f"confirm_slot_choice({rom_id}): migration failed before apply: {e}")
                return {
                    "success": False,
                    "reason": reason,
                    "needs_conflict_resolution": False,
                    "message": message,
                }

            if outcome["status"] == "conflict":
                # A local save differs — hold for the user. Do NOT persist the
                # confirm; the wizard re-calls (keep-local → migrate=false;
                # use-server → use_server_on_conflict=true).
                return {
                    "success": False,
                    "needs_conflict_resolution": True,
                    "reason": "local_conflict",
                    "message": f"A local save differs from the legacy save for slot '{normalized_slot}'",
                    "conflicts": outcome["conflicts"],
                }

            # ``no_op`` (server had no legacy saves) and ``migrated`` (the apply
            # phase ran) both confirm the slot. A partial per-target failure in
            # the apply phase is counted, not fatal (confirm-with-warning).
            await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
            if outcome["status"] == "no_op":
                return {
                    "success": True,
                    "needs_conflict_resolution": False,
                    "message": f"Slot '{normalized_slot}' confirmed",
                    "migrated": 0,
                    "failed": 0,
                }
            return {
                "success": True,
                "needs_conflict_resolution": False,
                "message": f"Migrated {outcome['migrated']} save(s) into '{normalized_slot}'",
                "migrated": outcome["migrated"],
                "failed": outcome["failed"],
            }

    def _migrate_slot_saves_io(
        self,
        rom_id: int,
        migrate_from_slot: str | None,
        use_server_on_conflict: bool,
        save_state: RomSaveSyncState,
        device_id: str,
        info: dict[str, Any],
    ) -> dict[str, Any]:
        """Copy the newest legacy save per canonical target into the confirmed slot.

        Synchronous worker (run via ``run_in_executor``) — acquires no
        ``rom_lock``, so it is safe under the lock ``confirm_slot_choice`` holds.
        ``device_id`` and ``info`` are the caller's already-validated
        preconditions. Content-based: for each canonical target it downloads the
        newest legacy save's content to a sibling temp, classifies it against the
        local file (``no_local`` / ``identical`` / ``differs``), and either copies
        it into the slot — adopting the per-file baseline via
        :meth:`SyncEngine.do_upload_save`, which uploads to the slot *save_state*
        was just confirmed with — or, for a differing local file with
        ``use_server_on_conflict`` unset, records a conflict for the wizard. The
        legacy source saves are never deleted; they stay in the read-only legacy
        bucket (#1478).

        Returns a discriminated result: ``{"status": "no_op"}`` (server had no
        legacy saves), ``{"status": "conflict", "conflicts": [...]}`` (a differing
        local save needs the user's decision), or ``{"status": "migrated",
        "migrated": int, "failed": int}`` (the apply phase ran). The **wholesale**
        pre-apply failures — a ``list_saves`` throw or a phase-1 download throw —
        **propagate** so the caller returns a canonical failure without
        confirming: phase 1 writes only scratch ``.tmp`` files (never the real
        save files) and the ``finally`` clears them, so nothing durable is
        mutated before the apply phase begins.
        """
        rom_name = info["rom_name"]
        saves_dir = info["saves_dir"]
        system = info["system"]

        # The legacy source (None/"") can't be addressed by ``slot=`` (RomM
        # stores it as null), so list ALL saves and filter client-side via
        # save_in_slot (#1061). A throw here propagates → wholesale failure.
        all_saves = self._retry.with_retry(
            lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
        )
        legacy_saves = [s for s in all_saves if save_in_slot(s, migrate_from_slot)]
        if not legacy_saves:
            return {"status": "no_op"}

        targets = newest_server_saves_by_target(legacy_saves, rom_name)
        core_so = self._resolve_core(rom_id)
        default_slot = resolve_default_slot(self._settings)
        cleanup_limit = autocleanup_limit(self._settings)
        self._save_file_store.make_dirs(saves_dir)

        # Every ``.tmp`` created below is scratch cleaned up on ANY exit: a
        # mid-phase-1 download throw, a conflict hold, or the apply phase (whose
        # rename/remove already consumes each temp, leaving the cleanup a no-op).
        created_temps: list[str] = []
        try:
            # Phase 1 — download + classify every target before touching any local
            # file, so an unanswered conflict (or a download throw) holds without
            # having migrated anything.
            plans: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
            for target, server_save in targets.items():
                local_path = os.path.join(saves_dir, target)
                tmp_path = local_path + ".tmp"
                created_temps.append(tmp_path)
                self._retry.with_retry(
                    lambda sid=server_save["id"], tp=tmp_path: self._romm_api.download_save(sid, tp),
                )
                server_hash = self._save_file_store.content_hash(tmp_path)
                if not self._save_file_store.is_file(local_path):
                    kind = "no_local"
                elif self._save_file_store.content_hash(local_path) == server_hash:
                    kind = "identical"
                else:
                    kind = "differs"
                plans.append({"target": target, "local_path": local_path, "tmp_path": tmp_path, "kind": kind})
                if kind == "differs" and not use_server_on_conflict:
                    conflicts.append(self._build_migration_conflict(target, server_save, local_path))

            if conflicts:
                # Hold for the user — migrate nothing (temps cleared in finally).
                return {"status": "conflict", "conflicts": conflicts}

            # Phase 2 — apply. From here mutations begin; a per-target failure is
            # counted, not fatal: the slot is still confirmed by the caller and the
            # failed legacy source is left in place (never deleted), so no data
            # that lives only there is lost.
            migrated = 0
            failed = 0
            for plan in plans:
                try:
                    self._apply_migration_plan(
                        plan, rom_id, save_state, device_id, saves_dir, system, core_so, default_slot, cleanup_limit
                    )
                    migrated += 1
                except Exception as e:
                    self._logger.warning(
                        f"_migrate_slot_saves_io({rom_id}): failed to migrate {plan['target']}: {e}",
                    )
                    failed += 1
            return {"status": "migrated", "migrated": migrated, "failed": failed}
        finally:
            for tmp_path in created_temps:
                with contextlib.suppress(OSError):
                    self._save_file_store.remove_file(tmp_path)

    def _apply_migration_plan(
        self,
        plan: dict[str, Any],
        rom_id: int,
        save_state: RomSaveSyncState,
        device_id: str | None,
        saves_dir: str,
        system: str,
        core_so: str | None,
        default_slot: str | None,
        cleanup_limit: int | None,
    ) -> None:
        """Place one target's server content locally and upload it into the slot.

        ``identical`` keeps the local file (the temp is redundant); ``no_local``
        moves the downloaded content into place; ``differs`` — reached only with
        ``use_server_on_conflict`` — quarantines the local file first (#965) then
        replaces it. The final upload copies the local file into the confirmed
        slot and adopts the per-file baseline (:meth:`SyncEngine.do_upload_save`).
        """
        target = plan["target"]
        local_path = plan["local_path"]
        tmp_path = plan["tmp_path"]
        kind = plan["kind"]

        if kind == "identical":
            with contextlib.suppress(OSError):
                self._save_file_store.remove_file(tmp_path)
        else:
            if kind == "differs":
                self._sync_engine.quarantine_local_file(saves_dir, target)
            self._save_file_store.rename(tmp_path, local_path)

        self._sync_engine.do_upload_save(
            rom_id,
            local_path,
            target,
            save_state,
            device_id,
            system,
            core_so,
            default_slot=default_slot,
            autocleanup_limit=cleanup_limit,
        )

    def _build_migration_conflict(
        self,
        target: str,
        server_save: dict[str, Any],
        local_path: str,
    ) -> dict[str, Any]:
        """Describe a legacy-vs-local collision for the wizard's resolution dialog.

        Carries both sides' timestamp/size so the wizard can render them: the
        server side from the legacy save row, the local side from the on-disk
        file.
        """
        return {
            "filename": target,
            "server_save_id": server_save.get("id"),
            "server_updated_at": server_save.get("updated_at", ""),
            "server_size": server_save.get("file_size_bytes"),
            "local_mtime": epoch_to_iso(self._save_file_store.get_mtime(local_path)),
            "local_size": self._save_file_store.get_size(local_path),
        }
