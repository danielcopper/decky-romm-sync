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


class SourceEntry(TypedDict):
    """One descendant held by a no-follow source claim."""

    identity: SourceIdentity
    sha256: NotRequired[str]


class SourceClaim(TypedDict):
    """A source root and its complete descriptor-inventoried subtree."""

    source_path: str
    safe_root: str
    source_identity: SourceIdentity
    entries: dict[str, SourceEntry]


class MutationOutcome(TypedDict):
    """Truthful result of a durable filesystem mutation attempt."""

    success: bool
    changed: bool
    ambiguous: bool
    message: str


class SteamRecoverySnapshot(TypedDict):
    """Backend-owned Steam Input state and files for one shortcut."""

    user_id: str
    user_dir: str
    steam_root: str
    controller_setting: str | None
    artifacts: list[RecoveryArtifact]
