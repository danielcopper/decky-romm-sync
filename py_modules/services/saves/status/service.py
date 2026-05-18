from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from models.state import PluginState

from domain.emulator_tag import detect_core_change
from domain.save_attribution import compute_uploaded_by_us
from domain.save_status import compute_save_sync_display
from domain.save_status_builders import (
    build_file_status,
    resolve_chosen_server,
    status_from_action,
)
from domain.sync_action import Conflict, Skip
from lib.iso_time import parse_iso_to_epoch

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        CoreResolverFn,
        DebugLogger,
        EventEmitter,
        RetryStrategy,
        RommSaveApi,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.state import StateService
    from services.saves.sync_engine import MatrixOutcome, SyncEngine


@dataclass(frozen=True)
class StatusServiceConfig:
    """Frozen wiring bundle handed to ``StatusService.__init__``.

    Holds the main plugin state dict, the peer save sub-services
    (state, sync_engine, rom_info), the Protocol-typed RomM adapter
    and retry strategy, the plugin event loop, the standard-library
    logger, the ``DebugLogger`` seam, the ES-DE core resolver, and
    the event emitter used to push background status updates to the
    frontend.
    """

    state: PluginState
    state_svc: StateService
    sync_engine: SyncEngine
    rom_info: RomInfoService
    romm_api: RommSaveApi
    retry: RetryStrategy
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    log_debug: DebugLogger
    get_active_core: CoreResolverFn
    emit: EventEmitter


