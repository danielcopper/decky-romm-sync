"""Library sync subsystem.

The package's public API is the :class:`LibraryService` façade — it
composes the library sync sub-services (:class:`LibraryFetcher`,
:class:`SyncOrchestrator`, :class:`SyncReporter`) over a shared
:class:`LibrarySyncStateBox` and exposes the callable surface consumed
by the Decky entrypoints (platform/collection metadata, sync preview/
apply, full sync, post-apply reporting, registry queries). RomM
communication goes through ``RommApiProtocol``; no ``import decky``.
"""

from services.library.service import LibraryService, LibraryServiceConfig

__all__ = ["LibraryService", "LibraryServiceConfig"]
