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
    MigrationPendingFn,
    RetryStrategy,
    SaveSortChangeFn,
    SessionAchievementSync,
    SessionMigrationReader,
    SessionPlaytimeRecorder,
    SessionPostExitSync,
)
from services.protocols.determinism import Clock, Sleeper, UuidGen
from services.protocols.files import (
    CoverArtFileStore,
    DownloadFileStore,
    DownloadQueueStore,
    FirmwareFileStore,
    MigrationFileStore,
    RomFileStore,
    SaveFileStore,
    SgdbArtworkCache,
)
from services.protocols.infra import (
    DebugLogger,
    DownloadQueueCleanup,
    EventEmitter,
    HostnameReader,
    PathExistsReader,
    PendingSyncReader,
)
from services.protocols.paths import (
    CoreInfoProvider,
    CoreNameProviderFn,
    CoreResolverFn,
    GamelistXmlEditor,
    RetroArchConfigReader,
    RetroArchCoreInfoReader,
    RetroArchSaveSortingProvider,
    RetroDeckPaths,
    SystemResolver,
)
from services.protocols.persistence import (
    FirmwareCachePersister,
    MetadataCachePersister,
    MetadataCacheStore,
    PluginMetadataReader,
    SaveSyncStatePersister,
    SettingsPersister,
    ShortcutRegistryStore,
    StatePersister,
)
from services.protocols.transport import (
    RommAchievementsApi,
    RommApi,
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
    SteamConfigStore,
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
    "DownloadFileStore",
    "DownloadQueueCleanup",
    "DownloadQueueStore",
    "EventEmitter",
    "FirmwareCachePersister",
    "FirmwareFileStore",
    "GamelistXmlEditor",
    "HostnameReader",
    "LaunchGateInstalledChecker",
    "LaunchGateRomLookup",
    "LaunchGateSaveStatusReader",
    "MetadataCachePersister",
    "MetadataCacheStore",
    "MetadataExtractor",
    "MigrationFileStore",
    "MigrationPendingFn",
    "PathExistsReader",
    "PendingSyncReader",
    "PluginMetadataReader",
    "RetroArchConfigReader",
    "RetroArchCoreInfoReader",
    "RetroArchSaveSortingProvider",
    "RetroDeckPaths",
    "RetryStrategy",
    "RomFileStore",
    "RommAchievementsApi",
    "RommApi",
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
    "SaveFileStore",
    "SaveSortChangeFn",
    "SaveSyncStatePersister",
    "SessionAchievementSync",
    "SessionMigrationReader",
    "SessionPlaytimeRecorder",
    "SessionPostExitSync",
    "SettingsPersister",
    "SgdbArtworkCache",
    "ShortcutRegistryStore",
    "Sleeper",
    "StatePersister",
    "SteamConfigStore",
    "SteamGridDbApi",
    "SystemResolver",
    "UuidGen",
]
