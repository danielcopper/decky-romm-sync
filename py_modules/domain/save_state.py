"""Typed in-memory model for ``save_sync_state.json``.

Pure data shapes — no I/O, no logging, no service/adapter imports.
``StateService`` owns the aggregate at runtime; on-disk format stays
JSON via ``SaveSyncState.from_dict`` / ``SaveSyncState.to_dict``.
Legacy schema migrations apply inside ``from_dict``.

Aggregate root is :class:`SaveSyncState`. Per-ROM, per-file, playtime,
and settings shapes are reflected in dedicated dataclasses. Mutation
is supported (dataclasses are *not* frozen) so existing code paths
that update nested fields in place continue to work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Schema version. Bump only when ``from_dict`` migrations change shape.
_SCHEMA_VERSION = 1


@dataclass
class FileSyncState:
    """Per-file sync tracking under a ROM's ``saves`` entry.

    Tracks the last-observed local hash + sizes + timestamps so the
    newest-wins matrix can detect drift on the next sync. New entries
    use the defaults; persistence preserves whatever fields were set
    at the time of the last update.
    """

    tracked_save_id: int | None = None
    last_sync_hash: str | None = None
    last_sync_at: str = ""
    last_sync_server_updated_at: str = ""
    last_sync_server_save_id: int | None = None
    last_sync_server_size: int | None = None
    last_sync_local_mtime: float | None = None
    last_sync_local_size: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileSyncState:
        """Build from a raw dict, dropping legacy keys (``dismissed_newer_save_id``)."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            tracked_save_id=data.get("tracked_save_id"),
            last_sync_hash=data.get("last_sync_hash"),
            last_sync_at=data.get("last_sync_at", "") or "",
            last_sync_server_updated_at=data.get("last_sync_server_updated_at", "") or "",
            last_sync_server_save_id=data.get("last_sync_server_save_id"),
            last_sync_server_size=data.get("last_sync_server_size"),
            last_sync_local_mtime=data.get("last_sync_local_mtime"),
            last_sync_local_size=data.get("last_sync_local_size"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the on-disk JSON dict."""
        return {
            "tracked_save_id": self.tracked_save_id,
            "last_sync_hash": self.last_sync_hash,
            "last_sync_at": self.last_sync_at,
            "last_sync_server_updated_at": self.last_sync_server_updated_at,
            "last_sync_server_save_id": self.last_sync_server_save_id,
            "last_sync_server_size": self.last_sync_server_size,
            "last_sync_local_mtime": self.last_sync_local_mtime,
            "last_sync_local_size": self.last_sync_local_size,
        }


@dataclass
class RomSaveState:
    """Per-ROM save-sync state — slot config, attribution, per-file tracking.

    ``last_sync_check_at`` records when the sync engine last evaluated
    the matrix for this ROM regardless of whether any file transferred.
    ``slots`` keeps the merged server/local slot listing so the UI can
    survive a server outage. ``extra`` holds any unknown keys preserved
    from disk so the aggregate can round-trip forward-compatible state
    without losing fields the current code does not understand.
    """

    active_slot: str | None = None
    slot_confirmed: bool = False
    emulator: str = "retroarch"
    system: str = ""
    last_synced_core: str | None = None
    # ``None`` distinguishes "uploader attribution unknown" (legacy state
    # entry created before #186) from "we definitely uploaded nothing"
    # (empty list). Consumers downstream (status DTOs, version listings)
    # surface ``uploaded_by_us=None`` in the former case so the UI can hide
    # the attribution badge instead of asserting "not yours".
    own_upload_ids: list[int] | None = None
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, FileSyncState] = field(default_factory=dict)
    last_sync_check_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    _KNOWN_KEYS = frozenset(
        {
            "active_slot",
            "slot_confirmed",
            "emulator",
            "system",
            "last_synced_core",
            "own_upload_ids",
            "slots",
            "files",
            "last_sync_check_at",
        }
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RomSaveState:
        """Build from a raw dict.

        Applies legacy migrations:

        - ``active_core`` → ``last_synced_core`` (if both are present,
          ``last_synced_core`` wins).

        Unknown keys are kept in ``extra`` so persistence preserves them.
        """
        if not isinstance(data, dict):
            return cls()
        raw_files = data.get("files")
        files: dict[str, FileSyncState] = {}
        if isinstance(raw_files, dict):
            files = {fn: FileSyncState.from_dict(fs) for fn, fs in raw_files.items()}

        slots_raw = data.get("slots")
        slots: dict[str, dict[str, Any]] = slots_raw if isinstance(slots_raw, dict) else {}

        # Preserve the "missing key" vs "explicitly empty" distinction.
        own_upload_ids: list[int] | None
        if "own_upload_ids" not in data:
            own_upload_ids = None
        elif isinstance(data.get("own_upload_ids"), list):
            own_upload_ids = data["own_upload_ids"]
        else:
            own_upload_ids = None

        last_synced_core = data.get("last_synced_core")
        if last_synced_core is None and "active_core" in data:
            last_synced_core = data.get("active_core")

        extra = {k: v for k, v in data.items() if k not in cls._KNOWN_KEYS and k != "active_core"}

        return cls(
            active_slot=data.get("active_slot"),
            slot_confirmed=bool(data.get("slot_confirmed", False)),
            emulator=str(data.get("emulator", "retroarch")),
            system=str(data.get("system", "") or ""),
            last_synced_core=last_synced_core,
            own_upload_ids=own_upload_ids,
            slots=slots,
            files=files,
            last_sync_check_at=data.get("last_sync_check_at"),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the on-disk JSON dict.

        Preserves any unknown keys captured in ``extra`` and keeps a
        stable ordering compatible with the previous untyped layout.
        """
        out: dict[str, Any] = {
            "active_slot": self.active_slot,
            "slot_confirmed": self.slot_confirmed,
            "emulator": self.emulator,
            "system": self.system,
            "last_synced_core": self.last_synced_core,
            "slots": dict(self.slots),
            "files": {fn: fs.to_dict() for fn, fs in self.files.items()},
        }
        # Persist ``own_upload_ids`` only when known; "missing key" round-trips
        # so legacy entries don't gain a misleading empty list on first reload.
        if self.own_upload_ids is not None:
            out["own_upload_ids"] = list(self.own_upload_ids)
        if self.last_sync_check_at is not None:
            out["last_sync_check_at"] = self.last_sync_check_at
        for k, v in self.extra.items():
            out[k] = v
        return out


@dataclass
class PlaytimeEntry:
    """Per-ROM playtime tracking entry.

    Owned by :class:`PlaytimeService`. Field shape mirrors what the
    service writes today: a running ``total_seconds`` counter, the
    open ``last_session_start`` timestamp, a ``session_count`` for
    UI display, and the optional ``note_id`` returned by the RomM
    notes API. ``offline_deltas`` is kept for forward compatibility
    with the (currently unused) offline-buffer code path; ``extra``
    preserves any unknown keys round-tripped from disk.
    """

    total_seconds: int = 0
    session_count: int = 0
    last_session_start: str | None = None
    last_session_duration_sec: int | None = None
    offline_deltas: list[Any] = field(default_factory=list)
    note_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    _KNOWN_KEYS = frozenset(
        {
            "total_seconds",
            "session_count",
            "last_session_start",
            "last_session_duration_sec",
            "offline_deltas",
            "note_id",
        }
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaytimeEntry:
        """Build from a raw dict, capturing unknown keys in ``extra``."""
        if not isinstance(data, dict):
            return cls()
        offline_raw = data.get("offline_deltas")
        offline_deltas = offline_raw if isinstance(offline_raw, list) else []
        extra = {k: v for k, v in data.items() if k not in cls._KNOWN_KEYS}
        return cls(
            total_seconds=int(data.get("total_seconds", 0) or 0),
            session_count=int(data.get("session_count", 0) or 0),
            last_session_start=data.get("last_session_start"),
            last_session_duration_sec=data.get("last_session_duration_sec"),
            offline_deltas=offline_deltas,
            note_id=data.get("note_id"),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the on-disk JSON dict."""
        out: dict[str, Any] = {
            "total_seconds": self.total_seconds,
            "session_count": self.session_count,
            "last_session_start": self.last_session_start,
            "last_session_duration_sec": self.last_session_duration_sec,
            "offline_deltas": list(self.offline_deltas),
        }
        if self.note_id is not None:
            out["note_id"] = self.note_id
        for k, v in self.extra.items():
            out[k] = v
        return out


@dataclass
class SaveSyncSettings:
    """Save-sync feature settings (user-toggleable).

    Legacy keys (``conflict_mode``, ``clock_skew_tolerance_sec``)
    are dropped at :meth:`from_dict`. Any forward-compatible unknown
    keys are preserved in ``extra``.
    """

    save_sync_enabled: bool = False
    sync_before_launch: bool = True
    sync_after_exit: bool = True
    default_slot: str | None = "default"
    autocleanup_limit: int = 10
    extra: dict[str, Any] = field(default_factory=dict)

    _KNOWN_KEYS = frozenset(
        {
            "save_sync_enabled",
            "sync_before_launch",
            "sync_after_exit",
            "default_slot",
            "autocleanup_limit",
        }
    )
    _LEGACY_DROPPED_KEYS = frozenset({"conflict_mode", "clock_skew_tolerance_sec"})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SaveSyncSettings:
        """Build from a raw dict, dropping legacy keys."""
        if not isinstance(data, dict):
            return cls()
        extra = {k: v for k, v in data.items() if k not in cls._KNOWN_KEYS and k not in cls._LEGACY_DROPPED_KEYS}
        # ``default_slot=None`` is the legacy ("no slots") mode; preserve it
        # rather than collapsing it to the "default" string default.
        raw_slot = data.get("default_slot", "default") if "default_slot" in data else "default"
        if raw_slot is None:
            default_slot: str | None = None
        else:
            slot_str = str(raw_slot)
            default_slot = slot_str if slot_str else None
        return cls(
            save_sync_enabled=bool(data.get("save_sync_enabled", False)),
            sync_before_launch=bool(data.get("sync_before_launch", True)),
            sync_after_exit=bool(data.get("sync_after_exit", True)),
            default_slot=default_slot,
            autocleanup_limit=int(data.get("autocleanup_limit", 10) or 10),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the on-disk JSON dict."""
        out: dict[str, Any] = {
            "save_sync_enabled": self.save_sync_enabled,
            "sync_before_launch": self.sync_before_launch,
            "sync_after_exit": self.sync_after_exit,
            "default_slot": self.default_slot,
            "autocleanup_limit": self.autocleanup_limit,
        }
        for k, v in self.extra.items():
            out[k] = v
        return out


@dataclass
class SaveSyncState:
    """Aggregate root for the on-disk ``save_sync_state.json``.

    Carries device identity, per-ROM save state, playtime tracking,
    and feature settings. Persistence flows through :meth:`to_dict` /
    :meth:`from_dict`; schema migrations apply on load. Mutation is
    in-place — held by reference across services.
    """

    version: int = _SCHEMA_VERSION
    device_id: str | None = None
    device_name: str | None = None
    server_device_id: int | str | None = None
    saves: dict[str, RomSaveState] = field(default_factory=dict)
    playtime: dict[str, PlaytimeEntry] = field(default_factory=dict)
    settings: SaveSyncSettings = field(default_factory=SaveSyncSettings)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SaveSyncState:
        """Build from a freshly-loaded JSON dict, applying legacy migrations."""
        if not isinstance(data, dict):
            return cls()
        raw_saves = data.get("saves")
        saves: dict[str, RomSaveState] = {}
        if isinstance(raw_saves, dict):
            saves = {rid: RomSaveState.from_dict(entry) for rid, entry in raw_saves.items()}

        raw_playtime = data.get("playtime")
        playtime: dict[str, PlaytimeEntry] = {}
        if isinstance(raw_playtime, dict):
            playtime = {rid: PlaytimeEntry.from_dict(entry) for rid, entry in raw_playtime.items()}

        return cls(
            version=int(data.get("version", _SCHEMA_VERSION) or _SCHEMA_VERSION),
            device_id=data.get("device_id"),
            device_name=data.get("device_name"),
            server_device_id=data.get("server_device_id"),
            saves=saves,
            playtime=playtime,
            settings=SaveSyncSettings.from_dict(data.get("settings", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the on-disk JSON dict.

        Output key order matches the historical untyped layout so the
        on-disk file diff is empty when nothing has changed semantically.
        """
        return {
            "version": self.version,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "server_device_id": self.server_device_id,
            "saves": {rid: rs.to_dict() for rid, rs in self.saves.items()},
            "playtime": {rid: pe.to_dict() for rid, pe in self.playtime.items()},
            "settings": self.settings.to_dict(),
        }

    def replace_with(self, other: SaveSyncState) -> None:
        """Mutate this aggregate in-place to mirror *other*.

        Used by :class:`StateService` so callers that hold a long-lived
        reference (other services, sub-services, ``main.py``) keep
        seeing the latest loaded state without rewiring.
        """
        self.version = other.version
        self.device_id = other.device_id
        self.device_name = other.device_name
        self.server_device_id = other.server_device_id
        self.saves = other.saves
        self.playtime = other.playtime
        self.settings = other.settings


__all__ = [
    "FileSyncState",
    "PlaytimeEntry",
    "RomSaveState",
    "SaveSyncSettings",
    "SaveSyncState",
]
