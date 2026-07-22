"""Copy a specific server save into another slot.

Owns the per-save "Copy to slot…" flow: it takes one server save (from a
named slot or the read-only legacy no-slot bucket) and copies its content
into a target slot, which becomes the ROM's active/confirmed slot with the
copied save as its current save. The source save is never deleted — this is
a copy, not a move. The actual file/server writes go through SyncEngine;
this class owns only the orchestration (pre-flight sync, make-current,
POST into the target). Persistence is the operation's own narrow Unit of
Work (ADR-0006).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.rom_save_sync_state import RomSaveSyncState
from domain.save_layout import SAVE_SYNC_CONTENT_DIR_REASON
from domain.save_slot import save_in_slot
from domain.save_status import compute_multi_file_slot
from lib.errors import RommConflictError
from services.saves._helpers import local_save_target
from services.saves._settings import resolve_default_slot

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import DebugLogger, RetryStrategy, RommSaveApi, UnitOfWorkFactory
    from services.saves.rom_info import RomInfoService
    from services.saves.sync_engine import SyncEngine
    from services.saves.sync_engine.devices import DeviceRegistry


@dataclass(frozen=True)
class SaveCopyServiceConfig:
    """Frozen wiring bundle handed to ``SaveCopyService.__init__``.

    Mirrors :class:`VersionsServiceConfig` — the copy flow reuses the same
    rollback-orchestration machinery (per-ROM lock, pre-flight sync, the
    download/upload workers) — minus ``save_file_store``: every file write
    routes through ``sync_engine``. Holds the live ``settings.json`` dict
    (default-slot seeding), the Unit-of-Work factory (the transactional seam
    over the SQLite repositories), the peer save sub-services consumed during
    orchestration (sync_engine, rom_info, and the shared :class:`DeviceRegistry`
    that owns the server device id), the core resolver used to stamp the upload
    emulator tag, the Protocol-typed RomM adapter and retry strategy, the plugin
    event loop, the standard-library logger, and the ``DebugLogger`` seam.
    """

    settings: dict[str, Any]
    uow_factory: UnitOfWorkFactory
    sync_engine: SyncEngine
    device_registry: DeviceRegistry
    rom_info: RomInfoService
    resolve_core: Callable[[int], str | None]
    romm_api: RommSaveApi
    retry: RetryStrategy
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    log_debug: DebugLogger


class SaveCopyService:
    """Aggregate root for the copy-save-to-slot flow.

    Per-ROM lock acquisition is delegated to the injected ``SyncEngine``;
    save-state persistence is the operation's own narrow Unit of Work. This
    class owns the copy orchestration on top of them.
    """

    def __init__(self, *, config: SaveCopyServiceConfig) -> None:
        self._config = config
        self._settings = config.settings
        self._uow_factory = config.uow_factory
        self._sync_engine = config.sync_engine
        self._device_registry = config.device_registry
        self._rom_info = config.rom_info
        self._resolve_core = config.resolve_core
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._loop = config.loop
        self._logger = config.logger
        self._log_debug = config.log_debug

    # ------------------------------------------------------------------
    # Narrow-UoW read/write helpers (ADR-0006)
    # ------------------------------------------------------------------

    def _read_inputs(self, rom_id: int) -> tuple[RomSaveSyncState, str | None]:
        with self._uow_factory() as uow:
            state = uow.rom_save_sync_states.get(rom_id) or RomSaveSyncState()
        return state, self._device_registry.get_device_id()

    def _write_save_state(self, rom_id: int, save_state: RomSaveSyncState) -> None:
        with self._uow_factory() as uow:
            uow.rom_save_sync_states.save(rom_id, save_state)

    def _local_component_filenames(self, rom_id: int) -> list[str]:
        """Distinct local save filenames on disk for the ROM (one per extension).

        More than one distinct filename means the current slot's save is an
        N-file set (e.g. Saturn ``.bkr``/``.bcr``/``.smpc``), not a single file
        — the interim #908 guard refuses a copy in that case. Reads only the
        local saves directory, no network: a copy target is always an
        *installed* ROM. Returns an empty list when the ROM is not installed.
        """
        return [lf["filename"] for lf in self._rom_info.find_save_files(rom_id)]

    @staticmethod
    def _find_already_present(server_saves: list[dict[str, Any]], save_id: int, target_slot: str) -> int | None:
        """The id of a save in *target_slot* whose content matches *save_id*, or None.

        Compares the chosen save's ``content_hash`` (RomM's own digest of the
        bytes) against every save already in the target slot. Returns the first
        match's id — meaning the copy would be a content-identical no-op that
        RomM would dedup server-side. Returns None when the chosen save is absent
        (a later ``version_deleted``), carries no ``content_hash`` (older servers;
        the copy falls through to RomM's own dedup), or has no content twin in the
        slot.
        """
        source = next((s for s in server_saves if s.get("id") == save_id), None)
        if source is None:
            return None
        source_hash = source.get("content_hash")
        if not source_hash:
            return None
        match = next(
            (s for s in server_saves if save_in_slot(s, target_slot) and s.get("content_hash") == source_hash),
            None,
        )
        return match.get("id") if match is not None else None

    # ------------------------------------------------------------------
    # Copy-to-slot API
    # ------------------------------------------------------------------

    def _copy_save_to_slot_io(
        self,
        rom_id: int,
        save_state: RomSaveSyncState,
        device_id: str | None,
        core_so: str | None,
        save_id: int,
        target_slot: str,
        info: dict[str, Any],
        server_saves: list[dict[str, Any]],
        default_slot: str,
    ) -> dict[str, Any]:
        """Blocking I/O portion of the copy flow — runs in executor.

        By the time this runs the caller has already brought the *current* slot
        in sync (the matrix pre-flight) so no dirty local is lost when the
        download below overwrites the canonical local file. This function is the
        copy proper:

        1. Locate the chosen save in the (unfiltered) server list; a missing id
           is ``version_deleted``.
        2. Make the target the ROM's active/confirmed slot
           (:meth:`RomSaveSyncState.confirm_slot`) — the upload resolves its slot
           purely from ``active_slot`` (``_resolve_upload_slot``), so this must
           precede the POST for the copy to land in the target.
        3. Download the chosen save's content onto the canonical local file
           (quarantines the current local file first, #965).
        4. POST that content into the now-active target slot
           (``server_save=None`` → a new save, ``overwrite=false`` → the 409
           backstop guards a busy target). A 409 is ``target_slot_busy`` (the
           target has newer changes from another device — resolve it first); any
           other upload error is ``copy_failed``.

        The source save is never touched. Mutates *save_state* in memory; the
        caller owns the write UoW.
        """
        target_save = next((s for s in server_saves if s.get("id") == save_id), None)
        if target_save is None:
            return {"status": "version_deleted"}

        saves_dir = info["saves_dir"]
        system = info["system"]
        rom_name = info["rom_name"]
        canonical = local_save_target(target_save, rom_name)
        local_path = os.path.join(saves_dir, canonical)

        # Make the target current BEFORE the upload: do_upload_save resolves the
        # upload slot from save_state.active_slot with no per-call override, so
        # the POST only lands in the target once it is the confirmed active slot.
        save_state.confirm_slot(target_slot)

        # Bring the chosen save's content down onto the canonical local file. The
        # existing local file (the current slot's save) is quarantined into
        # ``.romm-backup`` first, so nothing that lives only on disk is lost.
        self._sync_engine.do_download_save(
            target_save, saves_dir, canonical, save_state, device_id, system, default_slot
        )

        try:
            self._sync_engine.do_upload_save(
                rom_id,
                local_path,
                canonical,
                save_state,
                device_id,
                system,
                core_so,
                server_save=None,
                default_slot=default_slot,
                overwrite=False,
            )
        except RommConflictError as e:
            # The target slot has a newer server version this device hasn't
            # synced — an overwrite=false POST 409s rather than stack on it.
            # Surface it so the user resolves/syncs the target first, then retries.
            self._logger.warning(
                "_copy_save_to_slot_io: target slot %r busy for rom=%s save=%s: %s",
                target_slot,
                rom_id,
                save_id,
                e,
            )
            return {"status": "target_slot_busy", "message": str(e)}
        except Exception as e:
            self._logger.error(
                "_copy_save_to_slot_io: upload into slot %r failed for rom=%s save=%s: %s",
                target_slot,
                rom_id,
                save_id,
                e,
            )
            return {"status": "copy_failed", "message": str(e)}

        return {"status": "ok"}

    async def copy_save_to_slot(self, rom_id: int, save_id: int, target_slot: str) -> dict[str, Any]:
        """Copy one server save into *target_slot*, making it the active slot.

        The source save (``save_id``, from any slot including the legacy no-slot
        bucket) is copied — never moved or deleted — into ``target_slot``, which
        becomes the ROM's active/confirmed slot with the copied save as its
        current save.

        Flow:

        1. Validate ``target_slot`` (strip; empty/whitespace/None →
           ``invalid_slot_name``).
        2. Acquire ``rom_lock`` — every ``RomSaveSyncState`` read-mutate-write
           runs under it.
        3. Refuse an unconfigured ROM (``active_slot`` None / slot not confirmed)
           with ``not_configured``. The Copy action is only reachable on a
           configured ROM; this is the defensive backstop, not a UI-gate reliance.
        4. ``rom_not_installed`` when the ROM has no install record.
        5. Multi-file (#908) and content-dir (#239) layouts are ``unsupported``.
        6. Pre-flight ``do_sync_rom_saves`` on the current (confirmed) slot,
           unconditionally, to protect its dirty local before the copy overwrites
           it — ``conflict_blocked`` / ``preflight_failed`` on a bad pre-flight.
        7. ``list_saves`` with no slot filter (the source may be in any slot);
           failure is ``server_unreachable``.
        8. The copy proper runs in :meth:`_copy_save_to_slot_io`.

        A dedup pre-check between the ``list_saves`` and the copy short-circuits
        with ``already_present{existing_id}`` when the chosen save's content is
        already in the target slot — copying it would only churn the tracked save
        (RomM dedups server-side).

        Returns a discriminated-status dict (mirrors ``RollbackStatus``):
        ``ok | already_present{existing_id} | not_configured | invalid_slot_name |
        rom_not_installed | version_deleted | unsupported{reason?} |
        server_unreachable{message} | conflict_blocked{conflicts} |
        preflight_failed{errors} | target_slot_busy{message} | copy_failed{message}``.
        """
        rom_id = int(rom_id)
        save_id = int(save_id)

        target_slot = str(target_slot).strip()
        if not target_slot:
            return {"status": "invalid_slot_name"}

        async with self._sync_engine.rom_lock(rom_id):
            save_state, device_id = await self._loop.run_in_executor(None, self._read_inputs, rom_id)

            # Configured ROMs only: the current slot must be a confirmed named
            # slot. An unconfirmed slot, ``active_slot`` None, or the legacy
            # no-slot mode ("") is refused — the copy would otherwise land its
            # pre-flight/upload without a real source-slot context (#1529 lesson).
            if not (save_state.slot_confirmed and save_state.active_slot):
                self._log_debug(f"copy_save_to_slot: rom {rom_id} not configured; refusing")
                return {"status": "not_configured"}

            info = self._rom_info.get_rom_save_info(rom_id)
            if not info:
                return {"status": "rom_not_installed"}

            # Interim #908 guard: a multi-file slot (e.g. Saturn .bkr/.bcr/.smpc)
            # is one game state across N files — copying a single save into a slot
            # would produce an incoherent set. Local-only (no network): a copy
            # source is always installed, so its component files are on disk.
            component_files = await self._loop.run_in_executor(None, self._local_component_filenames, rom_id)
            if compute_multi_file_slot(component_files).is_multi_file:
                self._log_debug(f"copy_save_to_slot: multi-file slot for rom {rom_id} ({component_files}); refusing")
                return {"status": "unsupported"}

            # #239: RetroArch writes saves to the content dir — the copy's
            # download/POST target is ``saves_dir``, which RetroArch ignores in
            # that layout, so the copy could never take effect. Refuse before any
            # preflight or destructive I/O.
            if await self._sync_engine.content_dir_blocked("copy_save_to_slot"):
                self._log_debug(f"copy_save_to_slot: content-dir layout for rom {rom_id}; refusing")
                return {"status": "unsupported", "reason": SAVE_SYNC_CONTENT_DIR_REASON}

            core_so = await self._loop.run_in_executor(None, self._resolve_core, rom_id)
            default_slot = resolve_default_slot(self._settings)

            # Matrix pre-flight on the CURRENT slot: get its tracked save in sync
            # first (or surface a conflict), so the download below never overwrites
            # a dirty local that lives nowhere else.
            _uploaded, _downloaded, errors, conflicts = await self._loop.run_in_executor(
                None, self._sync_engine.do_sync_rom_saves, rom_id, save_state, device_id, core_so, default_slot
            )
            if conflicts:
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {"status": "conflict_blocked", "conflicts": list(conflicts)}
            if errors:
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {"status": "preflight_failed", "errors": errors}

            # No slot filter: the chosen save may live in ANY slot (a named slot or
            # the legacy no-slot bucket), so list everything and find it by id.
            try:
                server_saves: list[dict[str, Any]] = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id)),
                )
            except Exception as e:
                self._log_debug(f"copy_save_to_slot: failed to list saves: {e}")
                # Persist whatever the pre-flight mutated before bailing.
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {"status": "server_unreachable", "message": str(e)}

            # Dedup pre-check: if the chosen save's content is already present in
            # the target slot, a copy would make RomM dedup server-side and
            # do_upload_save would adopt the pre-existing save as current —
            # churning the tracked save for no gain (which surfaced the current
            # save in its own version history). Detect it up front via the
            # content_hash RomM populates on each save and refuse without touching
            # any copy state. A missing source content_hash skips the check; the
            # copy then falls through and RomM's own server-side dedup applies.
            existing_id = self._find_already_present(server_saves, save_id, target_slot)
            if existing_id is not None:
                self._log_debug(
                    f"copy_save_to_slot: rom {rom_id} save {save_id} already present in slot "
                    f"{target_slot!r} as {existing_id}; skipping copy"
                )
                # The pre-flight may have performed real uploads/downloads on the
                # current slot; persist those baselines even though the copy is a
                # no-op (#1012). No copy state was mutated.
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {"status": "already_present", "existing_id": existing_id}

            try:
                result = await self._loop.run_in_executor(
                    None,
                    self._copy_save_to_slot_io,
                    rom_id,
                    save_state,
                    device_id,
                    core_so,
                    save_id,
                    target_slot,
                    info,
                    server_saves,
                    default_slot,
                )
            finally:
                # The pre-flight (and the copy's own download/upload) mutate the
                # in-memory aggregate; persist regardless of how the copy ends or
                # the next sync mis-classifies (#1012).
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)

            return result
