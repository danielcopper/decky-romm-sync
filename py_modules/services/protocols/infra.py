"""Cross-cutting infrastructure callable Protocols.

Narrow callable seams that don't belong to a specific I/O surface or
external system: frontend event emission, debug logging, generic
filesystem existence probes, and the small cross-service read/cleanup
hooks (LibraryService pending-sync map, download queue cleanup) that
would otherwise require service-to-service concrete imports.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol


class EventEmitter(Protocol):
    """Emit named events with a data payload to the frontend."""

    async def __call__(self, event: str, /, *args: object) -> None: ...


class ResolveUploadConflictFn(Protocol):
    """Decide the fallback when an upload POST is rejected by RomM's 409.

    Given the four hashes that describe an upload race — the current local
    content hash, the baseline recorded at the last sync, the server's live
    content hash, and the server hash stored at the last sync — returns
    ``"download"`` when the local is provably safe to discard (unchanged
    since baseline, or byte-identical to the server head) and ``"conflict"``
    otherwise. ``None`` and ``""`` both read as "unknown" and never yield
    ``"download"``.

    Backed at runtime by the compiled gavel core (``adapters.gavel_native``);
    the in-tree ``domain.sync_action.resolve_upload_conflict`` kernel shares
    the exact contract and stands in as a trivial fake in service tests.
    """

    def __call__(
        self,
        local_hash: str | None,
        last_sync_hash: str | None,
        server_content_hash: str | None,
        last_sync_server_hash: str | None,
    ) -> Literal["download", "conflict"]: ...


class DebugLogger(Protocol):
    """Log a debug/trace message string."""

    def __call__(self, msg: str) -> None: ...


class HostnameReader(Protocol):
    """Local device hostname source.

    Services consume this Protocol instead of ``socket.gethostname``
    directly so device registration stays free of raw syscalls and tests
    can pin the hostname without monkey-patching :mod:`socket`.
    """

    def get(self) -> str:
        """Return the local device hostname."""
        ...


class MachineIdReader(Protocol):
    """Stable machine-derived device identity source.

    Supplies the per-machine identifier device registration sends to RomM
    as the fingerprint ``hostname`` so the server dedupes this device
    across local-state wipes and reinstalls. Services consume this
    Protocol instead of reading the identity file directly so device
    registration stays free of raw I/O and tests can pin the value
    without touching the filesystem. ``None`` signals the identity is
    unreadable — callers degrade to no-fingerprint registration rather
    than substituting a colliding value.
    """

    def get(self) -> str | None:
        """Return the stable machine id, or ``None`` when unreadable."""
        ...


class PathExistsReader(Protocol):
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


class RendererRssFn(Protocol):
    """Current RSS of the Steam ``SharedJSContext`` renderer, in KB.

    The session-budget gate consults this to decide whether the next apply
    chunk would cross Steam's per-session heap budget. Returns ``None`` when the
    renderer's RSS cannot be read (no ``steamwebhelper`` process, unreadable
    ``/proc``) — the gate treats ``None`` as "measurement unavailable" and skips,
    so a broken reading never blocks a sync (fail-open).
    """

    def __call__(self) -> int | None: ...


class RendererGcFn(Protocol):
    """Force a garbage collection in the Steam renderer, returning success.

    Fired before an RSS reading so the measurement reflects settled heap rather
    than transient garbage (Steam's natural GC is measured-unreliable). Drives
    the CEF debugger over CDP. Returns ``False`` on any failure and never raises
    — a failed GC only means the subsequent reading is less precise, never that
    the sync should stop.
    """

    def __call__(self) -> bool: ...


class PendingSyncReader(Protocol):
    """Read seam for the LibraryService pending-sync map.

    SteamGridService consults the pending-sync map when resolving SGDB
    IDs for ROMs that are mid-sync (not yet in the registry). Exposing
    this as a Protocol avoids a service-to-service concrete import and
    keeps the typed seam narrow to "give me the current mapping".
    """

    def __call__(self) -> dict[int, dict[str, Any]]: ...


class DownloadQueueCleanup(Protocol):
    """Eviction seam for the in-memory ROM download queue.

    Consumed by ``RomRemovalService`` to remove queue entries when a ROM
    is deleted. Exposing this as a Protocol avoids a service-to-service
    concrete import and keeps the typed seam narrow to "evict one entry"
    and "clear all entries".
    """

    def evict(self, rom_id: int) -> None: ...

    def clear(self) -> None: ...
