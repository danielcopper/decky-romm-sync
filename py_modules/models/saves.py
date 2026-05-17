"""Save sync dataclasses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SaveConflict:
    """A detected save file conflict between local and server."""

    rom_id: int
    filename: str
    local_path: str | None
    local_hash: str | None
    local_mtime: str | None
    local_size: int | None
    server_save_id: int | None
    server_updated_at: str
    server_size: int | None
    created_at: str


@dataclass(frozen=True)
class SaveSyncSettings:
    """User-facing save sync configuration."""

    save_sync_enabled: bool
    sync_before_launch: bool
    sync_after_exit: bool
