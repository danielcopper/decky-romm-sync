# Backend Architecture

## Overview

The Python backend follows **Cosmic Python** ("Architecture Patterns with Python") adapted for a single-user Decky
plugin. Code is split into four layers with a strictly enforced dependency direction:

- **`services/`** — orchestration. Business logic and the public callable surface.
- **`adapters/`** — I/O. Everything that touches the network, the filesystem, the clock, or Steam.
- **`domain/`** — pure compute. Functions in, values out; no I/O, no state mutation, no service/adapter imports.
- **`lib/`** — cross-cutting utilities independent of every other layer.
- **`models/`** — data shapes (TypedDicts, dataclasses) independent of every other layer.

Services depend on **Protocols** (defined in `services/protocols/`), never on concrete adapter classes. Adapters
implement those Protocols. `bootstrap.py` is the composition root — the only place where concrete adapters meet
services. `main.py` owns the Decky lifecycle and the callable surface; it holds no business logic.

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
│   SqliteUnitOfWork (+ repository adapters) — SQLite I/O │
│   CoverArtFileStore / DownloadFile                      │
│   FirmwareFile / MigrationFile / RomFile / SaveFile     │
│   RetroDeckPaths / RetroArchConfig / RetroArchCoreInfo  │
│   CoreResolver (ES-DE es_systems.xml)                   │
│   PlatformCoreReaderAdapter (settings platform_cores)   │
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

Every service takes a **single** `config` keyword argument — a frozen dataclass named `<ServiceName>Config`. All
dependencies live in the config: Protocol-typed adapters, infrastructure seams (event loop, logger, `Clock`, `UuidGen`,
`Sleeper`), persistence callbacks, and settings-derived values. There are no bare-param or mixed constructors.

```python
sync_service = LibraryService(
    config=LibraryServiceConfig(
        romm_api=...,           # Protocol-typed adapter
        steam_config=...,       # Protocol-typed adapter
        clock=...,              # Clock Protocol
        uuid_gen=...,           # UuidGen Protocol
        sleeper=...,            # Sleeper Protocol
        uow_factory=...,        # UnitOfWorkFactory Protocol (roms / sync_runs / kv_config / rom_metadata)
        artwork=...,            # cross-service Protocol-typed peer
        # ...
    ),
)
```

Outer services keep the `Service` token in both names (`SteamGridService` + `SteamGridServiceConfig`). Sub-services may
use role-based names without the token when it reads more naturally (`SyncEngine` + `SyncEngineConfig`,
`SyncOrchestrator` + `SyncOrchestratorConfig`).

## Module Responsibilities

### Services (`py_modules/services/`)

Two services are large enough to be decomposed into sub-service packages (`services/library/` and `services/saves/`);
the rest are single modules. A service over ~700 LOC is the decomposition signal.

