"""Wire-neutral data shapes shared by vanished-ROM cleanup services and adapters."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RecoveryArtifact(TypedDict):
    """One source copied into a recovery bundle under a generated destination."""

    source_path: str
    safe_root: str
    kind: str
    rom_id: NotRequired[int]


class SourceIdentity(TypedDict):
    """Exact no-follow source identity sealed before destructive mutation."""

    exists: bool
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


class SteamRecoverySnapshot(TypedDict):
    """Backend-owned Steam Input state and files for one shortcut."""

    user_id: str
    user_dir: str
    steam_root: str
    controller_setting: str | None
    artifacts: list[RecoveryArtifact]
