"""Unit-of-Work Protocols — the transactional seam over the eleven repositories.

A Unit of Work is the atomic boundary services work inside: open it, touch any
of the eleven repositories, and on a clean exit every change commits as one
transaction; on an exception everything rolls back. Services depend on these
Protocols, never on the concrete ``SqliteUnitOfWork`` — the composition root
wires the factory.

``UnitOfWork`` exposes the eleven repositories as typed properties. ``UnitOfWorkFactory``
is the call-shaped seam services hold to open a fresh unit per operation; the
concrete factory (``functools.partial(SqliteUnitOfWork, db_path)``) structurally
satisfies it. The concrete ``SqliteUnitOfWork`` returns concrete
``SqliteXxxRepository`` instances that structurally satisfy the repository
Protocols below, so the adapter package never imports this module — keeping the
``adapters ↛ services`` direction clean.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from types import TracebackType

    from services.protocols.repositories import (
        BiosFileRepository,
        CollectionSyncStateRepository,
        FirmwareCacheRepository,
        KvConfigRepository,
        PlatformSyncStateRepository,
        PlaytimeRepository,
        RomInstallRepository,
        RomMetadataRepository,
        RomRepository,
        RomSaveSyncStateRepository,
        SyncRunRepository,
    )


class UnitOfWork(Protocol):
    """Atomic transaction boundary exposing the eleven repositories.

    Used as a synchronous context manager: a clean ``__exit__`` commits, an
    exceptional one rolls back. The repositories share the unit's open
    connection, so writes across several of them are one transaction.

    The eleven repositories are read-only properties (not mutable attributes) so
    they are covariant: a concrete unit may expose a concrete ``SqliteXxxRepository``
    that satisfies the repository Protocol without being exactly it. A mutable
    attribute would be invariant and reject the concrete adapter types.
    """

    @property
    def roms(self) -> RomRepository: ...
    @property
    def rom_installs(self) -> RomInstallRepository: ...
    @property
    def rom_metadata(self) -> RomMetadataRepository: ...
    @property
    def playtime(self) -> PlaytimeRepository: ...
    @property
    def rom_save_sync_states(self) -> RomSaveSyncStateRepository: ...
    @property
    def bios_files(self) -> BiosFileRepository: ...
    @property
    def firmware_cache(self) -> FirmwareCacheRepository: ...
    @property
    def sync_runs(self) -> SyncRunRepository: ...
    @property
    def platform_sync_state(self) -> PlatformSyncStateRepository: ...
    @property
    def collection_sync_state(self) -> CollectionSyncStateRepository: ...
    @property
    def kv_config(self) -> KvConfigRepository: ...

    def __enter__(self) -> UnitOfWork:
        """Open the connection, begin the transaction, and return the unit."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Commit on a clean exit, roll back on an exception, then close."""
        ...


class UnitOfWorkFactory(Protocol):
    """Call-shaped seam that opens a fresh :class:`UnitOfWork` per operation."""

    def __call__(self) -> UnitOfWork:
        """Return a new, not-yet-entered unit of work."""
        ...
