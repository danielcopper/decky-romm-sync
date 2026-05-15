"""On-disk persistence Protocols for plugin state files.

Services delegate disk round-trips for plugin state, settings, save-sync
state, and the firmware listing cache to these Protocols so atomic
writes, locking, and corrupt-file recovery stay in adapters. Services
see only the dict payload (or zero-arg "flush" callable) they care
about; schema migrations on loaded data are a service concern.
"""

from __future__ import annotations

from typing import Protocol


class StatePersister(Protocol):
    """Persist plugin state to disk (zero-argument callable)."""

    def __call__(self) -> None: ...


class SettingsPersister(Protocol):
    """Persist settings to disk (zero-argument callable)."""

    def __call__(self) -> None: ...


class SaveSyncStatePersister(Protocol):
    """Read/write the on-disk save-sync state file.

    Implementations are responsible for atomic writes, locking, and
    handling missing/corrupt files. They perform dumb I/O only —
    schema migrations on loaded data live in ``StateService``, so
    ``load`` returns the raw dict (or ``None`` when the file does not
    yet exist) without versioning the payload.
    """

    def save(self, data: dict) -> None: ...

    def load(self) -> dict | None: ...


class FirmwareCachePersister(Protocol):
    """Read/write the on-disk firmware list cache.

    Owns the round-trip for the cached firmware listing consumed by
    ``FirmwareService``. Path, file format, and version handling are
    adapter concerns — services see only the dict payload they
    previously wrote. ``load`` returns an empty dict (not ``None``)
    when no cached payload is available so callers can probe with
    ``"items" in data`` without a None-check.
    """

    def save(self, data: dict) -> None: ...

    def load(self) -> dict: ...
