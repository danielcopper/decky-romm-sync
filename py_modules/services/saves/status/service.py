from __future__ import annotations

from typing import TYPE_CHECKING

from models.saves import SaveConflict

from domain.sync_action import Conflict, Skip
from lib.iso_time import parse_iso_to_epoch
from services.saves._helpers import _compute_uploaded_by_us
from services.saves.status.builders import (
    _build_file_status,
    _resolve_chosen_server,
    _status_from_action,
)

if TYPE_CHECKING:
    import logging

    from services.protocols import RommApiProtocol
    from services.saves import SaveService
    from services.saves.state import StateService
    from services.saves.sync_engine import MatrixOutcome, SyncEngine


class StatusService:
    """Read-only matrix-driven status reporting for the SAVES tab."""

    def __init__(
        self,
        *,
        save_service: SaveService,
        state_svc: StateService,
        sync_engine: SyncEngine,
        romm_api: RommApiProtocol,
        logger: logging.Logger,
    ) -> None:
        self._save_service = save_service
        self._state_svc = state_svc
        self._sync_engine = sync_engine
        self._romm_api = romm_api
        self._logger = logger

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
        chosen_server = _resolve_chosen_server(action, outcome.server_candidates)
        status_entry = _build_file_status(
            outcome.filename,
            local_path=outcome.local_path,
            local_hash=outcome.local_hash,
            local_mtime=outcome.local_mtime_iso,
            local_size=outcome.local_size,
            server=chosen_server,
            last_sync_at=outcome.file_state.get("last_sync_at"),
            status=_status_from_action(action),
            server_device_id=server_device_id,
            uploaded_by_us=_compute_uploaded_by_us(chosen_server, own_upload_ids),
        )
        conflict_entry: dict | None = None
        if isinstance(action, Conflict):
            self._save_service._log_debug(
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

    def _get_save_status_io(self, rom_id: int, server_saves: list[dict]) -> dict:
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
        """
        rom_id_str = str(rom_id)
        info = self._save_service._get_rom_save_info(rom_id)
        server_device_id = self._save_service._get_server_device_id()

        save_state = self._state_svc.data["saves"].get(rom_id_str, {})
        active_slot = save_state.get("active_slot")
        server_in_slot = self._sync_engine._filter_server_saves_to_slot(server_saves, active_slot)

        # own_upload_ids: None means missing key (legacy entry — unknown attribution).
        raw_own_ids = save_state.get("own_upload_ids")
        own_upload_ids: list[int] | None = raw_own_ids if isinstance(raw_own_ids, list) else None

        file_statuses: list[dict] = []
        conflicts: list[SaveConflict | dict] = []

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
                file_statuses.append(status_entry)
                if conflict_entry is not None:
                    conflicts.append(conflict_entry)

        playtime = self._state_svc.data.get("playtime", {}).get(rom_id_str, {})
        save_entry = self._state_svc.data.get("saves", {}).get(rom_id_str, {})

        return {
            "rom_id": rom_id,
            "files": file_statuses,
            "playtime": playtime,
            "device_id": self._state_svc.data.get("device_id", ""),
            "last_sync_check_at": save_entry.get("last_sync_check_at"),
            "conflicts": conflicts,
            "save_sort_changed": self._save_service._is_save_sort_changed(),
        }


def _outcome_server_sort_key(outcome: MatrixOutcome) -> float:
    """Sort key picking the newest server-side save across server-only outcomes."""
    candidates = outcome.server_candidates or []
    if not candidates:
        return 0.0
    newest = max(candidates, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)
    return parse_iso_to_epoch(newest.get("updated_at")) or 0.0
