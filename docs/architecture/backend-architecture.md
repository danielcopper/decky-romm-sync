# Backend Architecture

## Overview

The Python backend follows **Cosmic Python** ("Architecture Patterns with Python") adapted for a single-user Decky plugin. Code is split into four layers with a strictly enforced dependency direction:

- **`services/`** — orchestration. Business logic and the public callable surface.
- **`adapters/`** — I/O. Everything that touches the network, the filesystem, the clock, or Steam.
- **`domain/`** — pure compute. Functions in, values out; no I/O, no state mutation, no service/adapter imports.
- **`lib/`** — cross-cutting utilities independent of every other layer.
- **`models/`** — data shapes (TypedDicts, dataclasses) independent of every other layer.

Services depend on **Protocols** (defined in `services/protocols/`), never on concrete adapter classes. Adapters implement those Protocols. `bootstrap.py` is the composition root — the only place where concrete adapters meet services. `main.py` owns the Decky lifecycle and the callable surface; it holds no business logic.

```python
class Plugin:
    # No base classes — pure composition
    # Owns: the Decky lifecycle (_main / _unload) and the callable surface
    # Delegates: all business logic to services, all I/O to adapters
```

## Dependency Diagram

```text
main.py (Plugin — Decky lifecycle + callable routing)
    ↓ calls
bootstrap.py (composition root: bootstrap() builds adapters, wire_services() builds services)
    ↓ creates
┌─────────────────────────────────────────────────────────┐
│ Adapters (own all I/O — implement Protocols)            │
│   RommHttpAdapter / RommApiAdapter — RomM REST          │
│   SteamConfigAdapter — Steam VDF, grid dir, Steam Input │
│   SteamGridDbAdapter / SgdbArtworkCacheAdapter — SGDB   │
│   PersistenceAdapter (+ persister adapters) — JSON I/O  │
│   RegistryStoreAdapter / MetadataCacheStoreAdapter      │
│   CoverArtFileStore / DownloadFile / DownloadQueue      │
│   FirmwareFile / MigrationFile / RomFile / SaveFile     │
│   RetroDeckPaths / RetroArchConfig / RetroArchCoreInfo  │
│   CoreResolver / GamelistXmlEditor (ES-DE)              │
│   SystemClock / SystemUuidGen / AsyncioSleeper          │
│   HostnameAdapter / PathProbe / PluginMetadata          │
└────────────────────────┬────────────────────────────────┘
                         │ injected via *ServiceConfig
┌────────────────────────▼────────────────────────────────┐
│ Services (depend on Protocols, not concrete adapters)   │
│   LibraryService        SaveService                     │
│   DownloadService       PlaytimeService                 │
│   FirmwareService       SteamGridService                │
│   MetadataService       AchievementsService             │
│   MigrationService      GameDetailService               │
│   ArtworkService        RomRemovalService               │
│   ShortcutRemovalService  SettingsService               │
│   CoreService           ConnectionService               │
│   StartupHealingService LaunchGateService               │
│   SessionLifecycleService                               │
└────────────────────────┬────────────────────────────────┘
                         │ depend on
┌────────────────────────▼────────────────────────────────┐
│ Protocols (services/protocols/) — grouped topically:    │
│   transport / determinism / persistence / paths /       │
│   infra / files / cross_service                         │
└─────────────────────────────────────────────────────────┘

Domain (domain/) — pure compute, imported by services and adapters; imports nothing above it.
```

Arrow direction: depends-on (A -> B means A uses B).

## The `XxxServiceConfig` constructor pattern

Every service takes a **single** `config` keyword argument — a frozen dataclass named `<ServiceName>Config`. All dependencies live in the config: Protocol-typed adapters, infrastructure seams (event loop, logger, `Clock`, `UuidGen`, `Sleeper`), persistence callbacks, and settings-derived values. There are no bare-param or mixed constructors.

