"""Protocol interfaces for service dependencies.

Services depend on these Protocols, not concrete adapter implementations.
This keeps the dependency direction clean: adapters implement Protocols,
services consume them.

The package is organised topically — consumers always deep-import via
``from services.protocols import X`` regardless of the host module:

- ``transport``: external system clients (RomM REST, SGDB REST, Steam IPC).
- ``determinism``: ``Clock`` / ``UuidGen`` / ``Sleeper`` test seams.
- ``persistence``: on-disk plugin state, settings, save-sync state,
  firmware cache.
- ``paths``: RetroDECK path getters, system/core resolution, ES-DE
  read/write seams.
- ``infra``: cross-cutting callable seams (event emit, debug log,
  exists probe, cross-service callable bundles).
- ``files``: filesystem seams owning raw POSIX-style file I/O per
  service subtree.
- ``cross_service``: narrowly-typed multi-method seams one service
  exposes to another so services remain independent.
"""

from __future__ import annotations

from services.protocols.cross_service import (
    AchievementsReader,
    ArtworkManager,
    ArtworkRemover,
    BiosChecker,
    LaunchGateInstalledChecker,
    LaunchGateRomLookup,
    LaunchGateSaveStatusReader,
    MetadataExtractor,
    RetryStrategy,
    SessionAchievementSync,
    SessionMigrationReader,
    SessionPlaytimeRecorder,
    SessionPostExitSync,
)
from services.protocols.determinism import Clock, Sleeper, UuidGen
from services.protocols.files import (
    CoverArtFileStore,
    DownloadFileAdapter,
    DownloadQueueAdapter,
    FirmwareFileAdapter,
    MigrationFileAdapter,
    RomFileAdapter,
    SaveFileAdapter,
    SgdbArtworkCache,
)
from services.protocols.infra import (
    DebugLogger,
    DownloadQueueCleanup,
    EventEmitter,
    PathExistsProbe,
    PendingSyncReader,
)
from services.protocols.paths import (
    CoreInfoProvider,
    CoreNameProviderFn,
    CoreResolverFn,
    GamelistXmlEditorProtocol,
    RetroArchSaveSortingProvider,
    RetroDeckPaths,
    SystemResolver,
)
from services.protocols.persistence import (
    FirmwareCachePersister,
    MetadataCachePersister,
    SaveSyncStatePersister,
    SettingsPersister,
    StatePersister,
)
from services.protocols.transport import (
    RommAchievementsApi,
    RommConnectionApi,
    RommDeviceApi,
    RommFirmwareApi,
    RommLibraryApi,
    RommPlatformReader,
    RommPlaytimeApi,
    RommRomReader,
    RommSaveApi,
    RommSyncApi,
    RommVersion,
    SteamConfigAdapter,
    SteamGridDbApi,
)

__all__ = [
    "AchievementsReader",
    "ArtworkManager",
    "ArtworkRemover",
    "BiosChecker",
    "Clock",
    "CoreInfoProvider",
    "CoreNameProviderFn",
    "CoreResolverFn",
    "CoverArtFileStore",
    "DebugLogger",
    "DownloadFileAdapter",
    "DownloadQueueAdapter",
    "DownloadQueueCleanup",
    "EventEmitter",
    "FirmwareCachePersister",
    "FirmwareFileAdapter",
    "GamelistXmlEditorProtocol",
    "LaunchGateInstalledChecker",
    "LaunchGateRomLookup",
    "LaunchGateSaveStatusReader",
    "MetadataCachePersister",
    "MetadataExtractor",
    "MigrationFileAdapter",
    "PathExistsProbe",
    "PendingSyncReader",
    "RetroArchSaveSortingProvider",
    "RetroDeckPaths",
    "RetryStrategy",
    "RomFileAdapter",
    "RommAchievementsApi",
    "RommConnectionApi",
    "RommDeviceApi",
    "RommFirmwareApi",
    "RommLibraryApi",
    "RommPlatformReader",
    "RommPlaytimeApi",
    "RommRomReader",
    "RommSaveApi",
    "RommSyncApi",
    "RommVersion",
    "SaveFileAdapter",
    "SaveSyncStatePersister",
    "SessionAchievementSync",
    "SessionMigrationReader",
    "SessionPlaytimeRecorder",
    "SessionPostExitSync",
    "SettingsPersister",
    "SgdbArtworkCache",
    "Sleeper",
    "StatePersister",
    "SteamConfigAdapter",
    "SteamGridDbApi",
    "SystemResolver",
    "UuidGen",
]
