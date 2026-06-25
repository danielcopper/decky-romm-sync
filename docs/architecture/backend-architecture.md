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

| Module                    | Domain                                                                                                                                                                                                                                                                                                                                               |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `library/`                | LibraryService façade — fetch ROMs, preview/apply sync, per-unit shortcut delivery, `roms`/`SyncRun` writes + queries (decomposed; see below)                                                                                                                                                                                                        |
| `saves/`                  | SaveService aggregate — `.srm` upload/download, conflict detection, slots, versions (decomposed; see below)                                                                                                                                                                                                                                          |
| `downloads.py`            | DownloadService — ZIP extraction, M3U, progress, bounded-concurrency download queue (Semaphore(2) + reserved-bytes pre-flight); cancel/cleanup never deletes a live install                                                                                                                                                                          |
| `firmware.py`             | FirmwareService — BIOS registry, downloads, per-core filtering; `get_firmware_status` ships per-platform `bios_level` (ok/partial/missing via `domain.bios.compute_bios_level`) + `required_count`/`required_downloaded`/`server_count`/`local_count` so the System page reads the decision off the payload (#461)                                   |
| `session_lifecycle.py`    | SessionLifecycleService — post-exit orchestration (playtime + post-exit save sync + achievement sync + migration refresh)                                                                                                                                                                                                                            |
| `migration.py`            | MigrationService — RetroDECK path-change detection + file migration, save-sort change detection + conflict resolution                                                                                                                                                                                                                                |
| `steamgrid.py`            | SteamGridService — SteamGridDB fetch, cache, icons                                                                                                                                                                                                                                                                                                   |
| `artwork.py`              | ArtworkService — cover art download, staging, cleanup                                                                                                                                                                                                                                                                                                |
| `game_detail.py`          | GameDetailService — game detail page data aggregation                                                                                                                                                                                                                                                                                                |
| `playtime.py`             | PlaytimeService — session recording into `rom_playtime`, RomM-note reconciliation (session-end push + pull-only reconcile-on-view)                                                                                                                                                                                                                   |
| `achievements.py`         | AchievementsService — progress, caching, RA username                                                                                                                                                                                                                                                                                                 |
| `settings.py`             | SettingsService — settings reads/writes, Steam Input config                                                                                                                                                                                                                                                                                          |
| `rom_removal.py`          | RomRemovalService — ROM file deletion + `rom_installs` cleanup via the UoW; keeps the `roms` row, playtime, and saves per ADR-0007                                                                                                                                                                                                                   |
| `cores.py`                | CoreService — available-core lookup, per-game core pin/clear (`roms.emulator_override`), per-platform core write (`settings.json` `platform_cores`) + fan-out re-bake; see [Core and Emulator Selection](core-emulator-selection.md)                                                                                                                 |
| `active_core_resolver.py` | ActiveCoreResolver — the single per-ROM read seam: `active_core_for_rom(rom_id)` folds the per-game DB override + the per-platform `settings.json` core over the system layer (per-game override → per-platform core → es_systems default → core_defaults). Every per-game core read + every launch bake draws from it                               |
| `shortcut_removal.py`     | ShortcutRemovalService — shortcut removal; unbinds the ROM in `roms` (keeps the row per ADR-0007)                                                                                                                                                                                                                                                    |
| `metadata.py`             | MetadataService — ROM metadata reads from `rom_metadata` (7-day TTL), app_id mapping                                                                                                                                                                                                                                                                 |
| `launch_gate.py`          | LaunchGateService — pre-launch gate (rom lookup, install check, save status)                                                                                                                                                                                                                                                                         |
| `startup_healing.py`      | StartupHealingService — prunes stale `rom_installs` rows against disk on load (via the UoW) + reconciles orphaned `running` SyncRuns (a hard crash leaves a `running` row → marked errored) + `get_installed_relaunch_options()` builds the startup launch-options reconcile items (see [StartupHealingService notes](#startuphealingservice-notes)) |
| `connection.py`           | ConnectionService — connection test + RomM minimum-version gate + Client API Token lifecycle (mint/establish via credentials, host-bound to the minting origin; see [ConnectionService notes](#connectionservice-notes))                                                                                                                             |
| `protocols/`              | Protocol interfaces grouped by concern (see [Protocol Interfaces](#protocol-interfaces))                                                                                                                                                                                                                                                             |

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

**Per-unit wait: timeout vs. cancel.** The orchestrator emits `sync_apply_unit`, then waits on `unit_complete_event`
(heartbeat-clocked) for the frontend's `report_unit_results` ack. When the wait returns `None` the teardown branches on
the cause (#1052):

- **User cancel** (`is_cancelling()` already True at the return) — in-flight work is intentionally discarded: clear
  `pending_sync`, null `unit_complete_event`. A stray late ack then no-ops.
- **Heartbeat timeout** (still RUNNING) — the frontend has already created this unit's Steam shortcuts and will fire a
  late `report_unit_results`. The orchestrator **keeps** `pending_sync` + `unit_complete_event`, flags `unit_abandoned`,
  and stashes the unit's ROMs in `pending_unit_roms`, then flips CANCELLING so the loop stops.
  `SyncReporter.report_unit_results` observes `unit_abandoned` and drives `commit_unit_results` **itself** (rebuilding
  `acked_roms` from the stash so metadata is stamped too), persisting the delivered bindings instead of leaving orphan
  shortcuts that the next sync re-creates as duplicates. The committed binding is mapped by the next sync's
  existing-shortcut scan, so no active orphan deletion is needed (a Steam shortcut is the sole record of its tile).

**Run/unit identity on the ack (#1041).** Every `sync_apply_unit` event carries the `run_id` (the run's
`current_sync_id` UUID) and the `unit_id` (the `WorkUnit.id`); the frontend echoes both back on the
`report_unit_results` ack. The orchestrator stamps the dispatched unit's id into `active_unit_id` just before it emits,
and `report_unit_results` validates the ack against it: the `run_id` must match `current_sync_id` **and** the `unit_id`
must match `active_unit_id` (both compared by string value, since a platform's id is numeric and a collection's is a
string). An ack that fails the check — a **late ack from a cancelled run** arriving while a fresh run is in flight, or a
stray ack for a different unit — is ignored (logged at debug, returns `{success: True, count: 0, ignored: True}`): it is
neither recorded, signalled, nor committed, so it can never be credited to the wrong run/unit. `active_unit_id` survives
the heartbeat-timeout abandon window (the same unit's late ack must still validate) and is cleared once the unit commits
or is cancelled. On the frontend side, the unit handler **does not send the ack at all once cancel has been requested**
— the first line of defence against a cancelled run's bindings landing in whatever run started next.

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
renamed after a game-named playlist rather than a generically-named cue (`disc1.cue/`). Single-disc `.chd`/`.iso` arrive
as single-file downloads that never reach the extraction path, so they get no playlist.

**M3U is platform-gated on ES-DE's own extension list** ([ADR-0013](../adr/0013-platform-gated-m3u-via-es-systems.md)).
The file-count rule above only runs when the ROM's system actually supports `.m3u`. RomM bundles a platform-blind `.m3u`
into the ZIP for **every** multi-file game, including cartridge systems (Switch `.nsp`, Xbox 360 `.iso`) whose emulators
have no playlist concept — so an extension-only heuristic wrongly produced a `<Game>.m3u/` folder that never collapsed.
The plugin now asks whether ES-DE lists `.m3u` as a supported extension for that system, read from the same
`es_systems.xml` ES-DE uses to decide directory-collapse, via `CoreResolver.system_supports_m3u(system)` exposed through
the `SystemM3uSupportFn` Protocol (`services/protocols/`) and threaded into `DownloadService` from bootstrap. When the
answer is `False`, no `.m3u` is generated **and** the bundled one is never chosen as the launch file
(`detect_launch_file` skips its `.m3u` preference), so selection falls through to the real game file and the folder is
named `<Game>.nsp/` / `<Game>.iso/` instead. The capability crosses the service/domain seam as a plain `bool` — the
domain functions (`needs_m3u`, `detect_launch_file`) take `m3u_supported`, never a system name or an adapter. The
bundled `.m3u` is left inert on disk, never deleted. When `es_systems.xml` cannot be found the answer defaults to
`False` (safe: a missing playlist only degrades disc-switching, a wrong one breaks the launch).

Filesystem writes go through `DownloadFileAdapter`. ZIP extraction is ZIP-slip protected and streamed: `extract_zip`
copies each member in chunks and reports byte progress through an optional callback, so a multi-file ROM emits
`download_progress` frames with `status: "extracting"` (`bytes_downloaded`/`total_bytes` over the **uncompressed**
total, `resumable: false`) after the transfer hits 100%. The frontend reuses the same event — no new event name — to
switch the download button and QAM queue into the non-cancellable **Extracting…** phase. Single-file downloads never
emit it.

**Bounded concurrency + reserved-bytes pre-flight**: at most **two** ROMs transfer at once, gated by an
`asyncio.Semaphore(2)` around the transfer + post-IO critical section. `start_download` enters the queue with status
**`queued`** and reserves the download's required bytes in `_reserved_bytes[rom_id]`; `_do_download` flips the status to
`downloading` only once it acquires the semaphore (emitting a `download_progress` `status: "queued"` frame first if it
has to wait), and releases the reservation in its `finally`. The disk pre-flight accounts for siblings' outstanding
reservations (`free_space - sum(reserved) < required`) so two concurrent downloads that each fit alone but not together
can't both pass — the second is rejected with an `insufficient_space` failure.

**Cancel reaches the UI and never destroys a live install**: cancelling a download emits a terminal `download_progress`
`status: "cancelled"` frame so the frontend resets the button out of its downloading state (the cancel path used to be
silent). Because executor threads run to completion regardless of cancellation, a cancel that **loses the race** to a
just-committed install is reconciled (`_reconcile_post_io` awaits the in-flight post-IO future): if the install
committed, the download is surfaced as **completed** (launch options baked, `download_complete` emitted) rather than
torn down. `_cleanup_partial_download` removes **only** the transient transfer artifacts (`.zip.tmp` / `.tmp`) and, for
a multi-file ROM that did **not** commit, the extract dir(s) this download created — it **never** deletes the bare
`target_path`, so a re-download that fails mid-stream (or a cancel that lost the race) cannot destroy a pre-existing or
just-committed install.

#### ConnectionService notes

**A Client API Token is bound to the server it was minted against.** When the token is minted, the canonical origin of
`romm_url` (full `scheme://host[:port]`, default ports folded out, path/query dropped — `lib/url_host.normalize_origin`)
is stored alongside it as `romm_api_token_origin`. `RommHttpAdapter.auth_header()` attaches the bearer **only** when
that stored origin matches the current `romm_url` origin; on a mismatch it raises `TokenHostMismatchError` instead of
sending the credential to a host the token was not minted for. The error is non-retryable and maps to a `config_error`
failure (`Your saved RomM login is for a different server. Sign in again to continue.`), so every data flow fails fast
until the user re-signs-in. `https://h` and `http://h` are deliberately **different** origins — a plaintext downgrade is
a different destination, not the same one. A legacy token minted before origin stamping carries
`romm_api_token_origin =
None` and is treated as un-bound: it is still attached (never blocked) so existing installs
keep working until their next sign-in stamps the origin.

**Sign-in ordering: validate → probe → mint → persist (one atomic save).** `establish_token` trims the entered URL and
rejects a non-http(s) value before any network call. It then holds the candidate URL in memory only — clearing the
stored token in memory first so the version probe never carries the old server's bearer to the candidate host — and
persists nothing until the mint succeeds. On any failure (probe unreachable, version too old, forbidden/error mint, no
usable token, or a disk error) the in-memory auth state is rolled back to the previous working URL + token, and because
disk was never touched the prior working credentials survive a failed sign-in. Only a successful mint commits
`romm_url` + SSL flag + token + id + origin to disk in a single `save_settings()` call.

**The old-token DELETE is origin-guarded.** RomM scopes a Client API Token to the account, and re-auth deletes the
device's previous token. That DELETE is only fired when the old token's stored origin matches the new URL's origin
(same-server re-auth) — replaying it against a different server would delete an unrelated token there, so the DELETE is
skipped (and logged) when the origins differ or the old origin is unknown. The DELETE uses Basic auth from the one-time
credentials, unaffected by the cleared bearer.

The no-sign-in URL change path (`SettingsService.save_server_url`) deliberately does not touch the token, so pointing
the URL at a different origin leaves the stored token's origin mismatched and the auth-header guard makes subsequent
data flows fail fast with `config_error` until the user signs in again.

**Server-supplied paths are validated, fail-stop on traversal**: every server-supplied path component — the firmware
`file_name`, the ROM platform slug, and post-extraction URL-decoded ZIP member names — is checked through
`lib/path_safety` (`safe_join` for realpath containment, `safe_path_component` for single-component names) before any
write. A traversal attempt (`../`, an absolute path, or a `%2e%2e%2f`-encoded ZIP member that decodes to `../` after the
pre-decode ZIP-slip check passes) **aborts the whole download** rather than skipping the offending entry:
already-extracted members are cleaned up (no half-installed ROM), a canonical
`{"success": false, "reason": "path_traversal", "message": ...}` failure is returned, and the `download_failed` event
fires so the UI doesn't hang on "downloading". Firmware downloads surface the same canonical failure from
`download_firmware`.

#### StartupHealingService notes

Beyond the disk-prune and orphaned-`SyncRun` reconciliation, this service owns the **startup launch-options reconcile**
(#1043). `launch_options` (the full Steam-shortcut launch command) is written only event-driven — at sync, at
download-complete, and on RetroDECK-home migration (ADR-0009) — so any path that misses its bake leaves an installed
shortcut stuck on the `""` placeholder, and `bin/rom-launcher` then runs with no args and exits non-zero. There was no
backstop short of a Force Full Sync or uninstall/reinstall.

`get_installed_relaunch_options()` is the read half of the fix: a 0-arg read that returns `[{app_id, launch_options}]`
for every ROM that is both **installed** (has a `rom_installs` row) and **bound** (its `roms.shortcut_app_id` is set).
It snapshots the install/ROM rows in one short read UoW, then re-bakes each command **outside** that UoW through the
same `active_core` / `disc_resolver` seams every other bake site uses — resolving inside the iteration UoW would
deadlock, since `ActiveCoreResolver.active_core_for_rom` opens its own UoW (the per-connection write lock is not
re-entrant). Uninstalled and unbound ROMs are skipped by construction. The callable is read-only and **not**
migration-gated.

The frontend pulls this on mount, once the backend is proven reachable (it reuses the app-id/metadata init's
retry/backoff, not a second loop), and confirm-sets each entry via the existing `setLaunchOptionsConfirmed`
fire-then-poll. The pass is **idempotent and appId-safe**: re-confirming a correct command matches the read-back
instantly, and `launch_options` is not part of the appId CRC32, so artwork, collections, and the shortcut identity
survive. This heals drift from every cause at the next plugin load.

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
- **Crash-safe atomic writes**: `settings.json` is written with the durable write-tmp → `fsync(tmp)` → `os.replace()` →
  `fsync(dir)` recipe. The temp file's bytes are forced to disk **before** the rename, and the directory entry the
  rename creates is forced to disk **after** it. This closes the power-loss window on the Steam Deck's ext4: without the
  fsyncs, a crash after the rename but before the kernel flushed could leave a truncated or empty `settings.json` —
  which boot rewrites every run, so the window recurred. The directory fsync is best-effort: on the rare filesystem that
  rejects it, the error is logged at debug and swallowed (the content is already durable via the temp-file fsync).
- **Corrupt-file quarantine (never silently factory-reset)**: a `FileNotFoundError` on read is a legitimate first run —
  defaults are returned silently, no backup, no flag. An **unparseable** `settings.json` (a `JSONDecodeError`, e.g. a
  truncated file from a prior crash) is the data-loss hazard: returning defaults silently would let the immediate
  bootstrap save overwrite the corrupt file, wiping the user's RomM URL, API token, SGDB key, and platform/collection
  selections with no trace. Instead the adapter logs the corruption loudly at error level, renames the unparseable file
  aside to `settings.json.corrupt-<ts>` (the `<ts>` is the injected `Clock`'s epoch seconds — filesystem-safe), and sets
  a transient in-memory `corrupt_reset` flag before returning defaults. If the backup rename itself fails (e.g.
  permissions), the error is logged and defaults are still returned so boot never crashes. Bootstrap reads that
  transient flag after migration and — before the immediate save — folds it into the settings dict as a **persistent**
  `_settings_reset_notice` marker (`{"backed_up_to": <basename>}`), so it survives a plugin reload. The frontend reads
  it via the non-consuming `get_settings_reset_notice` callable and surfaces a persistent notice — a QAM `PanelSection`
  banner (with a **Dismiss** button) plus a game-detail `WarningCard` (informational; its copy points the user to the
  QAM to dismiss) — **not a toast** — telling the user their settings were reset and where the backup landed so they can
  re-enter the server URL and sign in. The marker is cleared **only by an explicit user acknowledgement**: the QAM
  Dismiss button calls `dismiss_settings_reset_notice`, which pops `_settings_reset_notice` and persists; the frontend
  clears the shared store on success so the banner and every game-detail card disappear at once. Sign-in does **not**
  clear the notice — the user decides when they have read it.
- **Version never down-stamps**: on write, the `version` field is stamped to `max(stored_version, _SETTINGS_VERSION)`. A
  file written by a **newer** plugin (stored version > current) is preserved as-is rather than down-stamped, so a later
  re-upgrade does not re-run migrations against down-stamped data. An absent or older version is stamped up to the
  current `_SETTINGS_VERSION`.

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
