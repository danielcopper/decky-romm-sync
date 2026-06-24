"""Protocol interfaces for service dependencies.

Services depend on these Protocols, not concrete adapter implementations.
This keeps the dependency direction clean: adapters implement Protocols,
services consume them.

The package is organised topically — consumers always deep-import via
``from services.protocols import X`` regardless of the host module:

- ``transport``: external system clients (RomM REST, SGDB REST, Steam IPC).
- ``determinism``: ``Clock`` / ``UuidGen`` / ``Sleeper`` test seams.
- ``persistence``: on-disk plugin settings and plugin metadata.
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
    ActiveCoreReader,
    ArtworkManager,
    ArtworkRemover,
    BiosChecker,
    DiscResolver,
    LaunchGateDriftReader,
    LaunchGateInstalledChecker,
    LaunchGateRomLookup,
    LaunchGateSaveStatusReader,
    MigrationPendingFn,
    RelaunchOptionsReader,
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
    DirectoryFileListerFn,
    DownloadFileStore,
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
    MachineIdReader,
    PathExistsReader,
    PendingSyncReader,
)
from services.protocols.paths import (
    CoreInfoProvider,
    CoreNameProviderFn,
    CoreResolverFn,
    PlatformCoreReader,
    RetroArchConfigReader,
    RetroArchCoreInfoReader,
    RetroArchSaveLayoutProvider,
    RetroDeckPaths,
    SystemM3uSupportFn,
    SystemResolver,
    SystemSupportedExtensionsFn,
)
from services.protocols.persistence import (
    PluginMetadataReader,
    SettingsPersister,
)
from services.protocols.repositories import (
    BiosFileRepository,
    FirmwareCacheRepository,
    KvConfigRepository,
    PlaytimeRepository,
    RomInstallRepository,
    RomMetadataRepository,
    RomRepository,
    RomSaveStateRepository,
    SyncRunRepository,
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
    RommTokenApi,
    RommVersion,
    SteamConfigStore,
    SteamGridDbApi,
)
from services.protocols.uow import UnitOfWork, UnitOfWorkFactory

__all__ = [
    "AchievementsReader",
    "ActiveCoreReader",
    "ArtworkManager",
    "ArtworkRemover",
    "BiosChecker",
    "BiosFileRepository",
    "Clock",
    "CoreInfoProvider",
    "CoreNameProviderFn",
    "CoreResolverFn",
    "CoverArtFileStore",
    "DebugLogger",
    "DirectoryFileListerFn",
    "DiscResolver",
    "DownloadFileStore",
    "DownloadQueueCleanup",
    "EventEmitter",
    "FirmwareCacheRepository",
    "FirmwareFileStore",
    "HostnameReader",
    "KvConfigRepository",
    "LaunchGateDriftReader",
    "LaunchGateInstalledChecker",
    "LaunchGateRomLookup",
    "LaunchGateSaveStatusReader",
    "MachineIdReader",
    "MigrationFileStore",
    "MigrationPendingFn",
    "PathExistsReader",
    "PendingSyncReader",
    "PlatformCoreReader",
    "PlaytimeRepository",
    "PluginMetadataReader",
    "RelaunchOptionsReader",
    "RetroArchConfigReader",
    "RetroArchCoreInfoReader",
    "RetroArchSaveLayoutProvider",
    "RetroDeckPaths",
    "RetryStrategy",
    "RomFileStore",
    "RomInstallRepository",
    "RomMetadataRepository",
    "RomRepository",
    "RomSaveStateRepository",
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
    "RommTokenApi",
    "RommVersion",
    "SaveFileStore",
    "SaveSortChangeFn",
    "SessionAchievementSync",
    "SessionMigrationReader",
    "SessionPlaytimeRecorder",
    "SessionPostExitSync",
    "SettingsPersister",
    "SgdbArtworkCache",
    "Sleeper",
    "SteamConfigStore",
    "SteamGridDbApi",
    "SyncRunRepository",
    "SystemM3uSupportFn",
    "SystemResolver",
    "SystemSupportedExtensionsFn",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UuidGen",
]