| Module                    | Domain                                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `library/`                | LibraryService façade — fetch ROMs, preview/apply sync, per-unit shortcut delivery, `roms`/`SyncRun` writes + queries (decomposed; see below)                                                                                                                                                                          |
| `saves/`                  | SaveService aggregate — `.srm` upload/download, conflict detection, slots, versions (decomposed; see below)                                                                                                                                                                                                            |
| `downloads.py`            | DownloadService — ZIP extraction, M3U, fcntl-locked queue, progress                                                                                                                                                                                                                                                    |
| `firmware.py`             | FirmwareService — BIOS registry, downloads, per-core filtering; `get_firmware_status` ships per-platform `bios_level` (ok/partial/missing via `domain.bios.compute_bios_level`) + `required_count`/`required_downloaded`/`server_count`/`local_count` so the System page reads the decision off the payload (#461)     |
| `session_lifecycle.py`    | SessionLifecycleService — post-exit orchestration (playtime + post-exit save sync + achievement sync + migration refresh)                                                                                                                                                                                              |
| `migration.py`            | MigrationService — RetroDECK path-change detection + file migration, save-sort change detection + conflict resolution                                                                                                                                                                                                  |
| `steamgrid.py`            | SteamGridService — SteamGridDB fetch, cache, icons                                                                                                                                                                                                                                                                     |
| `artwork.py`              | ArtworkService — cover art download, staging, cleanup                                                                                                                                                                                                                                                                  |
| `game_detail.py`          | GameDetailService — game detail page data aggregation                                                                                                                                                                                                                                                                  |
| `playtime.py`             | PlaytimeService — session recording into `rom_playtime`, RomM-note reconciliation (session-end push + pull-only reconcile-on-view)                                                                                                                                                                                     |
| `achievements.py`         | AchievementsService — progress, caching, RA username                                                                                                                                                                                                                                                                   |
| `settings.py`             | SettingsService — settings reads/writes, Steam Input config                                                                                                                                                                                                                                                            |
| `rom_removal.py`          | RomRemovalService — ROM file deletion + `rom_installs` cleanup via the UoW; keeps the `roms` row, playtime, and saves per ADR-0007                                                                                                                                                                                     |
| `cores.py`                | CoreService — available-core lookup, per-game core pin/clear (`roms.emulator_override`), per-platform core write (`settings.json` `platform_cores`) + fan-out re-bake; see [Core and Emulator Selection](core-emulator-selection.md)                                                                                   |
| `active_core_resolver.py` | ActiveCoreResolver — the single per-ROM read seam: `active_core_for_rom(rom_id)` folds the per-game DB override + the per-platform `settings.json` core over the system layer (per-game override → per-platform core → es_systems default → core_defaults). Every per-game core read + every launch bake draws from it |
| `shortcut_removal.py`     | ShortcutRemovalService — shortcut removal; unbinds the ROM in `roms` (keeps the row per ADR-0007)                                                                                                                                                                                                                      |
| `metadata.py`             | MetadataService — ROM metadata reads from `rom_metadata` (7-day TTL), app_id mapping                                                                                                                                                                                                                                   |
| `launch_gate.py`          | LaunchGateService — pre-launch gate (rom lookup, install check, save status)                                                                                                                                                                                                                                           |
| `startup_healing.py`      | StartupHealingService — prunes stale `rom_installs` rows against disk on load (via the UoW) + reconciles orphaned `running` SyncRuns (a hard crash leaves a `running` row → marked errored)                                                                                                                            |
| `connection.py`           | ConnectionService — connection test + RomM minimum-version gate + Client API Token lifecycle (mint/establish via credentials)                                                                                                                                                                                          |
| `protocols/`              | Protocol interfaces grouped by concern (see [Protocol Interfaces](#protocol-interfaces))                                                                                                                                                                                                                               |

#### LibraryService decomposition (`services/library/`)

The library sync subsystem is a façade over three sub-services that coordinate through a shared `LibrarySyncStateBox`:

| Module                 | Role                                                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `service.py`           | `LibraryService` façade — public callable surface; wires the sub-services and delegates                                                          |
| `fetcher.py`           | `LibraryFetcher` — read-only RomM roundtrips: list platforms/collections, the incremental/full pagination loop, per-unit work-queue construction |
| `sync_orchestrator.py` | `SyncOrchestrator` — preview (read-only), the per-unit apply pipeline, cancel, the heartbeat clock, progress emission                            |
| `reporter.py`          | `SyncReporter` — post-apply finalisation (artwork filenames, per-unit `roms` upsert + `SyncRun` lifecycle) and the `roms`-derived queries        |
| `_state.py`            | `LibrarySyncStateBox` — shared mutable in-flight sync state; the single source of truth threaded through every sub-service                       |

The pipeline is split **fetch (read-only) / apply (owns persistence)**: the fetcher never mutates the `roms` registry or
`rom_metadata`, and the reporter's per-unit commit upserts each acked ROM's `roms` row and stamps its cached
`rom_metadata` in the same write Unit of Work (Rom row first, then metadata — FK-safe). So a preview never mutates
state, and an interrupted apply leaves only the units it already committed — incremental, per-unit delivery.

**Where the synced-ROM state lives.** The registry of synced ROMs, the last-sync timestamp, and the sync stats are
SQLite, not JSON. The reporter upserts each acked ROM into the `roms` table via `Rom.synced(...)` / `update_cover_path`
/ `assign_sgdb_id` (artwork and steamgrid patch `cover_path` / `sgdb_id` on the same aggregate during the per-unit
commit) and, in the same write UoW, stamps the ROM's cached `rom_metadata` (`build_rom_metadata` maps the live RomM
`metadatum` — Rom row saved first so the `rom_id` FK holds); the orchestrator drives the `SyncRun` lifecycle (`start` at
apply-dispatch, `complete` / `mark_cancelled` / `mark_errored` at finalize). `sync_stats.roms` is a registry-derived
bound-shortcut count computed at read time (the ROMs still bound to a shortcut in `roms`, i.e. `shortcut_app_id` not
NULL), not a stored scalar. The old JSON `shortcut_registry` / `last_sync` / `sync_stats` are gone from this path; all
writes go through the `roms` / `sync_runs` Repository Protocols behind a narrow Unit of Work (per
[ADR-0006](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0006-narrow-unit-of-work-scope.md) the UoW
spans only the DB write, never the up-to-60s frontend ack). The platform `slug → display_name` map resolves live from
RomM each sync and is cached in a `kv_config` row for offline reads. Removing a shortcut **unbinds** the ROM
(`Rom.unbind_shortcut()` NULLs `shortcut_app_id`, keeping the row and its per-ROM children) rather than deleting it, per
[ADR-0007](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0007-rom-retention-identity-anchor.md).
The full schema and aggregate model are in [Database Design](database-design.md).

#### DownloadService notes

RomM exposes three mutually exclusive file-layout flags on every ROM detail. They control how the server stores files
and how the API serves them. The plugin maps each layout to a local on-disk path:

| RomM flag                | RomM server layout                                                | What `fs_name` is   | Plugin local layout                                                                                     |
| ------------------------ | ----------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------- |
| `has_simple_single_file` | `roms/<platform>/<file>` — one file, flat                         | the filename        | flat in platform folder: `roms/<platform>/<file>`                                                       |
| `has_nested_single_file` | `roms/<platform>/<folder>/<file>` — one file in a per-game folder | the **folder** name | flat in platform folder: `roms/<platform>/<file>`                                                       |
| `has_multiple_files`     | per-game folder with multiple files (multi-disc, BIN+CUE, etc.)   | the ZIP/folder name | extracted into per-game subfolder named after the launch file: `roms/<platform>/<launch-file-name>/...` |

**`has_nested_single_file` quirk**: `fs_name` is the parent folder name, not the filename. The actual filename with
extension lives in `files[0].file_name`. The plugin reads from `files[0].file_name` so the downloaded ROM lands with the
correct extension (e.g. `Game.chd`, not the extension-less folder name `Game`). A defensive helper falls back to
`fs_name` and warns if `files` is empty or missing.

**Why nested-single is flattened locally**: a nested-single-file ROM has no sidecars by definition — RomM would mark it
`has_multiple_files` if any companion files existed. The parent folder adds no value at the local layer, so the plugin
drops it and stores the ROM directly in the platform folder, matching the simple-single-file layout. Multi-file ROMs
keep their per-game subfolder because they contain multiple related files that belong together.

**Extract-vs-flat gate keys on `len(files) > 1`, not on `has_multiple_files`**: the plugin decides ZIP-extract vs
single-file download with the `is_multi_file_download` helper (`domain/rom_files.py`), which returns
`len(files) > 1 OR has_multiple_files`. This mirrors RomM's own download gate, which zips whenever the **total** file
count is not exactly 1. RomM computes `has_multiple_files` from **top-level** files only, so the two counts disagree for
a nested layout: a canonical Switch game (base file at the root plus `update/` and `dlc/` in subfolders) has exactly one
top-level file (`has_multiple_files=False`, `has_nested_single_file=True`) yet more than one total file, so RomM serves
a ZIP. Keying on `has_multiple_files` alone would take the single-file path and write the ZIP bytes verbatim into one
unreadable `.nsp`. The boolean is kept as a defensive fallback for payloads that omit `files`; a genuine nested-single
ROM has `len(files) == 1` and correctly stays on the flat single-file path.

**ES-DE directory-collapse rename**: a multi-file ROM is extracted into a staging folder named after the ZIP
(`fs_name_no_ext`), but the per-game folder is then renamed after the **detected launch file including its extension**
(e.g. `Final Fantasy VII (USA).m3u/` containing `Final Fantasy VII (USA).m3u`). ES-DE only collapses a directory into a
single game entry when the folder name matches the launch file's full name with extension; without the rename a
multi-disc game shows in ES-DE as a folder plus loose disc files. The launch file is only known after extraction (an
`.m3u` may be auto-generated — see below), so the rename happens last, after launch-file detection, via
`es_de_collapse_rename` (`domain/rom_files.py`) + the `DownloadFileStore.move_dir` whole-directory move. On a name
collision (target already exists) the rename is skipped and the staging folder is kept — never clobbered or merged.
Existing installs from before this feature keep their old folder layout until re-downloaded.

**M3U generation rule** (`needs_m3u` in `domain/rom_files.py`): a game-named `<fs_name_no_ext>.m3u` is auto-generated
(when no `.m3u` already exists) for **multi-disc** ROMs — two or more disc files of any kind (`.cue`/`.chd`/`.iso`) — so
the emulator can switch discs, **and** for **single-disc bin/cue** ROMs — exactly one `.cue` — so the extract dir is
renamed after a game-named playlist rather than a generically-named cue (`disc1.cue/`). The single-disc branch is scoped
strictly to `.cue`: bin/cue systems (PS1/PS2/Saturn/Sega CD/PC Engine CD, etc.) are M3U-friendly, whereas iso-based
GameCube/Wii (Dolphin) are never bin/cue and do not reliably launch from a single-entry M3U. Single-disc `.chd`/`.iso`
arrive as single-file downloads that never reach the extraction path, so they get no playlist.

Filesystem writes go through `DownloadFileAdapter`. ZIP extraction is ZIP-slip protected.

### Adapters (`py_modules/adapters/`)

Adapters own all I/O and implement the Protocols defined in `services/protocols/`. Selected adapters:

| Module                                                                     | Role                                                                                                                                                                          |
| -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `romm/http.py`                                                             | `RommHttpAdapter` — HTTP transport: auth, SSL, retry, User-Agent, platform map                                                                                                |
| `romm/romm_api.py`                                                         | `RommApiAdapter` — RomM REST surface (saves, ROMs, platforms, firmware, devices, notes) over the HTTP transport                                                               |
| `steam_config.py`                                                          | `SteamConfigAdapter` — Steam VDF read/write, grid dir, shortcut icon write, Steam Input config                                                                                |
| `steamgriddb.py`                                                           | `SteamGridDbAdapter` — SteamGridDB REST client                                                                                                                                |
| `sgdb_artwork_cache.py`                                                    | `SgdbArtworkCacheAdapter` — on-disk SGDB artwork cache                                                                                                                        |
| `cover_art_file_store.py`                                                  | `CoverArtFileStoreAdapter` — RomM cover art staging on disk                                                                                                                   |
| `persistence.py`                                                           | `PersistenceAdapter` + per-domain persister adapters — `settings.json` read/write plus the one-time legacy `save_sync_state.json` read that feeds the bootstrap settings fold |
| `repositories/`                                                            | `SqliteUnitOfWork` + per-aggregate repository adapters — SQLite I/O (the live persistence path; see [Database Design](database-design.md))                                    |
| `sqlite_migrations.py`                                                     | `apply_migrations` — schema migration runner (`db/migrations/NNN_*.sql`, `PRAGMA user_version`)                                                                               |
| `download_file.py`                                                         | `DownloadFileAdapter` — download filesystem                                                                                                                                   |
| `firmware_file.py` / `migration_file.py` / `rom_files.py` / `save_file.py` | per-subtree filesystem adapters (BIOS, RetroDECK migration, ROM removal, local saves)                                                                                         |
| `retrodeck_paths.py`                                                       | `RetroDeckPathsAdapter` — reads `retrodeck.json` for ROMs/saves/BIOS/home paths                                                                                               |
| `retroarch_config.py`                                                      | `RetroArchConfigAdapter` — reads `retroarch.cfg` save-sort flags                                                                                                              |
| `retroarch_core_info.py`                                                   | `RetroArchCoreInfoAdapter` — reads RetroArch `.info` files (`corename`, metadata)                                                                                             |
| `es_de_config.py`                                                          | `CoreResolver` — ES-DE `es_systems.xml` (system-layer default core + available cores); the gamelist is no longer read or written                                              |
| `system_clock.py` / `system_uuid_gen.py` / `asyncio_sleeper.py`            | concrete `Clock` / `UuidGen` / `Sleeper` seams                                                                                                                                |
| `hostname.py` / `path_probe.py` / `plugin_metadata.py` / `debug_logger.py` | hostname, path-exists probe, `package.json` version reader, settings-aware debug logger                                                                                       |

#### PersistenceAdapter notes

- **File locking**: write methods acquire an exclusive `fcntl.flock` before touching the file, preventing concurrent
  writes from corrupting state.
- **Schema versioning**: every state file written includes a `version` field. On read, a mismatch causes the file to be
  treated as absent (cache discarded, state reset to defaults) rather than loading incompatible data.
- **Atomic writes**: data is written to a temporary file in the same directory, then renamed into place with
  `os.replace()`, so a crash mid-write never leaves a partial file.

### Domain (`py_modules/domain/`)

Domain modules contain pure logic with no I/O and no Decky imports. They take inputs and return outputs; anything
stateless and I/O-free that would otherwise sit in a service lives here. Domain is stdlib + self only — it imports no
other internal layer (`lib` and `models` included). Aggregate roots and the enforcement that keeps them honest are
documented in [Database Design](database-design.md). Selected modules:

| Module                                                                            | Role                                                                                                                                                                                                                                                                                                                           |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `sync_action.py`                                                                  | `compute_sync_action` — the save-sync decision algorithm. Returns `SyncAction` union (`Skip` / `Upload` / `Download` / `Conflict`). See [Save File Sync Architecture](save-file-sync-architecture.md).                                                                                                                         |
| `sync_diff.py`                                                                    | ROM classification and platform/collection diff computation for the sync preview                                                                                                                                                                                                                                               |
| `preview_delta.py`                                                                | `PreviewDelta` shape for the sync preview                                                                                                                                                                                                                                                                                      |
| `work_unit.py`                                                                    | `WorkUnit` — the per-unit sync work item                                                                                                                                                                                                                                                                                       |
| `rom_save_state.py`                                                               | `RomSaveState` aggregate + `FileSyncState` value object — per-ROM save-sync state, backed by `rom_save_states` + `rom_save_files`                                                                                                                                                                                              |
| `save_path.py` / `save_attribution.py` / `save_status*.py` / `save_extensions.py` | save path resolution, uploader attribution, status DTO building                                                                                                                                                                                                                                                                |
| `firmware_paths.py` / `bios.py`                                                   | BIOS path computation and status formatting; `bios.py` holds the BIOS status dataclasses (`AvailableCore`, `BiosFileEntry`, `BiosStatus`) and owns the ok/partial/missing CLASSIFICATION boundary (`compute_bios_level` / `compute_bios_label`) — the single source of truth all surfaces read; phrasing + color stay UI-layer |
| `iso_time.py`                                                                     | `parse_iso` / `parse_iso_to_epoch` — ISO-8601 timestamp parsing (stdlib only)                                                                                                                                                                                                                                                  |
| `achievements.py`                                                                 | achievement progress computation                                                                                                                                                                                                                                                                                               |
| `shortcut_data.py`                                                                | shortcut data building (registry entries, shortcut dicts)                                                                                                                                                                                                                                                                      |
| `steam_categories.py`                                                             | Steam collection name computation                                                                                                                                                                                                                                                                                              |
| `sgdb_artwork.py`                                                                 | SGDB asset-type/endpoint maps and `to_signed_app_id`                                                                                                                                                                                                                                                                           |
| `installed_roms.py` / `rom_files.py`                                              | installed-ROM detection, M3U generation, launch-file detection                                                                                                                                                                                                                                                                 |
| `retroarch_core_info.py`                                                          | `parse_core_info` — pure parser for RetroArch `.info` files                                                                                                                                                                                                                                                                    |
| `state_migrations.py`                                                             | `migrate_settings` (`settings.json`) + `fold_legacy_save_sync_settings` (one-time legacy `save_sync_state.json` fold)                                                                                                                                                                                                          |
| `sync_state.py`                                                                   | `SyncState` enum (idle, running, cancelling)                                                                                                                                                                                                                                                                                   |
| `emulator_tag.py` / `version.py`                                                  | emulator-tag formatting, version parsing, core-change detection                                                                                                                                                                                                                                                                |

**Config-source parsers** follow a dedicated domain+adapter template (pure parse in domain, I/O in adapter, callback
Protocol into services). The full pattern, source catalog, and decisions log are on the
[Config Source Parsers](config-source-parsers.md) page.

### Models (`py_modules/models/`)

TypedDicts and dataclasses describing on-disk and in-flight data shapes (`state.py`, `metadata.py`). Models import
nothing from the other layers.

### Other

| File                 | Role                                                                                                          |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| `main.py`            | Plugin class — Decky lifecycle (`_main`/`_unload`) and the callable surface (one `async def` per `@callable`) |
| `bootstrap.py`       | Composition root — `bootstrap()` builds adapters, `wire_services()` builds services                           |
| `lib/errors.py`      | Exception hierarchy (`RommApiError`, `classify_error`)                                                        |
| `lib/list_result.py` | `ErrorCode` and the canonical callable failure shape                                                          |

## Composition Root (`bootstrap.py`)

The composition root has two functions:

1. **`bootstrap()`** — builds every adapter, applies the SQLite schema migrations, and loads + migrates `settings.json`
   (folding in the one-time legacy `save_sync_state.json` settings) so the settings persister binds the live mutable
   `settings` dict at construction. Returns a typed `BootstrapResult` carrying four bundles (`adapters`, `stores`,
   `callbacks`, `runtime_adapters`) plus a small `handles` struct for Plugin-only outputs.

2. **`wire_services()`** — takes a `WiringConfig` (the four bundles plus `min_required_version`) and constructs every
   service, injecting each one's `*ServiceConfig`. Returns a dict of named service instances.

The two-phase split exists because adapter instantiation and state loading happen first (`bootstrap()`), then `main.py`
composes the runtime bundle (event loop, `decky.emit`) and calls `wire_services()`. Services receive the `settings` dict
(the only field on `StateBundle`) plus the SQLite Unit-of-Work factory / repository handles for all relational state —
no plural in-memory state dicts remain. Some services are constructed before others to satisfy ordering constraints
(e.g. `MigrationService` before `SaveService` so save sync observes fresh save-sort state). Forward references between
peers are threaded via `LateBinding`.

Per the process-boundary rule, adapter instantiation never happens in `main.py`, and no service wiring happens in
`bootstrap.py`'s caller other than via `wire_services()`.

## Protocol Interfaces

Services depend on Protocols, never on concrete adapter implementations. The Protocols live in the `services/protocols/`
package, organised topically (consumers always deep-import `from services.protocols import X`):

- **`transport`** — external system clients: `RommApi` (and its narrowed facets `RommSaveApi`, `RommRomReader`,
  `RommDeviceApi`, `RommFirmwareApi`, `RommPlaytimeApi`, `RommLibraryApi`, `RommConnectionApi`, `RommPlatformReader`,
  `RommAchievementsApi`, `RommSyncApi`, `RommVersion`), `SteamConfigStore`, `SteamGridDbApi`.
- **`determinism`** — `Clock` / `UuidGen` / `Sleeper` test seams.
- **`persistence`** — `SettingsPersister`, `PluginMetadataReader`.
- **`paths`** — `RetroDeckPaths`, `SystemResolver`, `CoreInfoProvider`, `CoreResolverFn`, `CoreNameProviderFn`,
  `RetroArchConfigReader`, `RetroArchCoreInfoReader`, `RetroArchSaveSortingProvider`, `PlatformCoreReader`.
- **`infra`** — cross-cutting callable seams: `EventEmitter`, `DebugLogger`, `PathExistsReader`, `HostnameReader`,
  `PendingSyncReader`, `DownloadQueueCleanup`.
- **`files`** — filesystem seams: `CoverArtFileStore`, `DownloadFileStore`, `FirmwareFileStore`, `MigrationFileStore`,
  `RomFileStore`, `SaveFileStore`, `SgdbArtworkCache`.
- **`cross_service`** — narrowly-typed multi-method seams one service exposes to another so services stay independent:
  `BiosChecker`, `AchievementsReader`, `ArtworkManager`, `ArtworkRemover`, `RetryStrategy`, `MigrationPendingFn`,
  `SaveSortChangeFn`, the `LaunchGate*` and `Session*` seams.

Protocol names carry a suffix that signals shape (`…Reader`, `…Provider`/`…Fn`, `…Store`, `…Cache`, `…Persister`; bare
names for pervasive primitives like `Clock`).

`RommApiAdapter` implements `RommApi` over `RommHttpAdapter`, targeting RomM 4.8.1+ endpoints.

## Boundary Enforcement

Four CI-gated layers keep the dependency direction and the call-site rules from drifting. Aggregate-specific enforcement
(the `@cosmic_aggregate` decorator and the field-assignment check) is documented in
[Database Design](database-design.md).

### 1. import-linter (CI-enforced)

`.importlinter` declares the layer contracts:

```ini
# Services must not import concrete adapter implementations (Protocols OK)
[importlinter:contract:no-adapter-impl-in-services]
type = forbidden
source_modules = services
forbidden_modules = adapters

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

# Domain is pure compute — no dependency on any other internal layer
[importlinter:contract:domain-independence]
type = forbidden
source_modules = domain
forbidden_modules = services, adapters, lib, models

# Domain is stdlib + self only — no vendored third-party packages
[importlinter:contract:domain-stdlib-only]
type = forbidden
source_modules = domain
forbidden_modules = _vendor

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

The `service-independence` `modules` list is hand-enumerated, so `scripts/check_service_independence_contract.py`
(bundled into `mise run lint` and gated in CI) derives the expected services from `py_modules/services/` and fails if
the contract omits a service or carries a stale entry — keeping the list self-healing rather than silently rotting.

### 2. Cosmic Python call bans

`scripts/check_cosmic_call_bans.sh` (also bundled into `mise run lint`) complements the import-level guardrail at the
call site: services may not call `datetime.now()` / `asyncio.sleep()` / `time.time()` / `time.monotonic()` /
`uuid.uuid4()` / `random.*` directly — they inject the corresponding `Clock` / `Sleeper` / `UuidGen` Protocol instead.

### 3. Aggregate field-assignment check

`scripts/check_aggregate_field_assignment.py` (also bundled into `mise run lint`) is a small custom AST linter that
enforces the **mutation-only-via-methods** rule for aggregates — a rule no type checker can express directly. It
collects the class names decorated with `@cosmic_aggregate` in `domain/` (currently the 8 aggregate roots), then scans
`services/` for `<aggregate>.<field> = ...` assignments and fails CI on any it finds. The escape hatch is a trailing
`# pragma: no aggregate-check` on the offending line. Full detail in [Database Design](database-design.md).

### 4. Failure-shape dialect gate

`scripts/check_failure_shape.py --check` (also bundled into `mise run lint`) is a small custom AST linter that enforces
the **canonical failure shape** for dict-returning callables — every `success: False` return in `services/` must carry
both `reason` and `message` and must not carry the legacy `error_code` key or a second `error` key. It collapses the
three dialects that previously coexisted (`error_code`, `error`, and slug-less ad-hoc dicts) onto one vocabulary. The
two documented carve-outs (discriminated-status unions — a `status` key with no `success`; and partial-success payloads
carrying an additive `server_query_failed` / `recommended_action` flag) are pattern-exempt. Run without `--check` for
the report-mode inventory grouped by classification. The routing slugs come from `lib.list_result.ErrorCode` (the Lean
enum) plus bespoke plain-string reasons for non-server-reachability guards.

### 5. Enforced: underscore prefix

All internal methods use a `_` prefix; public callables (exposed to the frontend via `callable()`) have none. `main.py`
callable methods delegate directly to the corresponding service method. Even synchronous callable bodies are `async def`
— Decky's callable framework requires it.

This is no longer just a convention — basedpyright enforces it with `reportPrivateUsage = "error"`, so accessing a
`_`-prefixed name from outside its owning class is a hard type error. Tests are exempt via an `executionEnvironments`
override (white-box testing — inspecting and rebinding a system-under-test's private state — is an accepted pattern).
One corollary: a method one sub-service calls on a peer is part of that peer's **public** surface and carries no
underscore, which keeps `reportPrivateUsage` coherent with the saves-style peer-injection carve-out.

## Service Dependency Summary

Every service receives its dependencies through a single `*ServiceConfig` dataclass. Cross-service dependencies are
Protocol-typed (services never import each other's concrete classes). Selected wiring:

| Service                     | Key injected dependencies                                                                                                                                                                       |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **LibraryService**          | `RommLibraryApi`, `SteamConfigStore`, `ArtworkManager`, `Clock`/`UuidGen`/`Sleeper`, `SettingsPersister`, `UnitOfWorkFactory` (roms / sync_runs / kv_config / rom_metadata)                     |
| **MetadataService**         | `UnitOfWorkFactory` (reads `rom_metadata` / `roms`)                                                                                                                                             |
| **SaveService**             | `RommApi`, `RetryStrategy`, `SaveFileStore`, `UnitOfWorkFactory` (`rom_save_states` / `rom_save_files`), `Clock`, `RetroDeckPaths`, core-name/active-core providers, migration-detect callbacks |
| **DownloadService**         | `RommApi`, `DownloadFileStore`, `RetroDeckPaths`, `Clock`/`Sleeper`                                                                                                                             |
| **FirmwareService**         | `RommApi`, `FirmwareFileStore`, `CoreInfoProvider`, `RetroDeckPaths`, `UnitOfWorkFactory` (`firmware_cache`)                                                                                    |
| **SteamGridService**        | `SteamGridDbApi`, `RommApi`, `SteamConfigStore`, `SgdbArtworkCache`, `UnitOfWorkFactory` (sgdb_id on `roms`), `PendingSyncReader`                                                               |
| **MigrationService**        | `MigrationFileStore`, `RetroDeckPaths`, save-sort/active-core/core-name providers, BIOS-index callback                                                                                          |
| **GameDetailService**       | `BiosChecker`, `AchievementsReader` (cross-service), `Clock`, `UnitOfWorkFactory` (one read UoW over `roms` / `rom_installs` / `rom_save_states` / `rom_metadata` / `kv_config`)                |
| **AchievementsService**     | `RommAchievementsApi`, `Clock`, `DebugLogger`, `UnitOfWorkFactory` (reads `ra_id` from `roms`)                                                                                                  |
| **SettingsService**         | `SteamConfigStore`, `SettingsPersister`, `UnitOfWorkFactory` (reads bound `shortcut_app_id`s from `roms`)                                                                                       |
| **PlaytimeService**         | `RommPlaytimeApi`, `RetryStrategy`, `Clock`, `UnitOfWorkFactory` (reads/writes `rom_playtime`)                                                                                                  |
| **RomRemovalService**       | `RomFileStore`, `RetroDeckPaths`, `DownloadQueueCleanup` peer, `UnitOfWorkFactory` (reads/deletes `rom_installs`)                                                                               |
| **ShortcutRemovalService**  | `SteamConfigStore`, `ArtworkRemover` peer, `UnitOfWorkFactory` (unbinds via `roms`, offline name via `kv_config`)                                                                               |
| **SessionLifecycleService** | `Session*` cross-service seams (playtime / post-exit sync / achievement sync / migration reader)                                                                                                |
| **LaunchGateService**       | `LaunchGateRomLookup`, `LaunchGateInstalledChecker`, `LaunchGateSaveStatusReader` cross-service seams                                                                                           |
| **ConnectionService**       | `RommConnectionApi`, `SettingsPersister`, `min_required_version`                                                                                                                                |

Most services also receive the `settings` dict (`StateBundle`'s only field), the runtime infrastructure (event loop,
logger, the `DebugLogger` Protocol), and the `UnitOfWorkFactory` for relational state through their config. The old
in-memory `state` / `metadata_cache` / `save_sync_state` / `shortcut_registry` dicts are gone — every relational
read/write goes through the Unit of Work.
