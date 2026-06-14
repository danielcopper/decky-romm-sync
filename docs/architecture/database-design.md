# Database Design

## Overview

This page is the canonical home for the **aggregate domain model** behind the SQLite persistence migration (epic
[#271](https://github.com/danielcopper/decky-romm-sync/issues/271)). The migration replaced the JSON state files with a
SQLite database whose tables back a set of Cosmic Python aggregates.

The migration **and its teardown are complete**. The **enforcement infrastructure** (the decorator, the linters, and the
type-check rule that keep aggregates honest, [#788](https://github.com/danielcopper/decky-romm-sync/issues/788)), the
full **aggregate set** (the 8 aggregate roots, their fields, and their mutation methods), the **SQLite schema** (the
migration framework + `001_initial.sql`,
[#780](https://github.com/danielcopper/decky-romm-sync/issues/780)/[#781](https://github.com/danielcopper/decky-romm-sync/issues/781)),
the per-aggregate **Repository Protocols** ([#782](https://github.com/danielcopper/decky-romm-sync/issues/782)), and the
runtime **Unit of Work** + concrete `sqlite3` repository adapters
([#783](https://github.com/danielcopper/decky-romm-sync/issues/783)) are all in place — documented below. SQLite,
reached through the Unit of Work, is the **sole live persistence path for relational state**; the only remaining live
JSON file is `settings.json`.

The cutover ([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)) was a **hard cut** — SQLite started
empty, the JSON state was not migrated into it, and each JSON-era state class was deleted once its last consumer moved
over. Each vertical landed in turn:

- **library/roms** — the live sync path writes `roms` (the synced-ROM registry), `sync_runs` (the
  start→complete/cancel/error lifecycle that replaced the JSON `last_sync`/`sync_stats` scalars), and the `kv_config`
  `platform_slug → display_name` cache row, all through Repository Protocols + the Unit of Work.
- **metadata** — `rom_metadata` is written by the reporter's per-unit commit, the same write Unit of Work as the `roms`
  upsert (Rom row first, then metadata, so the `rom_id` FK is satisfied at commit and a ROM and its cached metadata land
  atomically); `MetadataService`/`GameDetailService` read it back from SQLite.
- **playtime** — `PlaytimeService` records sessions and reconciles RomM-note totals through the `rom_playtime` aggregate
  (session-end folds the duration in a short write UoW, then pushes to RomM outside the transaction; opening a game's
  detail page triggers a pull-only reconcile that folds the RomM note's total into `rom_playtime` via `reconcile_total`
  — total-only, never writing a note).
- **rom-removal + startup-healing** — `RomRemovalService.remove_rom`/`uninstall_all_roms` and
  `StartupHealingService.prune_stale_installed_roms` read and delete `rom_installs` through the Unit of Work; an
  uninstall (or stale prune) deletes only the on-disk files and the `rom_installs` row, never the `roms` identity row,
  playtime, saves, or metadata
  ([ADR-0007](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0007-rom-retention-identity-anchor.md)).
- **read-consumers** — `GameDetailService` resolves the ROM, install record, cached save state, cached metadata, and
  platform-name cache in one read Unit of Work (the has-saves badge reads `rom_save_states`, the platform display name
  the `kv_config` cache, both degrading gracefully when absent); `AchievementsService` reads each ROM's `ra_id` from
  `roms`; `SettingsService.apply_steam_input_setting` reads the bound `shortcut_app_id`s from `roms` (skipping unbound
  NULL rows).

With every consumer moved over, the teardown completed: the JSON-era state class `SaveSyncState`
(`domain/save_state.py`), the dead JSON stores (`RegistryStoreAdapter`, `MetadataCacheStoreAdapter`), the dead
persisters, and the in-memory state dicts (`shortcut_registry`, `metadata_cache`, and the catch-all `state` dict) are
all **deleted**.

## What an aggregate is here

An **aggregate** is a cluster of domain objects treated as a single unit for data consistency, with one root entity that
owns all invariants and is the only external entry point. The full definition — root, identity, transaction boundary,
by-id references, mutation-via-methods — lives in the
[`Aggregate` glossary entry in `CONTEXT.md`](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md). This
page uses that vocabulary; it does not re-derive the Cosmic Python theory.

Aggregate boundaries are **invariant boundaries, not storage boundaries** — one aggregate may be backed by several
tables, and table layout is a downstream decision. The first concrete aggregate-boundary decision, adopting `Platform`
as a full aggregate rather than a denormalized string, was recorded in
[ADR-0001](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0001-adopt-platform-aggregate.md) — now
**superseded by
[ADR-0003](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0003-json-sqlite-persistence-boundary.md)**,
which reverts `Platform` to a denormalized `platform_slug` string.

## Standards shared across all aggregates

Every aggregate in this codebase follows the same rules, so the enforcement layers below can be uniform:

- **Declared via the `@cosmic_aggregate` decorator.** This is the canonical form — not a transitional flag. The
  decorator marks the class as an aggregate root and is the marker the field-assignment check looks for.
- **Mutation only via verb-named methods on the root.** No external field assignment (`aggregate.field = value`) from
  services. Methods are named after the domain event that occurred (`adopt_baseline(...)`, `mark_installed(...)`,
  `confirm_slot(...)`) — per-field verbs even when slightly forced (`set_autocleanup_limit(10)`), consistency over
  expressiveness. Field access for reads is fine.
- **Cross-aggregate references by id only** — never by holding a Python reference to another aggregate's internals.
  `RomInstall` carries `rom_id: int`, not `rom: Rom`.
- **No `extra: dict[str, Any]` forward-compat hedge.** Schema migrations carry the model forward; aggregates do not hold
  an open-ended JSON dict against future change.

## CP enforcement layers

Four mechanisms keep the aggregate rules from drifting. They are layered — each catches a different class of violation,
and together they make "mutate an aggregate's fields from a service" fail before it merges.

| Layer | Mechanism                         | What it catches                                                        |
| ----- | --------------------------------- | ---------------------------------------------------------------------- |
| 1     | `@cosmic_aggregate` decorator     | Declares the root; gives it `__slots__` so unknown fields can't be set |
| 2     | AST field-assignment check        | `aggregate.field = value` in `services/`                               |
| 3     | import-linter domain contracts    | Non-stdlib / non-self imports into `domain/`                           |
| 4     | basedpyright `reportPrivateUsage` | Access to `_`-prefixed internals from production code                  |

### 1. The `@cosmic_aggregate` decorator

`py_modules/domain/_aggregate.py` defines the single canonical way to declare an aggregate root:

```python
from domain._aggregate import cosmic_aggregate

@cosmic_aggregate
class Rom:
    rom_id: int
    platform_slug: str
    # ...
```

The decorator applies `@dataclass(slots=True)`, so the root gets `__init__`, `__repr__`, `__eq__`, and `__slots__` for
free. `__slots__` matters for enforcement: a slotted dataclass rejects assignment to any attribute not declared as a
field, so typos and ad-hoc field additions fail at runtime, not silently. It is also the marker the AST check (layer 2)
scans for — `@cosmic_aggregate` is how a class opts into the mutation-via-methods rule.

**Value Objects do not use this decorator.** Immutable members of an aggregate (e.g. `FileSyncState`, `BiosFileEntry`)
use a plain `@dataclass(frozen=True, slots=True)` — they are immutable by construction and have no mutation surface to
police, so they need neither the marker nor the verb-method discipline. The decorator is for roots only.

### 2. AST field-assignment check

`scripts/check_aggregate_field_assignment.py` is a small custom linter, wired into CI alongside the cosmic call bans. It
enforces the **mutation-only-via-methods** rule that a type checker cannot express directly.

How it works:

1. It walks `py_modules/domain/`, parses every file, and collects the class names decorated with `@cosmic_aggregate`.
2. It walks `py_modules/services/` and flags every assignment whose target is `<receiver>.<field> = ...` where the
   receiver's variable name matches an aggregate class name (exact snake_case identifier match — variable `rom` matches
   aggregate `Rom`, `rom_state` does not). It skips `self.x = ...` (method-body internals) and subscript receivers
   (`d["k"].x = ...`).

The heuristic is conservative by design — a guardrail, not a prover. It can false-positive (a variable named `rom`
holding something else) and false-negative (assignment through a complex expression). The escape hatch is a trailing
comment on the offending line:

```python
rom.cover_path = path  # pragma: no aggregate-check
```

**The check is active.** The 8 aggregate roots all carry `@cosmic_aggregate`, so the aggregate-name set is populated and
any `aggregate.field = ...` assignment in a service fails CI. The escape hatch above is the only way past it.

### 3. import-linter — domain is stdlib + self only

Two `.importlinter` contracts confine `domain/` to the standard library and itself:

```ini
# Domain must not import services, adapters, lib, or models
[importlinter:contract:domain-independence]
type = forbidden
source_modules =
    domain
forbidden_modules =
    services
    adapters
    lib
    models

# Domain must not import vendored third-party packages
[importlinter:contract:domain-stdlib-only]
type = forbidden
source_modules =
    domain
forbidden_modules =
    _vendor
```

Together these say: **domain = stdlib + self only.** `domain-independence` forbids every sibling first-party layer (note
`lib` and `models` are now in the forbidden list — domain depends on no other internal layer); `domain-stdlib-only`
forbids the `_vendor` namespace, which is the codebase's only entry point for non-stdlib runtime code. This is the CP
doctrine that the domain model has no external runtime dependencies, mechanically enforced.

A consequence of `lib` being forbidden: anything domain needs from "shared utilities" lives inside `domain` itself.
ISO-8601 timestamp parsing (`parse_iso` / `parse_iso_to_epoch`) moved from `lib/iso_time.py` to `domain/iso_time.py` for
exactly this reason.

### 4. basedpyright `reportPrivateUsage = "error"`

`pyproject.toml` sets `reportPrivateUsage = "error"`, so accessing a `_`-prefixed name from outside its owning class is
a hard type error, not a convention nobody enforces. This makes the underscore-prefix convention real: production code
cannot reach into an aggregate's (or any class's) internals.

Tests are exempt via an execution-environment override:

```toml
[[tool.basedpyright.executionEnvironments]]
root = "tests"
extraPaths = ["py_modules"]
reportPrivateUsage = "none"
```

White-box testing — inspecting and rebinding the private state of the system under test — is a deliberate, accepted
pattern here. The guardrail targets production encapsulation, not test setup.

One corollary worth stating: a method that one sub-service calls on a peer is part of that peer's **public** surface and
carries **no** leading underscore. The `_` prefix is reserved for genuinely class-internal helpers, which keeps
`reportPrivateUsage` coherent with the saves-style peer-injection carve-out — peers call public methods, never private
ones.

## The aggregate set

Eight aggregate roots model the persisted domain. Each lives in its own `domain/<name>.py` module, is declared with
`@cosmic_aggregate`, and mutates only through verb-named methods. Per
[ADR-0003](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0003-json-sqlite-persistence-boundary.md)
the former `SyncSettings` knobs and `device_name` move to `settings.json`
([#822](https://github.com/danielcopper/decky-romm-sync/issues/822)) and `device_id` becomes a `kv_config` row
([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)) — they were config and a singleton scalar, not
relational state with invariants. The **Carries** column reflects the fields as actually implemented (`?` marks a
nullable/optional field); where the shipped shape is intentionally leaner than the original #788 plan, the **Why**
column says so. Cross-aggregate references are by id/slug only — the per-ROM aggregates (`RomMetadata`, `RomSaveState`,
`Playtime`) are keyed by `rom_id` externally rather than carrying it as a field; only `RomInstall` carries `rom_id` as a
field rather than keying on it externally, and it also denormalizes `platform_slug`/`system` so migration and save-sort
can read installs without a join.

| Aggregate root                                    | Carries                                                                                                                                                                                   | Why a separate aggregate                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Rom` (`domain/rom.py`)                           | `rom_id` (identity), `platform_slug`, `name`, `fs_name`, `shortcut_app_id?`, `last_synced_at`, `cover_path?`, `igdb_id?`, `sgdb_id?`, `ra_id?`, `emulator_override?`                      | Created/updated atomically when a ROM is synced from RomM; `shortcut_app_id` is NULL when the ROM is unbound (shortcut removed / gone from RomM) but the row is retained per [ADR-0007](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0007-rom-retention-identity-anchor.md). `emulator_override` is the nullable per-game core LABEL (NULL = follow the platform default), pinned/cleared via `Rom.pin_emulator_override` / `clear_emulator_override` and **excluded from the sync UPSERT** so re-sync never wipes a user's pin; anchoring it on `roms` lets the choice survive uninstall/reinstall ([ADR-0011](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0011-per-game-core-override-in-db-applied-via-e-flag.md)). The platform display name is **not** modeled as a local aggregate; `platform_slug` is a denormalized RomM slug and the display name is resolved live from RomM (per [ADR-0003](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0003-json-sqlite-persistence-boundary.md)), cached for offline reads in the `kv_config` `platform_names` row. |
| `RomInstall` (`domain/rom_install.py`)            | `rom_id`, `file_path`, `rom_dir?`, `platform_slug`, `system`, `installed_at`                                                                                                              | Exists only while a ROM is downloaded — created on download-complete, removed on uninstall. References `Rom` by `rom_id`. `file_path` is the launch target (always present); `rom_dir` is the dedicated per-ROM directory and is **NULL for single-file ROMs** (which live as a bare file in the shared `<roms>/<system>/` dir) — single-vs-multi is read from `rom_dir` presence, not re-derived from the path ([ADR-0008](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0008-rom-install-launch-file-and-rom-dir.md)). Denormalized `platform_slug` / `system` let migration + save-sort read installs without joining the registry. A per-file `RomFile[]` child (`category`: game/dlc/update/…) is the documented future model, deferred to #140/#129 and additive when it lands (ADR-0008).                                                                                                                                                                                                                                                                                                               |
| `RomMetadata` (`domain/rom_metadata.py`)          | `summary`, `genres`, `companies`, `first_release_date?`, `average_rating?`, `game_modes`, `player_count`, `cached_at`, `steam_categories`                                                 | 7-day staleness signal (`cached_at`), regenerated independently of library sync — staleness, not a schedule, prompts a refresh. Per-ROM, keyed by `rom_id`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `RomSaveState` (`domain/rom_save_state.py`)       | `active_slot?`, `slot_confirmed`, `emulator`, `system`, `last_synced_core?`, `own_upload_ids?`, `slots{}`, `files{}` (a `FileSyncState` value object per filename), `last_sync_check_at?` | Per-ROM saves aggregate. Matrix invariants hold inside: a file baseline always carries a non-empty `last_sync_hash`, while `tracked_save_id` is present only for server-anchored baselines (the `adopt_baseline` path) and NULL for hash-only baselines (the `update_baseline_hash` skip-adopt path); a non-legacy `active_slot` always has its `slots` key. Per-ROM, keyed by `rom_id`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `Playtime` (`domain/playtime.py`)                 | `total_seconds`, `session_count`, `last_session_start?`, `last_session_duration_sec?`, `note_id?`                                                                                         | Per-ROM, owned by PlaytimeService. Independent lifecycle from saves (`session_lifecycle.py` already treats them as separate concerns). Keyed by `rom_id`. Two write paths fold into the total via `reconcile_total` (never regresses): session-end (local duration folded, then pushed to the RomM note) and reconcile-on-view (the RomM note's total pulled in when a game's detail page opens — total-only, no note write).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `BiosFile` (`domain/bios_file.py`)                | `(platform_slug, file_name)` (composite identity), `file_path`, `downloaded_at`, `firmware_id?`                                                                                           | Per downloaded BIOS file. Composite key — a bare filename is unsafe (two platforms can ship same-named BIOS). `firmware_id` is nullable metadata, not identity.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `FirmwareCacheEntry` (`domain/firmware_cache.py`) | `id?`, `name`, `platform_slug`, `file_size_bytes`, `cached_at`                                                                                                                            | Per cached firmware item from RomM. TTL-cached server inventory; the cache is replaced wholesale on refresh and the TTL check lives in the service, so the aggregate stays a thin record.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `SyncRun` (`domain/sync_run.py`)                  | `id`, `started_at`, `status`, `platforms_planned`, `roms_planned`, `finished_at?`, `platforms_completed?`, `collections_completed?`, `error?`                                             | Models sync-as-operation — a `running` → `completed`/`cancelled`/`errored` state machine that terminates exactly once. Replaces scattered scalars (`last_sync`, `sync_stats`, `last_synced_platforms`, `last_synced_collections`). `sync_stats.roms` is not a field — it's a registry-derived count computed at read time.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |

`FileSyncState` (inside `RomSaveState`) is a **value object**, not an aggregate: a frozen
`@dataclass(frozen=True, slots=True)` built whole by `adopt_baseline(...)`, with no mutation surface of its own.

## The SQLite schema

The tables that back the aggregates, designed in [#780](https://github.com/danielcopper/decky-romm-sync/issues/780). The
authoritative DDL — every column type, default, constraint, and the full decision rationale inline — is
[`py_modules/db/migrations/001_initial.sql`](https://github.com/danielcopper/decky-romm-sync/blob/main/py_modules/db/migrations/001_initial.sql).
This section is the map, not a re-derivation.

### One table per aggregate

Each aggregate gets its own table — the per-ROM cluster is **not** a single wide `roms` mega-table. The epic floated a
mega-table as a starting proposal; #780 owns the final layout and split it. The deciding factor was integrity, not speed
(read performance is a non-issue at single-user scale): the per-ROM aggregates are **all-or-nothing groups** — an
install is either fully present or absent, metadata is cached or not — and separate tables let "state absent" mean "no
row" rather than a wide row of NULLs the schema cannot keep internally consistent. The rejected mega-table alternative
is recorded in
[ADR-0002](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0002-per-rom-table-per-aggregate-split.md).
One Repository per aggregate (the [CONTEXT.md](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md)
rule) maps 1:1 onto these tables.

| Table             | Backs                       | Key                          | Row present when               |
| ----------------- | --------------------------- | ---------------------------- | ------------------------------ |
| `roms`            | `Rom` (identity + shortcut) | `rom_id`                     | ROM is synced from RomM        |
| `rom_installs`    | `RomInstall`                | `rom_id`                     | ROM is downloaded              |
| `rom_metadata`    | `RomMetadata`               | `rom_id`                     | metadata has been cached       |
| `rom_playtime`    | `Playtime`                  | `rom_id`                     | ROM has been played            |
| `rom_save_states` | `RomSaveState` (scalars)    | `rom_id`                     | save tracking exists           |
| `rom_save_files`  | `FileSyncState` (1:N child) | `(rom_id, filename)`         | a file baseline is tracked     |
| `downloaded_bios` | `BiosFile`                  | `(platform_slug, file_name)` | a BIOS file is downloaded      |
| `firmware_cache`  | `FirmwareCacheEntry`        | `(platform_slug, name)`      | firmware inventory is cached   |
| `sync_runs`       | `SyncRun`                   | `id`                         | one row per sync run (history) |
| `kv_config`       | misc singleton scalars      | `key`                        | per key                        |

`SyncRun` carries its own invariants, so per CONTEXT.md it gets a typed table rather than untyped `kv_config` rows. The
full live `kv_config` key set is `device_id` (the server-issued device identity), `platform_names` (the JSON-encoded
`platform_slug → display_name` cache), `retrodeck_home_path` (+ its pending-migration `_previous`), and
`save_sort_settings` (+ `_previous`) — the truly miscellaneous singleton scalars. The `platform_names` cache is a single
JSON blob the library sync refreshes every run so offline reads (the DangerZone label, the game-detail platform name)
show "Nintendo 64" rather than the bare `n64` slug when RomM is unreachable. The schema version is **not** a `kv_config`
key — it is tracked in `PRAGMA user_version` by the [migration runner](#the-migration-framework)
([#781](https://github.com/danielcopper/decky-romm-sync/issues/781)).

`SyncRun` is a **history** table, not a single "last run" row: a 1-row table would let a newly-started run
(`status='running'`, no stats yet) erase the last completed run's displayable stats. "Last successful sync" is the
newest row with `status='completed'`; "is a sync running" is any row with `status='running'`.

### Foreign keys

Most relationships are _not_ parent-child (`startup_healing` prunes against disk truth; playtime survives shortcut
removal), so foreign keys are deliberately sparse:

- **Per-ROM tables → `roms`, `ON DELETE CASCADE`** (`rom_installs`, `rom_metadata`, `rom_playtime`, `rom_save_states`,
  `rom_save_files`). Per-ROM state is genuinely owned by the ROM, so a `DELETE FROM roms WHERE …` cascades it all away
  in one statement. Per
  [ADR-0007](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0007-rom-retention-identity-anchor.md)
  this cascade is **dormant**: a `roms` row is a permanent identity anchor keyed by RomM's stable `rom_id`. **Auto-stale
  = unbind** — when the automatic sync finds a ROM gone from RomM (or its shortcut is removed), the row is _unbound_
  (`Rom.unbind_shortcut()` NULLs `shortcut_app_id`, the row and its per-ROM children stay) so local
  playtime/saves/metadata survive. **Only a deliberate purge = delete** — an explicit, opt-in user action (which does
  not exist today) `DELETE`s the row and lets the cascade reap the children. The automatic sync never `DELETE`s a `roms`
  row.
  - **Caveat — writes to a cascade parent (`roms`) MUST UPSERT, never `INSERT OR REPLACE`/`REPLACE`.** In SQLite
    `REPLACE` resolves a PK conflict by _delete-then-insert_ of the parent row, and that DELETE fires the
    `ON DELETE CASCADE` above — silently wiping every per-ROM child on a re-save (a normal library re-sync, where the
    `roms` row already exists). `SqliteRomRepository.save()` therefore uses
    `INSERT … ON CONFLICT(rom_id) DO UPDATE SET …`, which updates the parent in place and never triggers the cascade
    ([#887](https://github.com/danielcopper/decky-romm-sync/issues/887)). Leaf tables with no cascade children may keep
    `INSERT OR REPLACE`.
- **`platform_slug` → no FK.** Carried on `roms` / `rom_installs` / `downloaded_bios` / `firmware_cache` as a plain
  denormalized RomM platform slug. There is no `platforms` table to reference —
  [ADR-0003](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0003-json-sqlite-persistence-boundary.md)
  dropped the `Platform` aggregate — so it is just a string. The platform _display name_ is not stored on the row: it
  resolves live from RomM during each sync and is cached for offline reads in a single `kv_config`
  `platform_slug → display_name` blob (refreshed every sync), degrading to the bare slug only when RomM is unreachable
  and the cache is empty.

The split moved the FK policy from the epic's "one FK only" (written for the mega-table world) to "CASCADE for the
per-ROM ownership relationships the split introduced; no FK for cross-aggregate references" — same intent, applied to
the new tables. See
[ADR-0002](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0002-per-rom-table-per-aggregate-split.md).

### Type conventions

All tables are `STRICT` (SQLite ≥ 3.37; the Deck ships 3.50). STRICT allows only `INTEGER` / `REAL` / `TEXT` / `BLOB` /
`ANY`, so:

- **Booleans** are `INTEGER` 0/1, guarded by `CHECK (col IN (0, 1))`.
- **Event timestamps** are `TEXT` ISO-8601 (sortable, human-readable); **cache/TTL timestamps** are `REAL` Unix-epoch
  seconds (cheap age math). The split is aggregate-driven — only the caches do age arithmetic.
- **JSON** arrays/objects are `TEXT` guarded by `CHECK (json_valid(col))`. They are display/read-model data, never
  queried by element, so normalization buys nothing.
- `rom_save_states.own_upload_ids` is nullable `TEXT` where **`NULL` ≠ `'[]'`**: `NULL` means attribution
  unknown/legacy, `'[]'` means we uploaded nothing — both meaningful.

No blanket `created_at`/`updated_at` audit columns (the aggregates already model the timestamps that matter), no
`systems` lookup table (`system` stays `TEXT`), and no secondary indexes yet (every lookup and cascade rides a primary
key; further indexing is deferred until profiling justifies it, per the epic).

## The migration framework

The schema above is not loaded as a special case — it is migration `001`, applied by the same runner that applies every
future schema change. The runner lives in
[`py_modules/adapters/sqlite_migrations.py`](https://github.com/danielcopper/decky-romm-sync/blob/main/py_modules/adapters/sqlite_migrations.py)
([#781](https://github.com/danielcopper/decky-romm-sync/issues/781)) — it does file + database I/O, so it is an adapter
— and is invoked from `bootstrap()` at plugin startup, before any service is wired. stdlib `sqlite3` only; no Alembic or
other third-party migration tooling.

**Versioning — `PRAGMA user_version`.** SQLite keeps a single integer in the database header, readable and writable via
`PRAGMA user_version`. The runner uses it as the applied-schema marker: a fresh database reports `0`; after migration
`NNN` is applied the runner stamps `user_version = NNN`. There is no separate `schema_migrations` table — `user_version`
is the whole mechanism (the same lean approach SDH-PlayTime and Junk-Store use). This is why the schema version is
deliberately **not** a `kv_config` key.

**Discovery — `NNN_descriptive_name.sql`.** Migrations are plain `.sql` files under `py_modules/db/migrations/`, named
with a leading integer (`001_initial.sql`). At startup the runner scans that directory, parses the integer prefix off
each filename, sorts ascending **numerically** (so `10` follows `2`, not lexically), and applies only the files whose
number is greater than the database's current `user_version`. Files that don't match `NNN_*.sql` are ignored.

**Atomic per migration.** Each migration runs inside its own transaction: `BEGIN` → the migration's DDL →
`PRAGMA user_version = NNN` → `COMMIT`. The version bump rides the same transaction as the DDL, so a migration is
all-or-nothing: if any statement fails, the transaction rolls back (DDL **and** version bump both undone) and the runner
re-raises, leaving the database at the last successfully-applied version. Migration files therefore contain
transaction-safe DDL only and must **not** carry their own `BEGIN`/`COMMIT` — the runner supplies the transaction.

**Connection PRAGMAs.** The runner sets `journal_mode=WAL` (persistent — recorded in the database file, so it carries
over to runtime connections) and `foreign_keys=ON` (so `CASCADE`-bearing DDL behaves here as it will at runtime). The
full per-connection PRAGMA set for runtime Unit-of-Work connections is applied by the UoW itself (see
[The runtime Unit of Work](#the-runtime-unit-of-work) below): `foreign_keys=ON`, `synchronous=NORMAL`,
`busy_timeout=5000`, `temp_store=MEMORY`, with `isolation_level=None` so the UoW drives `BEGIN`/`COMMIT`/`ROLLBACK`
explicitly.

**Database location.** The database is `romm_sync.db` in the plugin runtime directory
(`decky.DECKY_PLUGIN_RUNTIME_DIR`). The live path reads and writes it; DB-init is hard-failing (a migration failure
aborts startup rather than degrading silently) so a corrupt or unmigratable database never serves stale reads.

### Adding a migration past v1

Drop a new file `NNN_descriptive_name.sql` into `py_modules/db/migrations/` containing the schema change (e.g.
`ALTER TABLE roms ADD COLUMN …;` or a fresh `CREATE TABLE …;`) as transaction-safe DDL with no `BEGIN`/`COMMIT`. That's
the whole change — on the next startup the runner sees `NNN > user_version`, applies it inside its own transaction, and
bumps `user_version` to `NNN`. Existing databases receive only the new migrations; fresh databases receive all of them
in order. No code change is needed to register the file.

The first migration past `001` is
[`002_add_emulator_override.sql`](https://github.com/danielcopper/decky-romm-sync/blob/main/py_modules/db/migrations/002_add_emulator_override.sql)
— a single `ALTER TABLE roms ADD COLUMN emulator_override TEXT;` for the per-game core override
([ADR-0011](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0011-per-game-core-override-in-db-applied-via-e-flag.md)),
which stamps `user_version = 2`.

## The runtime Unit of Work

The schema is read and written at runtime through a **Unit of Work** (UoW) — the atomic transaction boundary one
operation works inside. The concrete UoW and the nine `sqlite3` repository adapters that back it live in
[`py_modules/adapters/repositories/`](https://github.com/danielcopper/decky-romm-sync/tree/main/py_modules/adapters/repositories)
([#783](https://github.com/danielcopper/decky-romm-sync/issues/783)). The `UnitOfWork` / `UnitOfWorkFactory` Protocols
services depend on live in `py_modules/services/protocols/uow.py`; the per-aggregate Repository Protocols in
`py_modules/services/protocols/repositories.py` ([#782](https://github.com/danielcopper/decky-romm-sync/issues/782)).

**Synchronous `sqlite3`, not `aiosqlite`.** Per
[ADR-0004](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0004-sync-sqlite-unit-of-work.md) the
runtime UoW uses stdlib `sqlite3` (synchronous), reversing the epic's earlier `aiosqlite` plan: `aiosqlite` is itself
thread-based with no concurrency win for this single-writer workload, it would add a vendored dependency, and it would
introduce a second I/O paradigm alongside the established `run_in_executor` idiom and the sync
[#781](https://github.com/danielcopper/decky-romm-sync/issues/781) migration runner. No new vendored dependency.

**Shape.** `SqliteUnitOfWork(db_path)` is a synchronous context manager. `__enter__` opens one connection
(`isolation_level=None`, `row_factory = sqlite3.Row`), applies the per-connection PRAGMAs (`foreign_keys=ON`,
`synchronous=NORMAL`, `busy_timeout=5000`, `temp_store=MEMORY` — `journal_mode=WAL` is already persistent from the
runner), issues an explicit `BEGIN IMMEDIATE`, builds the nine repositories over that shared connection, and returns
itself. `__exit__` commits on a clean exit, rolls back on an exception (then re-raises), and always closes the
connection. One UoW therefore equals one transaction; writes across several repositories commit or roll back together.

**Transaction policy — `BEGIN IMMEDIATE` for every UoW.** The UoW starts its transaction with `BEGIN IMMEDIATE`, not a
deferred `BEGIN`, so the write lock is acquired at transaction start. The reason: a read-then-write UoW (e.g. the
reporter's per-rom upsert does a `roms.get` then an `INSERT` in a loop) opened with a deferred `BEGIN` takes a read
snapshot first, then tries to _upgrade_ read → write on the first `INSERT`. Under WAL, if another connection commits a
write in between, that upgrade fails **immediately** with `SQLITE_BUSY_SNAPSHOT` — and `busy_timeout` does **not** retry
a snapshot-upgrade failure, so the operation errors spuriously. `BEGIN IMMEDIATE` holds the write lock from the start,
so there is no read → write upgrade and no snapshot to invalidate; concurrent writers serialize on `busy_timeout=5000`
instead of failing. The decision is **universal** — _all_ UoWs use `BEGIN IMMEDIATE`, with no read-only/write
distinction: across ~116 call sites in a single-user, short-DB-op workload, the universal rule is the safe one-liner
with no mislabeling footgun. The accepted tradeoff is that read-only UoWs also take the write lock (and so serialize
against each other) — negligible for this workload.

**Thread affinity.** A `sqlite3` connection is single-thread by default (`check_same_thread=True`, left at its safe
default). Services run the whole `with uow_factory() as uow:` block inside their synchronous `run_in_executor` worker
(the house `do_<verb>` / `_<verb>_io` idiom), so the connection is created, used, and closed entirely on one worker
thread and never escapes it.

**Repositories.** Each `SqliteXxxRepository` holds the UoW's open connection and maps rows ↔ domain aggregates: STRICT
booleans round-trip through `int(bool)` / `bool(int)`, JSON-array/object columns through `json.dumps` / `json.loads`.
`RomSaveStateRepository` spans two tables (`rom_save_states` + `rom_save_files`) — `save` writes the scalar row then
replaces the child file rows inside the same transaction. The adapters import only `sqlite3`, `json`, and `domain.*`
(never `services`), and structurally satisfy the Repository Protocols; the UoW structurally satisfies `UnitOfWork`,
keeping the `adapters ↛ services` boundary intact. The factory (`functools.partial(SqliteUnitOfWork, db_path)`) is wired
in `bootstrap()` and threaded into every migrated service's `*ServiceConfig` as `uow_factory`; per
[ADR-0006](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0006-narrow-unit-of-work-scope.md) a
service opens the UoW only around its DB reads/writes, never across the server/file I/O or the frontend ack.

## Cutover status

The service cutover ([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)) landed vertical by vertical and
is complete. Every slice migrated:

- **firmware** and **downloads** (the launcher's old SQLite read path was since removed — the launcher no longer reads
  SQLite at all; the launch command is baked into the shortcut's `launch_options` and the `rom-launcher` exec wrapper
  just runs it, per
  [ADR-0009](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0009-launcher-pure-exec-wrapper-baked-launch-options.md)).
- the **saves** vertical.
- the **library/roms** slice (registry → `roms`, sync lifecycle → `sync_runs`, the platform-name cache → `kv_config`).
- the **metadata** slice (`rom_metadata` written by the reporter's per-unit commit;
  `MetadataService`/`GameDetailService` read it from SQLite).
- the **playtime** slice (`PlaytimeService` records sessions and reconciles RomM-note totals through `rom_playtime` —
  session-end push + pull-only reconcile-on-view).
- the **rom-removal + startup-healing** slice (`RomRemovalService` and
  `StartupHealingService.prune_stale_installed_roms` read/delete `rom_installs` through the UoW).
- the **read-consumers** slice (`GameDetailService`/`AchievementsService`/`SettingsService` read the synced-shortcut
  registry, install record, save state, and `ra_id` from SQLite).
- the **migration** slice — `MigrationService` reads the RetroDECK-home and save-sort change-detection markers from
  `kv_config` (Bucket 2 per
  [ADR-0003](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0003-json-sqlite-persistence-boundary.md))
  and relocates installed-ROM file paths through `uow.rom_installs.relocate`; `SyncReporter.get_rom_by_steam_app_id`
  tests installed-ness via `uow.rom_installs.get`; `StartupHealingService.prune_stale_installed_roms` reads the
  pending-migration home from `kv_config`; and `RomInfoService` reads the save-sort markers from `kv_config`.

With every consumer moved over, the teardown completed: the dead persisters, the `RegistryStoreAdapter` /
`MetadataCacheStoreAdapter` JSON stores, `domain/save_state.py` (`SaveSyncState`), and the in-memory state dicts
(`shortcut_registry`, `metadata_cache`, `installed_roms`, and the catch-all `state` dict) are all deleted.

Chapter 8+ of the Cosmic Python book (domain events + message bus) is explicitly out of scope for this epic; the
triggers for revisiting that scope are recorded in `CLAUDE.md`.

## See also

- [Backend Architecture](backend-architecture.md) — the four-layer split, the `XxxServiceConfig` pattern, and the
  boundary-enforcement layers that aggregates build on.
- [`CONTEXT.md`](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md) — the `Aggregate`, `kv_config`,
  and `Rom`/`ROM`/`RomM` glossary entries.
- [ADR-0001](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0001-adopt-platform-aggregate.md) — the
  decision to adopt `Platform` as a full aggregate (**superseded by ADR-0003**).
- [ADR-0002](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0002-per-rom-table-per-aggregate-split.md)
  — the per-ROM table-per-aggregate split and the per-ROM CASCADE foreign keys.
- [ADR-0003](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0003-json-sqlite-persistence-boundary.md)
  — the JSON/SQLite persistence boundary; drops the `Platform`, `Device`, and `SyncSettings` aggregates and reverts
  `Platform` to a denormalized `platform_slug` string.