```python
sync_service = LibraryService(
    config=LibraryServiceConfig(
        romm_api=...,           # Protocol-typed adapter
        steam_config=...,       # Protocol-typed adapter
        clock=...,              # Clock Protocol
        uuid_gen=...,           # UuidGen Protocol
        sleeper=...,            # Sleeper Protocol
        state_persister=...,    # StatePersister Protocol
        metadata_service=...,   # cross-service Protocol-typed peer
        artwork=...,
        # ...
    ),
)
```

Outer services keep the `Service` token in both names (`SteamGridService` + `SteamGridServiceConfig`). Sub-services may use role-based names without the token when it reads more naturally (`SyncEngine` + `SyncEngineConfig`, `SyncOrchestrator` + `SyncOrchestratorConfig`).

## Module Responsibilities

### Services (`py_modules/services/`)

Two services are large enough to be decomposed into sub-service packages (`services/library/` and `services/saves/`); the rest are single modules. A service over ~700 LOC is the decomposition signal.

| Module | Domain |
| --- | --- |
| `library/` | LibraryService façade — fetch ROMs, preview/apply sync, per-unit shortcut delivery, registry queries (decomposed; see below) |
| `saves/` | SaveService aggregate — `.srm` upload/download, conflict detection, slots, versions (decomposed; see below) |
| `downloads.py` | DownloadService — ZIP extraction, M3U, fcntl-locked queue, progress |
| `firmware.py` | FirmwareService — BIOS registry, downloads, per-core filtering |
| `session_lifecycle.py` | SessionLifecycleService — post-exit orchestration (playtime + post-exit save sync + achievement sync + migration refresh) |
| `migration.py` | MigrationService — RetroDECK path-change detection + file migration, save-sort change detection + conflict resolution |
| `steamgrid.py` | SteamGridService — SteamGridDB fetch, cache, icons |
| `artwork.py` | ArtworkService — cover art download, staging, cleanup |
| `game_detail.py` | GameDetailService — game detail page data aggregation |
| `playtime.py` | PlaytimeService — session recording, RomM notes |
| `achievements.py` | AchievementsService — progress, caching, RA username |
| `settings.py` | SettingsService — settings reads/writes, Steam Input config |
| `rom_removal.py` | RomRemovalService — ROM file deletion + state cleanup |
| `cores.py` | CoreService — active-core lookup, core switching, gamelist edits |
| `shortcut_removal.py` | ShortcutRemovalService — shortcut removal + state cleanup |
| `metadata.py` | MetadataService — ROM metadata caching, 7-day TTL, app_id mapping |
| `launch_gate.py` | LaunchGateService — pre-launch gate (rom lookup, install check, save status) |
| `startup_healing.py` | StartupHealingService — registry/disk reconciliation on load |
| `connection.py` | ConnectionService — connection test + RomM minimum-version gate |
| `protocols/` | Protocol interfaces grouped by concern (see [Protocol Interfaces](#protocol-interfaces)) |

#### LibraryService decomposition (`services/library/`)

The library sync subsystem is a façade over three sub-services that coordinate through a shared `LibrarySyncStateBox`:

| Module | Role |
| --- | --- |
| `service.py` | `LibraryService` façade — public callable surface; wires the sub-services and delegates |
| `fetcher.py` | `LibraryFetcher` — read-only RomM roundtrips: list platforms/collections, the incremental/full pagination loop, per-unit work-queue construction |
| `sync_orchestrator.py` | `SyncOrchestrator` — preview (read-only), the per-unit apply pipeline, cancel, the heartbeat clock, progress emission |
| `reporter.py` | `SyncReporter` — post-apply finalisation (artwork filenames, registry append, last-sync metadata) and registry-derived queries |
| `_state.py` | `LibrarySyncStateBox` — shared mutable in-flight sync state; the single source of truth threaded through every sub-service |

The pipeline is split **fetch (read-only) / apply (owns persistence)**: the fetcher never mutates the metadata cache or registry, and the metadata cache is stamped per applied unit (`MetadataExtractor.record_unit_metadata`). So a preview never mutates state, and an interrupted apply leaves only the units it already applied stamped — incremental, per-unit delivery.

#### DownloadService notes

RomM exposes three mutually exclusive file-layout flags on every ROM detail. They control how the server stores files and how the API serves them. The plugin maps each layout to a local on-disk path:

| RomM flag | RomM server layout | What `fs_name` is | Plugin local layout |
| --- | --- | --- | --- |
| `has_simple_single_file` | `roms/<platform>/<file>` — one file, flat | the filename | flat in platform folder: `roms/<platform>/<file>` |
| `has_nested_single_file` | `roms/<platform>/<folder>/<file>` — one file in a per-game folder | the **folder** name | flat in platform folder: `roms/<platform>/<file>` |
| `has_multiple_files` | per-game folder with multiple files (multi-disc, BIN+CUE, etc.) | the ZIP/folder name | extracted into per-game subfolder: `roms/<platform>/<fs_name_no_ext>/...` |

**`has_nested_single_file` quirk**: `fs_name` is the parent folder name, not the filename. The actual filename with extension lives in `files[0].file_name`. The plugin reads from `files[0].file_name` so the downloaded ROM lands with the correct extension (e.g. `Game.chd`, not the extension-less folder name `Game`). A defensive helper falls back to `fs_name` and warns if `files` is empty or missing.

**Why nested-single is flattened locally**: a nested-single-file ROM has no sidecars by definition — RomM would mark it `has_multiple_files` if any companion files existed. The parent folder adds no value at the local layer, so the plugin drops it and stores the ROM directly in the platform folder, matching the simple-single-file layout. Multi-file ROMs keep their per-game subfolder because they contain multiple related files that belong together.

Filesystem writes go through `DownloadFileAdapter`; the offline-aware download queue is `DownloadQueueAdapter` (fcntl-locked). ZIP extraction is ZIP-slip protected.

### Adapters (`py_modules/adapters/`)

Adapters own all I/O and implement the Protocols defined in `services/protocols/`. Selected adapters:

| Module | Role |
| --- | --- |
| `romm/http.py` | `RommHttpAdapter` — HTTP transport: auth, SSL, retry, User-Agent, platform map |
| `romm/romm_api.py` | `RommApiAdapter` — RomM REST surface (saves, ROMs, platforms, firmware, devices, notes) over the HTTP transport |
| `steam_config.py` | `SteamConfigAdapter` — Steam VDF read/write, grid dir, shortcut icon write, Steam Input config |
| `steamgriddb.py` | `SteamGridDbAdapter` — SteamGridDB REST client |
| `sgdb_artwork_cache.py` | `SgdbArtworkCacheAdapter` — on-disk SGDB artwork cache |
| `cover_art_file_store.py` | `CoverArtFileStoreAdapter` — RomM cover art staging on disk |
| `persistence.py` | `PersistenceAdapter` + per-domain persister adapters — settings/state/cache/save-sync JSON I/O |
| `registry_store.py` | `RegistryStoreAdapter` — shortcut registry reads/writes |
| `metadata_cache_store.py` | `MetadataCacheStoreAdapter` — metadata cache reads/writes |
| `download_file.py` / `download_queue.py` | `DownloadFileAdapter` / `DownloadQueueAdapter` — download filesystem + fcntl-locked queue |
| `firmware_file.py` / `migration_file.py` / `rom_files.py` / `save_file.py` | per-subtree filesystem adapters (BIOS, RetroDECK migration, ROM removal, local saves) |
| `retrodeck_paths.py` | `RetroDeckPathsAdapter` — reads `retrodeck.json` for ROMs/saves/BIOS/home paths |
| `retroarch_config.py` | `RetroArchConfigAdapter` — reads `retroarch.cfg` save-sort flags |
| `retroarch_core_info.py` | `RetroArchCoreInfoAdapter` — reads RetroArch `.info` files (`corename`, metadata) |
| `es_de_config.py` | `CoreResolver` + `GamelistXmlEditorAdapter` — ES-DE `es_systems.xml` / `gamelist.xml` |
| `system_clock.py` / `system_uuid_gen.py` / `asyncio_sleeper.py` | concrete `Clock` / `UuidGen` / `Sleeper` seams |
| `hostname.py` / `path_probe.py` / `plugin_metadata.py` / `debug_logger.py` | hostname, path-exists probe, `package.json` version reader, settings-aware debug logger |

#### PersistenceAdapter notes

- **File locking**: write methods acquire an exclusive `fcntl.flock` before touching the file, preventing concurrent writes from corrupting state.
- **Schema versioning**: every state file written includes a `version` field. On read, a mismatch causes the file to be treated as absent (cache discarded, state reset to defaults) rather than loading incompatible data.
- **Atomic writes**: data is written to a temporary file in the same directory, then renamed into place with `os.replace()`, so a crash mid-write never leaves a partial file.

### Domain (`py_modules/domain/`)

Domain modules contain pure logic with no I/O and no Decky imports. They take inputs and return outputs; anything stateless and I/O-free that would otherwise sit in a service lives here. Selected modules:

| Module | Role |
| --- | --- |
| `sync_action.py` | `compute_sync_action` — the save-sync decision algorithm. Returns `SyncAction` union (`Skip` / `Upload` / `Download` / `Conflict`). See [Save File Sync Architecture](save-file-sync-architecture.md). |
| `sync_diff.py` | ROM classification and platform/collection diff computation for the sync preview |
| `preview_delta.py` | `PreviewDelta` shape for the sync preview |
| `work_unit.py` | `WorkUnit` — the per-unit sync work item |
| `save_state.py` | `SaveSyncState` aggregate + `from_dict`/`to_dict` (schema migrations live here) |
| `save_path.py` / `save_attribution.py` / `save_status*.py` / `save_extensions.py` | save path resolution, uploader attribution, status DTO building |
| `firmware_paths.py` / `bios.py` | BIOS path computation and status formatting |
| `achievements.py` | achievement progress computation |
| `shortcut_data.py` | shortcut data building (registry entries, shortcut dicts) |
| `steam_categories.py` | Steam collection name computation |
| `sgdb_artwork.py` | SGDB asset-type/endpoint maps and `to_signed_app_id` |
| `installed_roms.py` / `rom_files.py` | installed-ROM detection, M3U generation, launch-file detection |
| `retroarch_core_info.py` | `parse_core_info` — pure parser for RetroArch `.info` files |
| `state_migrations.py` | `migrate_settings` / `migrate_state` for the main state files |
| `sync_state.py` | `SyncState` enum (idle, running, cancelling) |
| `emulator_tag.py` / `version.py` | emulator-tag formatting, version parsing, core-change detection |

**Config-source parsers** follow a dedicated domain+adapter template (pure parse in domain, I/O in adapter, callback Protocol into services). The full pattern, source catalog, and decisions log are on the [Config Source Parsers](config-source-parsers.md) page.

### Models (`py_modules/models/`)

TypedDicts and dataclasses describing on-disk and in-flight data shapes (`state.py`, `bios.py`, `metadata.py`, `metadata_patches.py`, `registry_patches.py`). Models import nothing from the other layers.

### Other

| File | Role |
| --- | --- |
| `main.py` | Plugin class — Decky lifecycle (`_main`/`_unload`) and the callable surface (one `async def` per `@callable`) |
| `bootstrap.py` | Composition root — `bootstrap()` builds adapters, `wire_services()` builds services |
| `lib/errors.py` | Exception hierarchy (`RommApiError`, `classify_error`) |
| `lib/list_result.py` | `ErrorCode` and the canonical callable failure shape |

## Composition Root (`bootstrap.py`)

The composition root has two functions:

1. **`bootstrap()`** — builds every adapter and loads + migrates settings, plugin state, and the metadata cache so the persister adapters bind the live mutable dicts at construction. Returns a typed `BootstrapResult` carrying four bundles (`adapters`, `stores`, `callbacks`, `runtime_adapters`) plus a small `handles` struct for Plugin-only outputs.

2. **`wire_services()`** — takes a `WiringConfig` (the four bundles plus `min_required_version`) and constructs every service, injecting each one's `*ServiceConfig`. Returns a dict of named service instances.

The two-phase split exists because adapter instantiation and state loading happen first (`bootstrap()`), then `main.py` composes the runtime bundle (event loop, `decky.emit`) and calls `wire_services()` so services receive references to the fully-populated state dicts. Some services are constructed before others to satisfy ordering constraints (e.g. `MigrationService` before `SaveService` so save sync observes fresh save-sort state). Forward references between peers are threaded via `LateBinding`.

Per the process-boundary rule, adapter instantiation never happens in `main.py`, and no service wiring happens in `bootstrap.py`'s caller other than via `wire_services()`.

## Protocol Interfaces

Services depend on Protocols, never on concrete adapter implementations. The Protocols live in the `services/protocols/` package, organised topically (consumers always deep-import `from services.protocols import X`):

- **`transport`** — external system clients: `RommApi` (and its narrowed facets `RommSaveApi`, `RommRomReader`, `RommDeviceApi`, `RommFirmwareApi`, `RommPlaytimeApi`, `RommLibraryApi`, `RommConnectionApi`, `RommPlatformReader`, `RommAchievementsApi`, `RommSyncApi`, `RommVersion`), `SteamConfigStore`, `SteamGridDbApi`.
- **`determinism`** — `Clock` / `UuidGen` / `Sleeper` test seams.
- **`persistence`** — `StatePersister`, `SettingsPersister`, `MetadataCachePersister`, `MetadataCacheStore`, `FirmwareCachePersister`, `SaveSyncStatePersister`, `ShortcutRegistryStore`, `PluginMetadataReader`.
- **`paths`** — `RetroDeckPaths`, `SystemResolver`, `CoreInfoProvider`, `CoreResolverFn`, `CoreNameProviderFn`, `RetroArchConfigReader`, `RetroArchCoreInfoReader`, `RetroArchSaveSortingProvider`, `GamelistXmlEditor`.
- **`infra`** — cross-cutting callable seams: `EventEmitter`, `DebugLogger`, `PathExistsReader`, `HostnameReader`, `PendingSyncReader`, `DownloadQueueCleanup`.
- **`files`** — filesystem seams: `CoverArtFileStore`, `DownloadFileStore`, `DownloadQueueStore`, `FirmwareFileStore`, `MigrationFileStore`, `RomFileStore`, `SaveFileStore`, `SgdbArtworkCache`.
- **`cross_service`** — narrowly-typed multi-method seams one service exposes to another so services stay independent: `BiosChecker`, `AchievementsReader`, `ArtworkManager`, `ArtworkRemover`, `MetadataExtractor`, `RetryStrategy`, `MigrationPendingFn`, `SaveSortChangeFn`, the `LaunchGate*` and `Session*` seams.

Protocol names carry a suffix that signals shape (`…Reader`, `…Provider`/`…Fn`, `…Store`, `…Cache`, `…Persister`; bare names for pervasive primitives like `Clock`).

`RommApiAdapter` implements `RommApi` over `RommHttpAdapter`, targeting RomM 4.8.1+ endpoints.

## Boundary Enforcement

### 1. import-linter (CI-enforced)

`.importlinter` declares the layer contracts:

```ini
# Services must not import concrete adapter implementations (Protocols OK)
[importlinter:contract:no-adapter-impl-in-services]
type = forbidden
source_modules = services
forbidden_modules = adapters.romm.http, adapters.romm.romm_api, adapters.steam_config, ...

# Adapters must not import services
[importlinter:contract:no-services-in-adapters]
type = forbidden
source_modules = adapters
forbidden_modules = services

# Utilities (lib/) must not import services, adapters, or domain
[importlinter:contract:utilities-independence]
type = forbidden
source_modules = lib
forbidden_modules = services, adapters, domain

# Domain must not import services or adapters (lib is allowed)
[importlinter:contract:domain-independence]
type = forbidden
source_modules = domain
forbidden_modules = services, adapters

# Models must not import services, adapters, domain, or lib
[importlinter:contract:models-independence]
type = forbidden
source_modules = models
forbidden_modules = services, adapters, domain, lib

# Services must not import stdlib I/O / non-deterministic primitives directly
[importlinter:contract:no-stdlib-io-in-services]
type = forbidden
source_modules = services
forbidden_modules = random, subprocess, threading, requests, time, uuid

# Services must be independent of each other
[importlinter:contract:service-independence]
type = independence
modules = services.library, services.saves, services.playtime, ...
```

Run with `PYTHONPATH=py_modules lint-imports` (or `mise run lint`). CI gates on this.

### 2. Cosmic Python call bans

`scripts/check_cosmic_call_bans.sh` (also bundled into `mise run lint`) complements the import-level guardrail at the call site: services may not call `datetime.now()` / `asyncio.sleep()` / `time.time()` / `time.monotonic()` / `uuid.uuid4()` / `random.*` directly — they inject the corresponding `Clock` / `Sleeper` / `UuidGen` Protocol instead.

### 3. Convention: underscore prefix

All internal methods use a `_` prefix. Public callables (exposed to the frontend via `callable()`) have no prefix. `main.py` callable methods delegate directly to the corresponding service method. Even synchronous callable bodies are `async def` — Decky's callable framework requires it.

## Service Dependency Summary

Every service receives its dependencies through a single `*ServiceConfig` dataclass. Cross-service dependencies are Protocol-typed (services never import each other's concrete classes). Selected wiring:

| Service | Key injected dependencies |
| --- | --- |
| **LibraryService** | `RommLibraryApi`, `SteamConfigStore`, `MetadataExtractor`, `ArtworkManager`, `Clock`/`UuidGen`/`Sleeper`, persisters, `ShortcutRegistryStore` |
| **SaveService** | `RommApi`, `RetryStrategy`, `SaveFileStore`, `SaveSyncStatePersister`, `Clock`, `RetroDeckPaths`, core-name/active-core providers, migration-detect callbacks |
| **DownloadService** | `RommApi`, `DownloadFileStore`, `DownloadQueueStore`, `RetroDeckPaths`, `Clock`/`Sleeper`, migration-pending callback |
| **FirmwareService** | `RommApi`, `FirmwareFileStore`, `FirmwareCachePersister`, `CoreInfoProvider`, `RetroDeckPaths` |
| **SteamGridService** | `SteamGridDbApi`, `RommApi`, `SteamConfigStore`, `SgdbArtworkCache`, `ShortcutRegistryStore`, `PendingSyncReader` |
| **MigrationService** | `MigrationFileStore`, `RetroDeckPaths`, save-sort/active-core/core-name providers, BIOS-index callback |
| **GameDetailService** | `BiosChecker`, `AchievementsReader` (cross-service), `Clock` |
| **RomRemovalService** | `RomFileStore`, `RetroDeckPaths`, `StatePersister`, `SaveSyncStatePersister`-writer peer, `DownloadQueueCleanup` peer |
| **ShortcutRemovalService** | `SteamConfigStore`, `ArtworkRemover` peer, `StatePersister`, `ShortcutRegistryStore` |
| **SessionLifecycleService** | `Session*` cross-service seams (playtime / post-exit sync / achievement sync / migration reader) |
| **LaunchGateService** | `LaunchGateRomLookup`, `LaunchGateInstalledChecker`, `LaunchGateSaveStatusReader` cross-service seams |
| **ConnectionService** | `RommConnectionApi`, `min_required_version` |

All services also receive shared state (`state`, `settings`, `metadata_cache`, `save_sync_state`), the event loop, the logger, and the `DebugLogger` Protocol through their config.
