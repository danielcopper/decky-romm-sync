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
class SaveFileStatus:
    """Status of a single save file for display."""

    filename: str
    status: str
    last_sync_at: str | None
    local_path: str | None = None
    local_hash: str | None = None
    local_mtime: str | None = None
    local_size: int | None = None
    server_save_id: int | None = None
    server_updated_at: str | None = None
    server_size: int | None = None


@dataclass(frozen=True)
class SaveSyncSettings:
    """User-facing save sync configuration."""

    save_sync_enabled: bool
    sync_before_launch: bool
    sync_after_exit: bool


@dataclass(frozen=True)
class SyncResult:
    """Result of a sync operation."""

    success: bool
    message: str
    synced: int = 0
    errors: tuple[str, ...] = ()
    conflicts: tuple[SaveConflict, ...] = ()
    offline: bool = False
