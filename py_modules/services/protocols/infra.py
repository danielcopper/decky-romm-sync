"""Cross-cutting infrastructure callable Protocols.

Narrow callable seams that don't belong to a specific I/O surface or
external system: frontend event emission, debug logging, generic
filesystem existence probes, and the small cross-service read/cleanup
hooks (LibraryService pending-sync map, download queue cleanup) that
would otherwise require service-to-service concrete imports.
"""

from __future__ import annotations

from typing import Protocol


class EventEmitter(Protocol):
    """Emit named events with a data payload to the frontend."""

    async def __call__(self, event: str, /, *args: object) -> None: ...


class DebugLogger(Protocol):
    """Log a debug/trace message string."""

    def __call__(self, msg: str) -> None: ...


class HostnameProvider(Protocol):
    """Local device hostname source.

    Services consume this Protocol instead of ``socket.gethostname``
    directly so device registration stays free of raw syscalls and tests
    can pin the hostname without monkey-patching :mod:`socket`.
    """

    def get(self) -> str:
        """Return the local device hostname."""
        ...


class PathExistsProbe(Protocol):
    """Generic filesystem existence probe.

    Used by services that need to check whether a path is currently
    present on disk without touching ``os.path`` directly. Distinct from
    the domain-shaped ``CoverArtFileStore`` / ``DownloadFileStore`` /
    ``MigrationFileStore`` Protocols: this one exposes only the
    semantic question "does this path exist?" and carries no implication
    about which subtree of the filesystem the caller is reasoning about.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...


class PendingSyncReader(Protocol):
    """Read seam for the LibraryService pending-sync map.

    SteamGridService consults the pending-sync map when resolving SGDB
    IDs for ROMs that are mid-sync (not yet in the registry). Exposing
    this as a Protocol avoids a service-to-service concrete import and
    keeps the typed seam narrow to "give me the current mapping".
    """

    def __call__(self) -> dict: ...


class DownloadQueueCleanup(Protocol):
    """Eviction seam for the in-memory ROM download queue.

    Consumed by ``RomRemovalService`` to remove queue entries when a ROM
    is deleted. Exposing this as a Protocol avoids a service-to-service
    concrete import and keeps the typed seam narrow to "evict one entry"
    and "clear all entries".
    """

    def evict(self, rom_id: int) -> None: ...

    def clear(self) -> None: ...