class StatusService:
    """Read-only matrix-driven status reporting for the SAVES tab."""

    def __init__(self, *, config: StatusServiceConfig) -> None:
        self._config = config
        self._state = config.state
        self._state_svc = config.state_svc
        self._sync_engine = config.sync_engine
        self._rom_info = config.rom_info
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._loop = config.loop
        self._logger = config.logger
        self._log_debug = config.log_debug
        self._get_active_core = config.get_active_core
        self._emit = config.emit

    def _status_entry_from_outcome(
        self,
        outcome: MatrixOutcome,
        *,
        rom_id: int,
        server_device_id: str | None,
        own_upload_ids: list[int] | None,
    ) -> tuple[dict, dict | None]:
        """Build the status DTO + optional conflict descriptor for one outcome.

        Returns ``(status_entry, conflict_entry_or_None)``. The conflict
        entry is the ``sync_conflict`` descriptor when the matrix returned
        ``Conflict``; otherwise ``None``.
        """
        action = outcome.action
        chosen_server = resolve_chosen_server(action, outcome.server_candidates)
        status_entry = build_file_status(
            outcome.filename,
            local_path=outcome.local_path,
            local_hash=outcome.local_hash,
            local_mtime=outcome.local_mtime_iso,
            local_size=outcome.local_size,
            server=chosen_server,
            last_sync_at=outcome.file_state.last_sync_at or None,
            status=status_from_action(action),
            server_device_id=server_device_id,
            uploaded_by_us=compute_uploaded_by_us(chosen_server, own_upload_ids),
        )
        conflict_entry: dict | None = None
        if isinstance(action, Conflict):
            self._log_debug(
                f"_get_save_status_io({rom_id}): conflict {outcome.filename} "
                f"server_save_id={action.server_save.get('id')}"
            )
            conflict_entry = self._sync_engine._build_sync_conflict_entry(
                rom_id, outcome.filename, action.server_save, outcome.local_path, outcome.local_hash
            )
        return status_entry, conflict_entry

    def _partition_outcomes(
        self,
        rom_id: int,
        rom_id_str: str,
        server_in_slot: list[dict],
        info: dict,
    ) -> tuple[MatrixOutcome | None, list[MatrixOutcome]]:
        """Iterate matrix outcomes for the active slot, splitting them into local/server-only buckets.

        Side effect: when an outcome is ``Skip(adopt_baseline=True)`` with
        a local hash, records that hash as the new sync baseline via the
        sync engine.

        Returns ``(first_local_outcome, server_only_outcomes)``. The local
        bucket is the first outcome with a local file present — matching
        the active-slot single-entry view this status flow surfaces.
        """
        local_outcome: MatrixOutcome | None = None
        server_only_outcomes: list[MatrixOutcome] = []
        for outcome in self._sync_engine.iter_matrix_outcomes(rom_id, server_in_slot, info=info):
            if isinstance(outcome.action, Skip) and outcome.action.adopt_baseline and outcome.local_hash:
                self._sync_engine._adopt_baseline_hash(rom_id_str, outcome.filename, outcome.local_hash)
            if outcome.local_path is None:
                server_only_outcomes.append(outcome)
            elif local_outcome is None:
                local_outcome = outcome
        return local_outcome, server_only_outcomes

    def _get_save_status_io(
        self,
        rom_id: int,
        server_saves: list[dict],
        *,
        server_query_failed: bool = False,
    ) -> dict:
        """Sync helper for get_save_status — runs in executor.

        Builds the saves-tab status for one ROM as a single-entry view of
        the active slot:

        - Local file present: run ``compute_sync_action`` and surface the
          resulting status, server attribution, and any conflict.
        - No local file but the slot has server saves: surface the newest
          server save as "ready to download". The canonical local target
          is ``<rom_name>.<server.file_extension>`` — derived purely from
          RetroArch's view of the ROM.
        - ROM not installed (no rom_name available) → no entry. There is
          no server-derived filename fallback: without a deterministic
          local path we cannot tell the user where a download would land.
        - Empty slot → no entry.

        Older versions of the same slot are reachable via the lazy-fetched
        ``Previous Versions`` dropdown (``list_file_versions``).

        The one allowed mutation is recording an adopted baseline hash when
        the action requests it (``Skip(adopt_baseline=True)``) — pure state
        hygiene, no network traffic.

        When *server_query_failed* is True the caller's ``list_saves`` call
        raised before *server_saves* was populated. Matrix evaluation runs
        as usual against the empty list (so local-file rows still appear
        with paths/sizes/hashes), but each resulting status is rewritten
        to ``"unknown"`` and server-side fields are nulled out — the empty
        list classifies every local save as Upload, which would surface a
        misleading "ready to upload" indicator on what is in fact a
        connectivity blip.
        """
        rom_id_str = str(rom_id)
        info = self._rom_info.get_rom_save_info(rom_id)
        server_device_id = self._state_svc.get_server_device_id()

        save_state = self._state_svc.state.saves.get(rom_id_str)
        active_slot = save_state.active_slot if save_state else None
        server_in_slot = self._sync_engine._filter_server_saves_to_slot(server_saves, active_slot)

        own_upload_ids: list[int] | None = save_state.own_upload_ids if save_state else None

        file_statuses: list[dict] = []
        conflicts: list[dict] = []

        if info is not None:
            local_outcome, server_only_outcomes = self._partition_outcomes(rom_id, rom_id_str, server_in_slot, info)

            chosen = local_outcome
            if chosen is None and server_only_outcomes:
                chosen = max(server_only_outcomes, key=_outcome_server_sort_key)

            if chosen is not None:
                status_entry, conflict_entry = self._status_entry_from_outcome(
                    chosen,
                    rom_id=rom_id,
                    server_device_id=server_device_id,
                    own_upload_ids=own_upload_ids,
                )
                if server_query_failed:
                    status_entry = _redact_server_fields(status_entry)
                    conflict_entry = None
                file_statuses.append(status_entry)
                if conflict_entry is not None:
                    conflicts.append(conflict_entry)

        playtime_entry = self._state_svc.state.playtime.get(rom_id_str)
        playtime = playtime_entry.to_dict() if playtime_entry is not None else {}
        save_entry = self._state_svc.state.saves.get(rom_id_str)
        last_sync_check_at = save_entry.last_sync_check_at if save_entry else None

        return {
            "rom_id": rom_id,
            "files": file_statuses,
            "playtime": playtime,
            "device_id": self._state_svc.state.device_id or "",
            "last_sync_check_at": last_sync_check_at,
            "conflicts": conflicts,
            "save_sort_changed": self._rom_info.is_save_sort_changed(),
            "save_sync_display": asdict(
                compute_save_sync_display(
                    file_statuses,
                    last_sync_check_at,
                    server_query_failed=server_query_failed,
                )
            ),
            "server_query_failed": server_query_failed,
        }

    # ------------------------------------------------------------------
    # Public callable surface — invoked via the SaveService aggregate root
    # ------------------------------------------------------------------

    async def get_save_status(self, rom_id: int) -> dict:
        """Get save sync status for a ROM (local files, server saves, conflict state).

        When the ``list_saves`` call raises (transient network blip, server
        offline, …) the returned dict carries ``server_query_failed: True``
        and each surfaced file is marked ``status="unknown"`` instead of
        the matrix-derived "ready to upload" label that an empty server
        list would otherwise produce — see ``_get_save_status_io``.
        """
        rom_id = int(rom_id)

        server_saves: list[dict] = []
        server_query_failed = False
        try:
            device_id = self._state_svc.get_server_device_id()
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id)),
            )
        except Exception as e:
            self._log_debug(f"Failed to fetch saves for rom {rom_id}: {e}")
            server_query_failed = True

        return await self._loop.run_in_executor(
            None,
            lambda: self._get_save_status_io(
                rom_id,
                server_saves,
                server_query_failed=server_query_failed,
            ),
        )

    async def check_save_status_background(self, rom_id: int) -> None:
        """Run full save status check in background and emit result to frontend."""
        try:
            result = await self.get_save_status(rom_id)
            await self._emit("save_status_updated", result)
        except Exception as e:
            self._log_debug(f"Background save status check failed for rom {rom_id}: {e}")

    def check_core_change(self, rom_id: int) -> dict:
        """Check if emulator core changed since last sync for a ROM."""
        if not self._state_svc.is_save_sync_enabled():
            return {"changed": False}

        rom_id_str = str(rom_id)
        save_entry = self._state_svc.state.saves.get(rom_id_str)
        if not save_entry:
            return {"changed": False}  # Never synced

        stored_core = save_entry.last_synced_core
        system = save_entry.system
        if not stored_core or not system:
            return {"changed": False}

        # Resolve ROM filename for per-game core detection
        rom_filename = None
        installed = self._state.get("installed_roms", {}).get(rom_id_str)
        if installed:
            file_path = installed.get("file_path", "")
            if file_path:
                rom_filename = os.path.basename(file_path)

        # Core labels come from ES-DE config which may differ from RetroArch's
        # corename (e.g. "Snes9x - Current" vs "Snes9x"). Aligning with RetroArch
        # core names is tracked in #208.
        try:
            active_core, active_label = self._get_active_core(system, rom_filename)
        except Exception:
            return {"changed": False}

        changed = detect_core_change(stored_core, active_core)

        if not changed:
            return {"changed": False}

        # Strip _libretro suffix for display (stored_core is guaranteed non-None here)
        old_label = stored_core.replace("_libretro", "")

        return {
            "changed": True,
            "old_core": stored_core,
            "new_core": active_core,
            "old_label": old_label,
            "new_label": active_label or (active_core.replace("_libretro", "") if active_core else None),
        }


