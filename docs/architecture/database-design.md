# Database Design

## Overview

This page is the canonical home for the **aggregate domain model** behind the SQLite persistence migration (epic [#271](https://github.com/danielcopper/decky-romm-sync/issues/271)). The migration replaces the current JSON state files with a SQLite database whose tables back a set of Cosmic Python aggregates.

The migration is phased. As of [#788](https://github.com/danielcopper/decky-romm-sync/issues/788), the **enforcement infrastructure** (the decorator, the linters, and the type-check rule that keep aggregates honest) and the full **aggregate set** (the 11 aggregate roots, their fields, and their mutation methods) are both in place — documented below. The SQLite schema and the per-aggregate Repository Protocols land in the downstream sub-issues; see [Coming in later PRs](#coming-in-later-prs).

The aggregate roots are defined but **not yet wired** into any service: they coexist in the source tree alongside the live JSON-era state classes (`domain/save_state.py`) they will replace. Nothing reads or writes them at runtime until the cutover wave ([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)), which is a **hard cut** — SQLite starts empty, the JSON state is not migrated into it, and the old classes are deleted then.

## What an aggregate is here

An **aggregate** is a cluster of domain objects treated as a single unit for data consistency, with one root entity that owns all invariants and is the only external entry point. The full definition — root, identity, transaction boundary, by-id references, mutation-via-methods — lives in the [`Aggregate` glossary entry in `CONTEXT.md`](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md). This page uses that vocabulary; it does not re-derive the Cosmic Python theory.

Aggregate boundaries are **invariant boundaries, not storage boundaries** — one aggregate may be backed by several tables, and table layout is a downstream decision. The first concrete aggregate-boundary decision, adopting `Platform` as a full aggregate rather than a denormalized string, is recorded in [ADR-0001](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0001-adopt-platform-aggregate.md).

## Standards shared across all aggregates

Every aggregate in this codebase follows the same rules, so the enforcement layers below can be uniform:

- **Declared via the `@cosmic_aggregate` decorator.** This is the canonical form — not a transitional flag. The decorator marks the class as an aggregate root and is the marker the field-assignment check looks for.
- **Mutation only via verb-named methods on the root.** No external field assignment (`aggregate.field = value`) from services. Methods are named after the domain event that occurred (`adopt_baseline(...)`, `mark_installed(...)`, `confirm_slot(...)`) — per-field verbs even when slightly forced (`set_autocleanup_limit(10)`), consistency over expressiveness. Field access for reads is fine.
- **Cross-aggregate references by id only** — never by holding a Python reference to another aggregate's internals. `RomInstall` carries `rom_id: int`, not `rom: Rom`.
- **No `extra: dict[str, Any]` forward-compat hedge.** Schema migrations carry the model forward; aggregates do not hold an open-ended JSON dict against future change.

## CP enforcement layers

Four mechanisms keep the aggregate rules from drifting. They are layered — each catches a different class of violation, and together they make "mutate an aggregate's fields from a service" fail before it merges.

| Layer | Mechanism | What it catches |
| --- | --- | --- |
| 1 | `@cosmic_aggregate` decorator | Declares the root; gives it `__slots__` so unknown fields can't be set |
| 2 | AST field-assignment check | `aggregate.field = value` in `services/` |
| 3 | import-linter domain contracts | Non-stdlib / non-self imports into `domain/` |
| 4 | basedpyright `reportPrivateUsage` | Access to `_`-prefixed internals from production code |

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

The decorator applies `@dataclass(slots=True)`, so the root gets `__init__`, `__repr__`, `__eq__`, and `__slots__` for free. `__slots__` matters for enforcement: a slotted dataclass rejects assignment to any attribute not declared as a field, so typos and ad-hoc field additions fail at runtime, not silently. It is also the marker the AST check (layer 2) scans for — `@cosmic_aggregate` is how a class opts into the mutation-via-methods rule.

**Value Objects do not use this decorator.** Immutable members of an aggregate (e.g. `FileSyncState`, `BiosFileEntry`) use a plain `@dataclass(frozen=True, slots=True)` — they are immutable by construction and have no mutation surface to police, so they need neither the marker nor the verb-method discipline. The decorator is for roots only.

### 2. AST field-assignment check

`scripts/check_aggregate_field_assignment.py` is a small custom linter, wired into CI alongside the cosmic call bans. It enforces the **mutation-only-via-methods** rule that a type checker cannot express directly.

How it works:

1. It walks `py_modules/domain/`, parses every file, and collects the class names decorated with `@cosmic_aggregate`.
2. It walks `py_modules/services/` and flags every assignment whose target is `<receiver>.<field> = ...` where the receiver's variable name matches an aggregate class name (exact snake_case identifier match — variable `rom` matches aggregate `Rom`, `rom_state` does not). It skips `self.x = ...` (method-body internals) and subscript receivers (`d["k"].x = ...`).

The heuristic is conservative by design — a guardrail, not a prover. It can false-positive (a variable named `rom` holding something else) and false-negative (assignment through a complex expression). The escape hatch is a trailing comment on the offending line:

```python
rom.cover_path = path  # pragma: no aggregate-check
```

**It is a no-op until aggregates exist.** No class carries `@cosmic_aggregate` yet, so the aggregate-name set is empty and the check finds nothing. It activates automatically as the aggregate roots land in later PRs — the moment a `@cosmic_aggregate` class appears, any `aggregate.field = ...` in a service starts failing CI. Old JSON-era containers (e.g. `SaveSyncState`) don't carry the decorator, so they keep working until the cutover wave replaces them.

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

Together these say: **domain = stdlib + self only.** `domain-independence` forbids every sibling first-party layer (note `lib` and `models` are now in the forbidden list — domain depends on no other internal layer); `domain-stdlib-only` forbids the `_vendor` namespace, which is the codebase's only entry point for non-stdlib runtime code. This is the CP doctrine that the domain model has no external runtime dependencies, mechanically enforced.

A consequence of `lib` being forbidden: anything domain needs from "shared utilities" lives inside `domain` itself. ISO-8601 timestamp parsing (`parse_iso` / `parse_iso_to_epoch`) moved from `lib/iso_time.py` to `domain/iso_time.py` for exactly this reason.

### 4. basedpyright `reportPrivateUsage = "error"`

`pyproject.toml` sets `reportPrivateUsage = "error"`, so accessing a `_`-prefixed name from outside its owning class is a hard type error, not a convention nobody enforces. This makes the underscore-prefix convention real: production code cannot reach into an aggregate's (or any class's) internals.

Tests are exempt via an execution-environment override:

```toml
[[tool.basedpyright.executionEnvironments]]
root = "tests"
extraPaths = ["py_modules"]
reportPrivateUsage = "none"
```

White-box testing — inspecting and rebinding the private state of the system under test — is a deliberate, accepted pattern here. The guardrail targets production encapsulation, not test setup.

One corollary worth stating: a method that one sub-service calls on a peer is part of that peer's **public** surface and carries **no** leading underscore. The `_` prefix is reserved for genuinely class-internal helpers, which keeps `reportPrivateUsage` coherent with the saves-style peer-injection carve-out — peers call public methods, never private ones.

## The aggregate set

Eleven aggregate roots model the persisted domain. Each lives in its own `domain/<name>.py` module, is declared with `@cosmic_aggregate`, and mutates only through verb-named methods. The **Carries** column reflects the fields as actually implemented (`?` marks a nullable/optional field); where the shipped shape is intentionally leaner than the original #788 plan, the **Why** column says so. Cross-aggregate references are by id/slug only — the per-ROM aggregates (`RomMetadata`, `RomSaveState`, `Playtime`) are keyed by `rom_id` externally rather than carrying it as a field; only `RomInstall` carries `rom_id` as a field rather than keying on it externally, and it also denormalizes `platform_slug`/`system` so migration and save-sort can read installs without a join.

| Aggregate root | Carries | Why a separate aggregate |
| --- | --- | --- |
| `Rom` (`domain/rom.py`) | `rom_id` (identity), `platform_slug`, `name`, `fs_name`, `shortcut_app_id`, `last_synced_at`, `cover_path?`, `igdb_id?`, `sgdb_id?`, `ra_id?` | Created/updated atomically when a ROM is synced from RomM. `platform_name` is **not** carried — resolved via the `platform_slug` FK into `Platform`. |
| `Platform` (`domain/platform.py`) | `slug` (identity), `display_name`, `excluded_from_sync` | Per-platform state the plugin owns locally; cached `display_name` survives RomM downtime. Shipped lean per [ADR-0001](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0001-adopt-platform-aggregate.md) — `emulation_stack` / `manual_emulator_path` are deferred future fields, not present yet. RetroDECK-managed per-platform state stays outside. |
| `RomInstall` (`domain/rom_install.py`) | `rom_id`, `file_path`, `install_path`, `platform_slug`, `system`, `installed_at` | Exists only while a ROM is downloaded — created on download-complete, removed on uninstall. References `Rom` by `rom_id`. Denormalized `platform_slug` / `system` let migration + save-sort read installs without joining the registry. |
| `RomMetadata` (`domain/rom_metadata.py`) | `summary`, `genres`, `companies`, `first_release_date?`, `average_rating?`, `game_modes`, `player_count`, `cached_at`, `steam_categories` | 7-day staleness signal (`cached_at`), regenerated independently of library sync — staleness, not a schedule, prompts a refresh. Per-ROM, keyed by `rom_id`. |
| `RomSaveState` (`domain/rom_save_state.py`) | `active_slot?`, `slot_confirmed`, `emulator`, `system`, `last_synced_core?`, `own_upload_ids?`, `slots{}`, `files{}` (a `FileSyncState` value object per filename), `last_sync_check_at?` | Per-ROM saves aggregate. Matrix invariants hold inside: a file baseline always carries its `tracked_save_id`, and a non-legacy `active_slot` always has its `slots` key. Per-ROM, keyed by `rom_id`. |
| `Playtime` (`domain/playtime.py`) | `total_seconds`, `session_count`, `last_session_start?`, `last_session_duration_sec?`, `note_id?` | Per-ROM, owned by PlaytimeService. Independent lifecycle from saves (`session_lifecycle.py` already treats them as separate concerns). Keyed by `rom_id`. |
| `Device` (`domain/device.py`) | `device_id` (identity, server-issued string), `device_name?` | Singleton. `device_id` and the old `server_device_id` collapse to a single field — they were always the same server row id JSON-side. |
| `SyncSettings` (`domain/sync_settings.py`) | `save_sync_enabled`, `sync_before_launch`, `sync_after_exit`, `default_slot?`, `autocleanup_limit` | Singleton. Save-sync feature settings, distinct from `settings.json` (which stays JSON per the epic). |
| `BiosFile` (`domain/bios_file.py`) | `(platform_slug, file_name)` (composite identity), `file_path`, `downloaded_at`, `firmware_id?` | Per downloaded BIOS file. Composite key — a bare filename is unsafe (two platforms can ship same-named BIOS). `firmware_id` is nullable metadata, not identity. |
| `FirmwareCacheEntry` (`domain/firmware_cache.py`) | `id?`, `name`, `platform_slug`, `file_size_bytes`, `cached_at` | Per cached firmware item from RomM. TTL-cached server inventory; the cache is replaced wholesale on refresh and the TTL check lives in the service, so the aggregate stays a thin record. |
| `SyncRun` (`domain/sync_run.py`) | `id`, `started_at`, `status`, `platforms_planned`, `roms_planned`, `finished_at?`, `platforms_completed?`, `collections_completed?`, `error?` | Models sync-as-operation — a `running` → `completed`/`cancelled`/`errored` state machine that terminates exactly once. Replaces scattered scalars (`last_sync`, `sync_stats`, `last_synced_platforms`, `last_synced_collections`). `sync_stats.roms` is not a field — it's a registry-derived count computed at read time. |

`FileSyncState` (inside `RomSaveState`) is a **value object**, not an aggregate: a frozen `@dataclass(frozen=True, slots=True)` built whole by `adopt_baseline(...)`, with no mutation surface of its own.

## The SQLite schema

The tables that back the aggregates, designed in [#780](https://github.com/danielcopper/decky-romm-sync/issues/780). The authoritative DDL — every column type, default, constraint, and the full decision rationale inline — is [`py_modules/db/migrations/001_initial.sql`](https://github.com/danielcopper/decky-romm-sync/blob/main/py_modules/db/migrations/001_initial.sql). This section is the map, not a re-derivation.

### One table per aggregate

Each aggregate gets its own table — the per-ROM cluster is **not** a single wide `roms` mega-table. The epic floated a mega-table as a starting proposal; #780 owns the final layout and split it. The deciding factor was integrity, not speed (read performance is a non-issue at single-user scale): the per-ROM aggregates are **all-or-nothing groups** — an install is either fully present or absent, metadata is cached or not — and separate tables let "state absent" mean "no row" rather than a wide row of NULLs the schema cannot keep internally consistent. The rejected mega-table alternative is recorded in [ADR-0002](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0002-per-rom-table-per-aggregate-split.md). One Repository per aggregate (the [CONTEXT.md](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md) rule) maps 1:1 onto these tables.

| Table | Backs | Key | Row present when |
| --- | --- | --- | --- |
| `roms` | `Rom` (identity + shortcut) | `rom_id` | ROM is synced from RomM |
| `rom_installs` | `RomInstall` | `rom_id` | ROM is downloaded |
| `rom_metadata` | `RomMetadata` | `rom_id` | metadata has been cached |
| `rom_playtime` | `Playtime` | `rom_id` | ROM has been played |
| `rom_save_states` | `RomSaveState` (scalars) | `rom_id` | save tracking exists |
| `rom_save_files` | `FileSyncState` (1:N child) | `(rom_id, filename)` | a file baseline is tracked |
| `platforms` | `Platform` | `slug` | platform seen / configured |
| `downloaded_bios` | `BiosFile` | `(platform_slug, file_name)` | a BIOS file is downloaded |
| `firmware_cache` | `FirmwareCacheEntry` | `(platform_slug, name)` | firmware inventory is cached |
| `sync_runs` | `SyncRun` | `id` | one row per sync run (history) |
| `device` | `Device` | `id = 1` (singleton) | after registration |
| `sync_settings` | `SyncSettings` | `id = 1` (singleton) | always |
| `kv_config` | misc singleton scalars | `key` | per key |

`Device`, `SyncSettings`, and `SyncRun` carry their own invariants, so per CONTEXT.md they get typed tables rather than untyped `kv_config` rows. `kv_config` is reserved for the truly miscellaneous: `retrodeck_home_path` (+ its pending-migration `_previous`) and `save_sort_settings` (+ `_previous`). The schema version is **not** a `kv_config` key — it is tracked in `PRAGMA user_version` by the [migration runner](#the-migration-framework) ([#781](https://github.com/danielcopper/decky-romm-sync/issues/781)).

`SyncRun` is a **history** table, not a single "last run" row: a 1-row table would let a newly-started run (`status='running'`, no stats yet) erase the last completed run's displayable stats. "Last successful sync" is the newest row with `status='completed'`; "is a sync running" is any row with `status='running'`.

### Foreign keys

Most relationships are *not* parent-child (`startup_healing` prunes against disk truth; playtime survives shortcut removal), so foreign keys are deliberately sparse:

- **Per-ROM tables → `roms`, `ON DELETE CASCADE`** (`rom_installs`, `rom_metadata`, `rom_playtime`, `rom_save_states`, `rom_save_files`). Per-ROM state is genuinely owned by the ROM, so a deliberate library prune (`DELETE FROM roms WHERE …`) cascades it all away in one statement.
- **`platform_slug` → no FK.** Carried on `roms` / `rom_installs` / `downloaded_bios` / `firmware_cache` as a logical/join reference only. An enforced FK would force sync ordering (platforms before ROMs) and block platform pruning while ROMs exist — fighting the disk-truth-pruning model. ADR-0001's "FK" wording means this logical reference, not a DB constraint.

The split moved the FK policy from the epic's "one FK only" (written for the mega-table world) to "CASCADE for the per-ROM ownership relationships the split introduced; no FK for cross-aggregate references" — same intent, applied to the new tables. See [ADR-0002](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0002-per-rom-table-per-aggregate-split.md).

### Type conventions

All tables are `STRICT` (SQLite ≥ 3.37; the Deck ships 3.50). STRICT allows only `INTEGER` / `REAL` / `TEXT` / `BLOB` / `ANY`, so:

- **Booleans** are `INTEGER` 0/1, guarded by `CHECK (col IN (0, 1))`.
- **Event timestamps** are `TEXT` ISO-8601 (sortable, human-readable); **cache/TTL timestamps** are `REAL` Unix-epoch seconds (cheap age math). The split is aggregate-driven — only the caches do age arithmetic.
- **JSON** arrays/objects are `TEXT` guarded by `CHECK (json_valid(col))`. They are display/read-model data, never queried by element, so normalization buys nothing.
- `rom_save_states.own_upload_ids` is nullable `TEXT` where **`NULL` ≠ `'[]'`**: `NULL` means attribution unknown/legacy, `'[]'` means we uploaded nothing — both meaningful.

No blanket `created_at`/`updated_at` audit columns (the aggregates already model the timestamps that matter), no `systems` lookup table (`system` stays `TEXT`), and no secondary indexes yet (every lookup and cascade rides a primary key; further indexing is deferred until profiling justifies it, per the epic).

## The migration framework

The schema above is not loaded as a special case — it is migration `001`, applied by the same runner that applies every future schema change. The runner lives in [`py_modules/adapters/sqlite_migrations.py`](https://github.com/danielcopper/decky-romm-sync/blob/main/py_modules/adapters/sqlite_migrations.py) ([#781](https://github.com/danielcopper/decky-romm-sync/issues/781)) — it does file + database I/O, so it is an adapter — and is invoked from `bootstrap()` at plugin startup, before any service is wired. stdlib `sqlite3` only; no Alembic or other third-party migration tooling.

**Versioning — `PRAGMA user_version`.** SQLite keeps a single integer in the database header, readable and writable via `PRAGMA user_version`. The runner uses it as the applied-schema marker: a fresh database reports `0`; after migration `NNN` is applied the runner stamps `user_version = NNN`. There is no separate `schema_migrations` table — `user_version` is the whole mechanism (the same lean approach SDH-PlayTime and Junk-Store use). This is why the schema version is deliberately **not** a `kv_config` key.

**Discovery — `NNN_descriptive_name.sql`.** Migrations are plain `.sql` files under `py_modules/db/migrations/`, named with a leading integer (`001_initial.sql`). At startup the runner scans that directory, parses the integer prefix off each filename, sorts ascending **numerically** (so `10` follows `2`, not lexically), and applies only the files whose number is greater than the database's current `user_version`. Files that don't match `NNN_*.sql` are ignored.

**Atomic per migration.** Each migration runs inside its own transaction: `BEGIN` → the migration's DDL → `PRAGMA user_version = NNN` → `COMMIT`. The version bump rides the same transaction as the DDL, so a migration is all-or-nothing: if any statement fails, the transaction rolls back (DDL **and** version bump both undone) and the runner re-raises, leaving the database at the last successfully-applied version. Migration files therefore contain transaction-safe DDL only and must **not** carry their own `BEGIN`/`COMMIT` — the runner supplies the transaction.

**Connection PRAGMAs.** The runner sets `journal_mode=WAL` (persistent — recorded in the database file, so it carries over to runtime connections) and `foreign_keys=ON` (so `CASCADE`-bearing DDL behaves here as it will at runtime). The full per-connection PRAGMA set for runtime Unit-of-Work connections is a separate concern ([#783](https://github.com/danielcopper/decky-romm-sync/issues/783)).

**Database location.** The database is `romm_sync.db` in the plugin runtime directory (`decky.DECKY_PLUGIN_RUNTIME_DIR`), alongside today's JSON state files. Pre-cutover ([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)) nothing reads it, so creating the schema at startup is a harmless but visible behavior change; a migration failure is logged and startup continues — whether a failure should ever become fatal is a cutover-era decision, deferred to #784.

### How to add a v2 migration

Drop a new file `002_descriptive_name.sql` into `py_modules/db/migrations/` containing the schema change (e.g. `ALTER TABLE roms ADD COLUMN …;` or a fresh `CREATE TABLE …;`) as transaction-safe DDL with no `BEGIN`/`COMMIT`. That's the whole change — on the next startup the runner sees `002 > user_version`, applies it inside its own transaction, and bumps `user_version` to `2`. Existing databases receive only the new migration; fresh databases receive `001` then `002` in order. No code change is needed to register the file.

## Coming in later PRs

With the aggregate set and the schema (applied by the migration framework above) now in place, the remaining persistence work lands downstream:

- **Per-aggregate Repository Protocols** — one Repository per aggregate root (not per table), defined downstream in [#782](https://github.com/danielcopper/decky-romm-sync/issues/782).
- **The runtime Unit-of-Work + connection PRAGMAs** — how services open and share a database connection per operation, in [#783](https://github.com/danielcopper/decky-romm-sync/issues/783).
- **The service cutover** — wiring the aggregates + Repositories into the services and the hard cut off the JSON state, in [#784](https://github.com/danielcopper/decky-romm-sync/issues/784).

Chapter 8+ of the Cosmic Python book (domain events + message bus) is explicitly out of scope for this epic; the triggers for revisiting that scope are recorded in `CLAUDE.md`.

## See also

- [Backend Architecture](backend-architecture.md) — the four-layer split, the `XxxServiceConfig` pattern, and the boundary-enforcement layers that aggregates build on.
- [`CONTEXT.md`](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md) — the `Aggregate`, `kv_config`, and `Rom`/`ROM`/`RomM` glossary entries.
- [ADR-0001](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0001-adopt-platform-aggregate.md) — the decision to adopt `Platform` as a full aggregate.
- [ADR-0002](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0002-per-rom-table-per-aggregate-split.md) — the per-ROM table-per-aggregate split and the per-ROM CASCADE foreign keys.
