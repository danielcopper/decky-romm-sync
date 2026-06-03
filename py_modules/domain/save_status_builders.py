"""Pure builders for the file-status DTO shape consumed by the saves UI.

Anything that derives status metadata from a sync action, the local
file state, and the server-side save records — without touching disk,
state, or network — belongs here. Code that needs the live
:class:`SaveSyncState` aggregate, network calls, or file I/O belongs in
``services/saves/status/``.
"""

from __future__ import annotations

from typing import Any

from domain.iso_time import parse_iso_to_epoch
from domain.sync_action import Conflict, Download, Skip, Upload


def build_file_status(
    filename: str,
    *,
    local_path: str | None,
    local_hash: str | None,
    local_mtime: str | None,
    local_size: int | None,
    server: dict[str, Any] | None,
    last_sync_at: str | None,
    status: str,
    server_device_id: str | None = None,
    uploaded_by_us: bool | None = None,
) -> dict[str, Any]:
    """Build a file status dict for the frontend."""
    server_device_syncs = server.get("device_syncs", []) if server else []
    device_syncs = [
        {
            "device_id": ds.get("device_id", ""),
            "device_name": ds.get("device_name", ""),
            "is_current": ds.get("is_current", False),
            "last_synced_at": ds.get("last_synced_at"),
        }
        for ds in server_device_syncs
    ]
    own_sync = (
        next(
            (ds for ds in server_device_syncs if ds.get("device_id") == server_device_id),
            None,
        )
        if server_device_id
        else None
    )
    is_current = own_sync.get("is_current", True) if own_sync else True

    return {
        "filename": filename,
        "local_path": local_path,
        "local_hash": local_hash,
        "local_mtime": local_mtime,
        "local_size": local_size,
        "server_save_id": server.get("id") if server else None,
        "server_file_name": server.get("file_name") if server else None,
        "server_emulator": server.get("emulator") if server else None,
        "server_updated_at": server.get("updated_at", "") if server else None,
        "server_size": server.get("file_size_bytes") if server else None,
        "last_sync_at": last_sync_at,
        "status": status,
        "device_syncs": device_syncs,
        "is_current": is_current,
        "uploaded_by_us": uploaded_by_us,
    }


def status_from_action(action: object) -> str:
    """Map a ``SyncAction`` outcome to the legacy file-status string."""
    if isinstance(action, Skip):
        return "synced"
    if isinstance(action, Upload):
        return "upload"
    if isinstance(action, Download):
        return "download"
    if isinstance(action, Conflict):
        return "conflict"
    return "synced"


def resolve_chosen_server(action: object, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the server-save dict to display alongside the file-status row.

    - ``Download`` and ``Conflict`` carry the chosen save explicitly on the
      action — use that.
    - ``Skip`` falls back to the newest in *candidates* so the status panel
      still shows a server reference where one exists (e.g. synced rows
      continue to display the server save's metadata).
    - ``Upload(target_save_id=None)`` (POST-as-new) has no server reference
      yet → ``None``.
    - ``Upload(target_save_id=int)`` (PUT) targets an existing save in
      *candidates* — fall back to the newest so the status panel still
      shows the server-side metadata while the upload is pending.
    """
    if isinstance(action, Download | Conflict):
        return action.server_save
    if isinstance(action, Upload) and action.target_save_id is None:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)
