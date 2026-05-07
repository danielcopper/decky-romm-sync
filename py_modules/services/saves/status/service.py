from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from models.saves import SaveConflict

from domain.sync_action import Conflict, Skip, compute_sync_action
from lib.iso_time import parse_iso_to_epoch
from services.saves._helpers import _compute_uploaded_by_us, _local_save_target
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


class StatusService:
    """Read-only matrix-driven status reporting for the SAVES tab."""

    def __init__(
        self,
        *,
        save_service: SaveService,
        state_svc: StateService,
        romm_api: RommApiProtocol,
        logger: logging.Logger,
    ) -> None:
        self._save_service = save_service
        self._state_svc = state_svc
        self._romm_api = romm_api
        self._logger = logger

    def _status_entry_for_local_file(
        self,
        local_file: dict,
        *,
        rom_id: int,
        rom_id_str: str,
        server_in_slot: list[dict],
        files_state: dict,
        server_device_id: str | None,
        own_upload_ids: list[int] | None,
    ) -> tuple[dict, dict | None]:
        """Build the file-status entry for an existing local file.

        Returns ``(status_entry, conflict_entry_or_None)``. The conflict
        entry is the ``sync_conflict`` descriptor when ``compute_sync_action``
        returns ``Conflict``; otherwise None.
        """
        filename = local_file["filename"]
        local_path = local_file["path"]
        local_hash = self._save_service._file_md5(local_path) if os.path.isfile(local_path) else None
        file_state = files_state.get(filename, {})
        action = compute_sync_action(
            local_file=self._save_service._build_local_input(local_path, filename),
            server_saves_in_slot=server_in_slot,
            files_state=file_state,
            device_id=server_device_id or "",
            local_hash=local_hash,
        )
        if isinstance(action, Skip) and action.adopt_baseline and local_hash is not None:
            self._save_service._adopt_baseline_hash(rom_id_str, filename, local_hash)
        chosen_server = _resolve_chosen_server(action, server_in_slot)
        local_mtime = (
            datetime.fromtimestamp(os.path.getmtime(local_path), tz=UTC).isoformat()
            if os.path.isfile(local_path)
            else None
        )
        local_size = os.path.getsize(local_path) if os.path.isfile(local_path) else None
        status_entry = _build_file_status(
            filename,
            local_path=local_path,
            local_hash=local_hash,
            local_mtime=local_mtime,
            local_size=local_size,
            server=chosen_server,
            last_sync_at=file_state.get("last_sync_at"),
            status=_status_from_action(action),
            server_device_id=server_device_id,
            uploaded_by_us=_compute_uploaded_by_us(chosen_server, own_upload_ids),
        )
        conflict_entry: dict | None = None
        if isinstance(action, Conflict):
            self._save_service._log_debug(
                f"_get_save_status_io({rom_id}): conflict {filename} server_save_id={action.server_save.get('id')}"
            )
            conflict_entry = self._save_service._build_sync_conflict_entry(
                rom_id, filename, action.server_save, local_path, local_hash
            )
        return status_entry, conflict_entry

    def _status_entry_for_server_only(
        self,
        server_in_slot: list[dict],
        *,
        rom_name: str,
        server_device_id: str | None,
        own_upload_ids: list[int] | None,
    ) -> dict:
        """Build the ready-to-download status entry when no local file exists
        but the slot has server saves. Picks newest by ``updated_at``."""
        newest = max(server_in_slot, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)
        return _build_file_status(
            _local_save_target(newest, rom_name),
            local_path=None,
            local_hash=None,
            local_mtime=None,
            local_size=None,
            server=newest,
            last_sync_at=None,
            status="download",
            server_device_id=server_device_id,
            uploaded_by_us=_compute_uploaded_by_us(newest, own_upload_ids),
        )

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
        files_state = save_state.get("files", {})
        active_slot = save_state.get("active_slot")
        server_in_slot = self._save_service._filter_server_saves_to_slot(server_saves, active_slot)

        # own_upload_ids: None means missing key (legacy entry — unknown attribution).
        raw_own_ids = save_state.get("own_upload_ids")
        own_upload_ids: list[int] | None = raw_own_ids if isinstance(raw_own_ids, list) else None

        file_statuses: list[dict] = []
        conflicts: list[SaveConflict | dict] = []

        if info is not None:
            rom_name = info["rom_name"]
            local_files = self._save_service._find_save_files(rom_id)
            local_file = local_files[0] if local_files else None

            if local_file is not None:
                status_entry, conflict_entry = self._status_entry_for_local_file(
                    local_file,
                    rom_id=rom_id,
                    rom_id_str=rom_id_str,
                    server_in_slot=server_in_slot,
                    files_state=files_state,
                    server_device_id=server_device_id,
                    own_upload_ids=own_upload_ids,
                )
                file_statuses.append(status_entry)
                if conflict_entry is not None:
                    conflicts.append(conflict_entry)
            elif server_in_slot:
                file_statuses.append(
                    self._status_entry_for_server_only(
                        server_in_slot,
                        rom_name=rom_name,
                        server_device_id=server_device_id,
                        own_upload_ids=own_upload_ids,
                    )
                )

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