def _outcome_server_sort_key(outcome: MatrixOutcome) -> float:
    """Sort key picking the newest server-side save across server-only outcomes."""
    candidates = outcome.server_candidates or []
    if not candidates:
        return 0.0
    newest = max(candidates, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)
    return parse_iso_to_epoch(newest.get("updated_at")) or 0.0


def _redact_server_fields(entry: dict) -> dict:
    """Return a copy of *entry* with status="unknown" and server fields nulled out.

    Used when the ``list_saves`` query failed: the matrix ran against an
    empty server list, so any "synced"/"upload"/"download"/"conflict" verdict
    and any server-side attribution (id, file_name, emulator, updated_at,
    size, device_syncs, is_current, uploaded_by_us) reflects "we have no
    server information", not the actual state of the server. The local-file
    fields (filename, local_path, local_hash, local_mtime, local_size,
    last_sync_at) come from local state and stay intact.
    """
    redacted = dict(entry)
    redacted["status"] = "unknown"
    redacted["server_save_id"] = None
    redacted["server_file_name"] = None
    redacted["server_emulator"] = None
    redacted["server_updated_at"] = None
    redacted["server_size"] = None
    redacted["device_syncs"] = []
    redacted["is_current"] = True
    redacted["uploaded_by_us"] = None
    return redacted
