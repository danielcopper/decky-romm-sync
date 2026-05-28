# JSON/SQLite persistence boundary

## Status

Accepted. **Supersedes [ADR-0001](0001-adopt-platform-aggregate.md)** (Platform-as-aggregate).

## Context

Epic [#271](https://github.com/danielcopper/decky-romm-sync/issues/271) migrates
persistence from JSON files to SQLite. The epic decided that `settings.json`
"stays JSON", but the recorded rationale is thin — three asserted adjectives in
the epic's storage-scope table ("small, rarely written; credential security is
orthogonal"), no ADR, never stress-tested key by key. Every later mention only
*cites* the epic (`domain/sync_settings.py`, `database-design.md`), never
re-argues it.

In practice the boundary drifted to **by file, not by concept**, and two
inconsistencies surfaced while scoping the Repository Protocols
([#782](https://github.com/danielcopper/decky-romm-sync/issues/782)):

- **`SyncSettings` is config that landed in the DB.** The save-sync feature
  toggles (`save_sync_enabled`, `sync_before_launch`, `sync_after_exit`,
  `default_slot`, `autocleanup_limit`) became a SQLite aggregate + `sync_settings`
  table (#788 / #780). These are pure user-intent toggles — appsettings, not
  state — yet structurally-identical config in `settings.json` stayed JSON. So
  "settings stay JSON" was already false at the concept level.
- **Platform models a sync toggle that lives elsewhere.** ADR-0001 made
  `Platform` a full aggregate; its `excluded_from_sync` field is the same concept
  as `enabled_platforms` in `settings.json` (inverted), but `excluded_from_sync`
  is **dead code** — declared on the aggregate and as a SQL column, read/written
  nowhere. Platform's other justification fields (`emulation_stack`,
  `manual_emulator_path`) **do not exist in code**, and its `display_name`
  caching is already covered by denormalisation onto the ROM rows.

This forced the underlying question: should `settings.json` move *into* the DB,
or is the boundary simply mis-drawn? Reviewed from both sides:

- **"Decky convention keeps settings as JSON"** is self-defeating as an argument
  — by that logic the whole SQLite migration is wrong. Dropped.
- **"Config can't be recovered after the empty-start cutover"** was overstated —
  the user *can* re-type it; the real cost is manual re-entry, removable with a
  one-time importer. Weak on its own.
- **"settings.json has no `.prev` backup, so a crash truncates it"** (the
  strongest pro-DB point) is **wrong**: `_locked_write` already writes to a temp
  file and `os.replace`s it atomically, so a crash leaves the prior file intact.
  The missing one-deep backup is cheaply addable in JSON, not a migration driver.

What *does* hold: flat config has no cross-row invariants or relationships, so
SQLite's value (aggregates, foreign keys, transactional integrity) buys nothing
for it — moving it in is the relational-store tax for none of the benefit. The
fix is therefore not "move settings to the DB" but **draw the boundary by what
the data *is*, not which file it currently lives in.**

## Decision

Persisted data falls into three buckets, and the bucket decides the store:

1. **User-intent config** — flat, no relationships, the user sets it. → **`settings.json`** (appsettings.json-style flat file).
2. **Observed / derived-from-an-external-source** — read, not set (e.g. RetroDECK/RetroArch config on the system). → **read live by default.** Persist (in SQLite) **only** with a concrete reason: *cross-run change detection* (store the last-seen value to diff against live) **or** *offline survival of remote data*.
3. **Synced / derived relational state with real invariants** — per-ROM groups, history, caches of remote data. → **SQLite aggregates.**

### Full mapping

| Data | Today | Bucket | Home | Why |
| --- | --- | --- | --- | --- |
| `romm_url`, `romm_user`, `romm_pass`, `steamgriddb_api_key` | settings.json | 1 | **settings.json** | User-set config/secrets. No DB security gain (plaintext either way); not re-derivable, must survive the empty-start cutover. |
| `steam_input_mode`, `log_level`, `romm_allow_insecure_ssl`, `collection_create_platform_groups` | settings.json | 1 | **settings.json** | Pure scalar toggles. |
| `enabled_platforms`, `enabled_collections` | settings.json | 1 | **settings.json** | Sync-*selection* = user intent. No relational invariants; the available list comes from RomM, the choice is config. |
| `save_sync_enabled`, `sync_before_launch`, `sync_after_exit`, `default_slot`, `autocleanup_limit` (SyncSettings) | save_sync_state.json → `sync_settings` table | 1 | **settings.json** | Feature toggles = config. The `sync_settings` table is dropped. |
| `device_name` | save_sync_state.json → `device` table | 1 | **settings.json** | User-set label. |
| `device_id` (server_device_id) | save_sync_state.json → `device` table | 3 | **`kv_config`** (DB) | Server-issued identity; singleton scalar, no invariants → a kv row, not a table. |
| `retrodeck_home_path` (+`_previous`), `save_sort_settings` (+`_previous`) | state.json | 2 | **`kv_config`** (DB) | Observed values persisted as last-seen markers for cross-run change detection (`migration.py` diffs live vs stored to trigger path migration). |
| `shortcut_registry`, `installed_roms`, `downloaded_bios`, sync scalars | state.json | 3 | DB (`roms`, `rom_installs`, `downloaded_bios`, `sync_runs`) | Synced/derived relational state. Unchanged from the epic. |
| per-ROM saves, playtime | save_sync_state.json | 3 | DB (`rom_save_states`/`rom_save_files`, `rom_playtime`) | Per-ROM state with invariants. Unchanged. |
| RomM metadata, firmware inventory | metadata_cache.json, firmware_cache.json | 3 | DB (`rom_metadata`, `firmware_cache`) | Offline cache of remote data, per owning table. Unchanged. |

No generic `romm_cache` catch-all table: remote data is cached in the table that
owns it (`rom_metadata`, `firmware_cache`); a generic cache table would be an
untyped dumping ground (the trap `kv_config` itself is fenced against).

## Consequences

**Three tables + aggregates are dropped from the #780 schema:**

- `sync_settings` table + `SyncSettings` aggregate — knobs move to `settings.json`.
- `platforms` table + `Platform` aggregate — **this supersedes ADR-0001.** Platform reverts to a denormalised `platform_slug` string (ADR-0001's rejected option 1). Sync exclusion stays `enabled_platforms` in settings; display name survives RomM downtime via the existing denormalisation onto ROM rows; `excluded_from_sync` (dead) is removed. Rebuild a Platform aggregate when a concrete need lands (the standalone-emulator roadmap), not speculatively.
- `device` table + `Device` aggregate — `device_id` → a `kv_config` row, `device_name` → `settings.json`. The two were unrelated scalars sharing a struct, so the split is honest, not a split aggregate.

**The Repository Protocol set (#782) shrinks 12 → 9:** `Rom`, `RomInstall`,
`RomMetadata`, `Playtime`, `RomSaveState`, `BiosFile`, `FirmwareCache`,
`SyncRun`, `KvConfig`. (Dropped: `SyncSettings`, `Platform`, `Device`.)

**Migration paths** (resolved here, implemented downstream):

- **SyncSettings (5 knobs) + `device_name`: a JSON→JSON move**, independent of
  SQLite — it can ship anytime, decoupled from the cutover. A settings-schema
  bump (`migrate_settings`, `_SETTINGS_VERSION` 3 → 4 in
  `domain/state_migrations.py`) reads the values out of `save_sync_state.json`
  once and folds them into `settings.json`; services repoint to settings; the
  keys are dropped from the save-sync state shape. Because these now live in
  `settings.json`, they **survive the empty-start cutover** untouched.
- **`device_id`: `save_sync_state.json` → `kv_config`**, at the cutover
  ([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)) — it is
  DB-bound, so it moves when the DB becomes the live store.
- **Schema:** amend `001_initial.sql` to remove the three tables. The DB is
  created-but-unused pre-cutover (the migration runner from
  [#781](https://github.com/danielcopper/decky-romm-sync/issues/781) applies the
  schema, but no service reads it yet), so any existing `romm_sync.db` is
  disposable — amending the initial schema is correct; no `DROP TABLE` migration
  is needed for real data that does not exist.

**`kv_config` stays** as the home for the change-detection markers and
`device_id`. It remains reserved for the truly-singleton and miscellaneous —
not a dumping ground.

**Not a one-way door.** If the standalone-emulator work later needs genuinely
locally-owned per-platform state, a `Platform` aggregate is reintroduced then,
with real call sites — a numbered migration, not a user-facing break.

## Alternatives considered

- **Move everything (incl. settings + secrets) into SQLite.** Rejected: flat
  config has no relational payoff (DB-as-hashmap), and the strongest technical
  pro-DB argument (no atomic settings write) is factually wrong — `_locked_write`
  already gives atomicity via temp-file + `os.replace`. A full move buys one
  migration mechanism at the cost of a one-time importer + a credentials table,
  for config that has nothing relational to gain.
- **Keep the status-quo by-file boundary.** Rejected: it is not principled — it
  leaves config (`SyncSettings`) in the DB and dead config (`excluded_from_sync`)
  in the schema. The asymmetry is incidental, not designed.
- **Keep `Platform`/`Device` as forward-looking aggregates.** Rejected on YAGNI:
  `excluded_from_sync` is dead code, `emulation_stack`/`manual_emulator_path`
  do not exist, display-name caching is already covered, and `device` is two
  unrelated scalars. Build the aggregate when the need is concrete.
