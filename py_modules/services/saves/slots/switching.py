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

from domain.iso_time import parse_iso_to_epoch
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

    async def set_active_slot(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Set the active save slot for a specific game.

        If the slot doesn't exist yet (not on server), it is persisted
        as a local slot. It will be promoted to server once a save is
        uploaded to it. Owns its own read→mutate→write Unit of Work, held
        under the per-ROM lock so the flip serialises against any in-flight
        sync/status on the same ROM (see SyncEngine.rom_lock).
        """
        rom_id = int(rom_id)
        slot_str = str(slot).strip() if slot else ""
        # Empty string = legacy mode (None slot)
        resolved_slot: str | None = slot_str if slot_str else None

        async with self._sync_engine.rom_lock(rom_id):
            with self._uow_factory() as uow:
                rom_state = uow.rom_save_states.get(rom_id) or RomSaveState()
                rom_state.switch_active_slot(resolved_slot)
                uow.rom_save_states.save(rom_id, rom_state)

        # The background check re-acquires rom_lock when it runs later, so it
        # must be scheduled outside the held lock above.
        self._loop.create_task(self._status_service.check_save_status_background(rom_id))
        return {"success": True, "active_slot": resolved_slot}

    def _check_slot_switch_readiness(self, rom_id: int, save_state: RomSaveState) -> dict[str, Any]:
        """Check whether it is safe to switch slots for this ROM.

        A switch is unsafe if local files have changed since the last sync
        to the current slot — those changes would be lost.
        Files that were never synced do not block: the switch quarantines them
        into ``.romm-backup`` rather than destroying them (#965).

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

        On success the local saves dir and per-file tracking are made coherent
        with the new slot: every local file the new slot does not provide is
        quarantined into ``.romm-backup`` (never destroyed — #965) and untracked,
        the newest server save per canonical target is downloaded (#1058), and
        nothing is uploaded — saves are not carried between slots. A partial
        download failure still persists the flipped slot and returns
        ``reason="switch_incomplete"`` so the caller can retry.
        """
        rom_id = int(rom_id)

        # 1. Save sync must be enabled
        if not save_sync_enabled(self._settings):
            return {"success": False, "reason": "sync_disabled", "message": "Save sync is disabled"}

        # 2. Slot normalisation (empty → None for legacy mode)
        slot_str = str(new_slot).strip() if new_slot else ""
        resolved_slot: str | None = slot_str if slot_str else None

        # 3. ROM must be installed
        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "reason": "not_installed", "message": "ROM is not installed"}

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

        # The read→mutate→write of the RomSaveState aggregate must serialise
        # against every other path that touches this ROM's state (sync, status,
        # the other slot mutations). Hold the per-ROM lock across the whole
        # critical section — never around the tail ``get_save_status`` below,
        # which re-acquires the same non-reentrant lock (see SyncEngine.rom_lock).
        async with self._sync_engine.rom_lock(rom_id):
            save_state, device_id = await self._loop.run_in_executor(None, self._read_inputs, rom_id)

            # 4. Check for pending local changes (hashing — run in executor)
            readiness = await self._loop.run_in_executor(
                None,
                self._check_slot_switch_readiness,
                rom_id,
                save_state,
            )
            self._log_debug(f"switch_slot: rom={rom_id} new_slot={new_slot!r} readiness={readiness}")
            if not readiness.get("ready"):
                return {
                    "success": False,
                    "reason": readiness.get("reason", "pending_uploads"),
                    "message": "Pending local changes — upload or discard first",
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
                    "reason": ErrorCode.SERVER_UNREACHABLE.value,
                    "message": str(e),
                }

            # Filter to the target slot (FakeSaveApi doesn't filter, real API may not either)
            # Normalize "" and None both to None before comparing (legacy saves may use either)
            slot_saves = [s for s in all_server_saves if (s.get("slot") or None) == resolved_slot]
            self._log_debug(
                f"switch_slot: fetch rom={rom_id} resolved_slot={resolved_slot!r} "
                f"server_all={[(s.get('id'), s.get('slot')) for s in all_server_saves]} "
                f"slot_saves_ids={[s.get('id') for s in slot_saves]}"
            )

            # 6. Flip active slot in memory.
            save_state.switch_active_slot(resolved_slot)

            # 7. Make the saves dir + tracking coherent with the new slot:
            default_slot = resolve_default_slot(self._settings)
            targets = self._newest_server_saves_by_target(slot_saves, info["rom_name"])
            switch_errors = await self._loop.run_in_executor(
                None, self._apply_slot_switch, rom_id, saves_dir, system, save_state, device_id, targets, default_slot
            )

            # 8. Persist the coherent state regardless of partial download failures.
            save_state.mark_sync_evaluated(self._clock.now().isoformat())
            await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)

            if switch_errors:
                return {
                    "success": False,
                    "reason": "switch_incomplete",
                    "message": f"Switched to slot but {len(switch_errors)} save(s) failed to download — retry",
                }

        # 9. Return fresh status. MUST stay outside the lock above —
        # get_save_status re-acquires rom_lock(rom_id), which would self-deadlock.
        save_status = await self._status_service.get_save_status(rom_id)
        return {"success": True, "save_status": save_status}

    @staticmethod
    def _newest_server_saves_by_target(slot_saves: list[dict[str, Any]], rom_name: str) -> dict[str, dict[str, Any]]:
        """Pick the newest server save per canonical local target.

        Two server saves mapping to one local target (e.g. both resolve to
        ``<rom_name>.srm``) collapse to only the newest by ``updated_at``, so the
        on-disk result + ``tracked_save_id`` are deterministic rather than
        server-list-order dependent (#1058). Keyed by the canonical target
        filename the save downloads into.
        """
        newest: dict[str, dict[str, Any]] = {}
        for ss in slot_saves:
            target = local_save_target(ss, rom_name)
            current = newest.get(target)
            if current is None:
                newest[target] = ss
                continue
            if (parse_iso_to_epoch(ss.get("updated_at")) or 0.0) > (
                parse_iso_to_epoch(current.get("updated_at")) or 0.0
            ):
                newest[target] = ss
        return newest

    def _apply_slot_switch(
        self,
        rom_id: int,
        saves_dir: str,
        system: str,
        save_state: RomSaveState,
        device_id: str | None,
        targets: dict[str, dict[str, Any]],
        default_slot: str | None,
    ) -> list[str]:
        """Make the local saves dir + tracking match the new slot.

        Quarantines every local save file the new slot does not provide (and
        drops its tracking) so no stale file lingers to upload into the new slot
        (#1058) and no never-synced save is destroyed without a backup (#965),
        then downloads the newest server save per target. Mutates *save_state*
        in memory; the caller owns the write Unit of Work. Runs synchronously —
        call via ``run_in_executor``. Returns the filenames whose download
        failed (empty when all succeeded).
        """
        target_names = set(targets)
        local_now = self._rom_info.find_save_files(rom_id)
        self._log_debug(
            f"switch_slot: apply rom={rom_id} targets={list(target_names)} "
            f"local_files={[lf['filename'] for lf in local_now]} tracked={list(save_state.files)}"
        )

        # Quarantine + untrack every local file the new slot does not provide.
        for lf in local_now:
            if lf["filename"] not in target_names:
                moved = self._sync_engine.quarantine_local_file(saves_dir, lf["filename"])
                save_state.delete_file_tracking(lf["filename"])
                self._log_debug(f"switch_slot: quarantine {lf['filename']} moved={moved}")

        # Drop stale baseline entries that have no local file. Snapshot the
        # keys first — delete_file_tracking mutates save_state.files.
        stale_tracked = [fn for fn in save_state.files if fn not in target_names]
        for fn in stale_tracked:
            save_state.delete_file_tracking(fn)

        errors: list[str] = []
        for target_name, server_save in targets.items():
            self._log_debug(
                f"switch_slot: download target={target_name} save_id={server_save.get('id')} "
                f"slot={server_save.get('slot')!r}"
            )
            try:
                self._sync_engine.do_download_save(
                    server_save, saves_dir, target_name, save_state, device_id, system, default_slot
                )
            except Exception as e:
                self._log_debug(f"switch_slot: failed to download {target_name}: {e}")
                errors.append(target_name)
        return errors
