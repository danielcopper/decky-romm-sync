-- =============================================================================
-- 001_initial.sql — initial SQLite schema for decky-romm-sync
-- Issue #780 (schema design) · Epic #271 (JSON -> SQLite persistence migration)
-- =============================================================================
--
-- This file is pure DDL. It is applied once, to an empty database, on first
-- run after the cutover (#784). There is NO JSON importer — SQLite starts
-- empty and the library re-syncs (BREAKING CHANGE, beta plugin).
--
-- HOW THIS FILE IS LOADED is intentionally out of scope here. Connection-level
-- PRAGMAs (journal_mode=WAL, synchronous=NORMAL, busy_timeout, temp_store,
-- foreign_keys=ON, isolation_level) are set per-connection by the adapter, and
-- schema versioning (PRAGMA user_version vs a schema_migrations table) is owned
-- by the migration-framework sub-issue (#782). This file only declares tables.
--
-- NOTE: foreign_keys must be ON at runtime for the ON DELETE CASCADE clauses
-- below to fire. That PRAGMA is per-connection (the epic locks foreign_keys=ON)
-- and cannot be expressed in the schema itself.
--
-- -----------------------------------------------------------------------------
-- Decisions locked here (the #780 deferred-decision set). Each is also noted
-- inline at the table/column it governs.
-- -----------------------------------------------------------------------------
--
-- STRICT tables everywhere (#4). SQLite >= 3.37 (Steam Deck ships 3.50). STRICT
--   restricts column types to INTEGER / REAL / TEXT / BLOB / ANY — there is no
--   native BOOL or DATETIME. Consequences applied throughout:
--     * Booleans      -> INTEGER 0/1, guarded by CHECK (col IN (0, 1)).
--     * Event times   -> TEXT, ISO-8601 (human-readable, lexically sortable).
--     * TTL/cache time -> REAL, Unix epoch seconds (cheap age arithmetic).
--     * JSON arrays/objects -> TEXT, guarded with CHECK (json_valid(col)).
--   The two timestamp representations are aggregate-driven, not accidental:
--   caches (rom_metadata, firmware_cache) do age math, so they store epochs;
--   everything else stores ISO strings it only displays and orders.
--
-- Per-ROM layout: table-per-aggregate, NOT a single roms mega-table (#1 + #7).
--   The epic's "one roms mega-table" was the starting proposal; #780 owns the
--   final layout. Each per-ROM aggregate gets its own table so "state absent"
--   is "no row" rather than a wide row of NULLs. This lets the schema enforce
--   all-or-nothing groups (an install is all-present or absent) that loose
--   nullable columns in a mega-table could not. Read perf is a non-issue at
--   single-user scale (the deciding factor was integrity, not speed). One
--   Repository per aggregate (CONTEXT.md) maps 1:1 onto these tables. Rationale
--   and the rejected mega-table alternative: docs/adr/0002.
--
-- Foreign keys: per-ROM child tables CASCADE to roms; cross-aggregate slug
--   references carry NO FK. The epic locked "one FK only" for the mega-table
--   world, where the only candidates were platform_slug (a cross-reference) and
--   the lone rom_save_files child. The split creates a new category — per-ROM
--   tables that are true parent-child (per-ROM state is owned by the ROM) — so
--   they take ON DELETE CASCADE, same as rom_save_files. platform_slug stays a
--   logical/join reference with no enforced FK: an FK there would force sync
--   ordering (platforms before ROMs) and block platform pruning while ROMs
--   exist, fighting the disk-truth-pruning model. "Playtime survives shortcut
--   removal" is preserved — shortcut removal does not delete the roms row; only
--   a deliberate full prune does, and cascading then is correct.
--
-- No lookup/normalization tables for `system` (#2). ~15-30 distinct values over
--   ~20k rows is trivial storage; a `systems` table owns no state (unlike
--   `platforms`, which is a real aggregate per ADR-0001) and would fight the
--   deliberate denormalization of `system` onto rom_installs. `system` stays
--   TEXT. `platforms` exists as an aggregate table, not a lookup table.
--
-- No blanket audit columns (#5). No generic created_at / updated_at. The
--   aggregates already model the timestamps that matter (last_synced_at,
--   installed_at, downloaded_at, cached_at). Generic audit columns would be
--   redundant write overhead and clash with the lean-schema, no-`extra`-hedge
--   ethos (see database-design.md). Add later via migration if a real need
--   appears.
--
-- Retention (#3): rows in roms are deleted ONLY on a deliberate library prune
--   (ROM genuinely gone from RomM), never on transient absence. No time-based
--   GC: row count is bounded by library size, not time (roms mirror RomM, they
--   do not accumulate like a log). A prune DELETE on roms cascades to every
--   per-ROM child table via the FKs below.
--
-- NULL vs DEFAULT (#6): DEFAULT where "absent" has a natural zero and the field
--   is always conceptually present (playtime counters, booleans, emulator);
--   NULL where absence is meaningful and distinct from any value (install cols
--   when not installed, metadata when never cached, unresolved ids). The
--   standout invariant: rom_save_states.own_upload_ids — NULL ("attribution
--   unknown / legacy") is DISTINCT from '[]' ("we uploaded nothing"). See its
--   column comment.
--
-- No secondary indexes (epic: "no tuning until profiling shows a problem").
--   Every point-lookup and cascade rides a PRIMARY KEY (FK child columns are
--   all PK-covered, so CASCADE is already indexed). Candidate indexes for when
--   profiling justifies them — roms(platform_slug) for platform-grouped views,
--   sync_runs(status, started_at) for the last-completed-run query — are
--   deliberately deferred, not forgotten.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- roms — Rom aggregate: ROM identity + the Steam-shortcut binding.
-- One row per ROM the plugin tracks. Created when a ROM is synced from RomM;
-- the anchor every per-ROM child table cascades from. Deleted only on a
-- deliberate library prune (see retention note above).
-- -----------------------------------------------------------------------------
CREATE TABLE roms (
    rom_id          INTEGER PRIMARY KEY,            -- RomM id; server-issued, stable
    platform_slug   TEXT    NOT NULL,               -- logical ref to platforms.slug (NO FK — see header)
    name            TEXT    NOT NULL,
    fs_name         TEXT    NOT NULL,
    shortcut_app_id INTEGER NOT NULL,               -- Steam non-Steam shortcut app id
    last_synced_at  TEXT    NOT NULL,               -- ISO-8601
    cover_path      TEXT,                           -- NULL until artwork is written
    igdb_id         INTEGER,                        -- NULL until resolved
    sgdb_id         INTEGER,                        -- NULL until resolved
    ra_id           INTEGER                         -- NULL until resolved
) STRICT;


-- -----------------------------------------------------------------------------
-- rom_installs — RomInstall aggregate: where a downloaded ROM lives on disk.
-- Present ONLY while the ROM is downloaded (row created on download-complete,
-- deleted on uninstall). All columns NOT NULL: the row's existence means a
-- complete install record — the all-or-nothing group the split exists to
-- enforce. platform_slug / system are denormalized so migration + save-sort
-- read an install without joining roms.
-- -----------------------------------------------------------------------------
CREATE TABLE rom_installs (
    rom_id        INTEGER PRIMARY KEY REFERENCES roms(rom_id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL,                    -- the specific launch file
    install_path  TEXT NOT NULL,                    -- the install directory
    platform_slug TEXT NOT NULL,                    -- denormalized; logical ref (NO FK)
    system        TEXT NOT NULL,                    -- emulator system slug; plain TEXT (#2, no lookup)
    installed_at  TEXT NOT NULL                     -- ISO-8601
) STRICT;


-- -----------------------------------------------------------------------------
-- rom_metadata — RomMetadata aggregate: cached RomM game metadata.
-- Present only when metadata has been cached; regenerated independently of
-- library sync on a 7-day staleness check driven by cached_at. genres /
-- companies / game_modes / steam_categories are JSON arrays as TEXT (#8) — they
-- are display data, never queried by element, so normalization buys nothing.
-- -----------------------------------------------------------------------------
CREATE TABLE rom_metadata (
    rom_id             INTEGER PRIMARY KEY REFERENCES roms(rom_id) ON DELETE CASCADE,
    summary            TEXT NOT NULL,
    genres             TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(genres)),
    companies          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(companies)),
    game_modes         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(game_modes)),
    steam_categories   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(steam_categories)),  -- JSON array of ints (Steam category ids)
    player_count       TEXT NOT NULL,
    first_release_date INTEGER,                     -- Unix epoch seconds (INTEGER: sub-second precision irrelevant for a release date); NULL when unknown
    average_rating     REAL,                        -- NULL when unrated
    cached_at          REAL NOT NULL                -- Unix epoch (s); presence + 7-day staleness driver
) STRICT;


-- -----------------------------------------------------------------------------
-- rom_playtime — Playtime aggregate: cumulative play time + open-session marker.
-- Present once a ROM has been played. Counters DEFAULT 0 (a played-zero ROM is a
-- valid state, not "absent" — so these are DEFAULT, not NULL, #6). Independent
-- lifecycle: playtime survives shortcut removal (the roms row persists through
-- shortcut/uninstall; only a full prune deletes it, cascading here).
-- -----------------------------------------------------------------------------
CREATE TABLE rom_playtime (
    rom_id                    INTEGER PRIMARY KEY REFERENCES roms(rom_id) ON DELETE CASCADE,
    total_seconds             INTEGER NOT NULL DEFAULT 0,
    session_count             INTEGER NOT NULL DEFAULT 0,
    last_session_start        TEXT,                 -- ISO-8601; NULL when no session is open
    last_session_duration_sec INTEGER,             -- NULL until a session has been recorded
    note_id                   INTEGER               -- RomM playtime-note id; NULL until linked
) STRICT;


-- -----------------------------------------------------------------------------
-- rom_save_states — RomSaveState aggregate: per-ROM save-sync scalars.
-- Present only when save tracking exists for the ROM. The per-file baselines
-- live in the rom_save_files child below.
-- -----------------------------------------------------------------------------
CREATE TABLE rom_save_states (
    rom_id             INTEGER PRIMARY KEY REFERENCES roms(rom_id) ON DELETE CASCADE,
    active_slot        TEXT,                        -- NULL = legacy "no slots" mode (meaningful, #6)
    slot_confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (slot_confirmed IN (0, 1)),  -- bool
    emulator           TEXT NOT NULL DEFAULT 'retroarch',
    system             TEXT NOT NULL DEFAULT '',    -- system the ROM runs under (save-path resolution)
    last_synced_core   TEXT,                        -- NULL until first sync under a known core
    -- own_upload_ids: JSON array of server save ids we uploaded. NULL and '[]'
    -- are BOTH meaningful and DISTINCT: NULL = attribution unknown / legacy
    -- entry (UI hides the "yours" badge); '[]' = we definitely uploaded
    -- nothing. The CHECK permits NULL or a valid JSON array, never invalid text.
    own_upload_ids     TEXT CHECK (own_upload_ids IS NULL OR json_valid(own_upload_ids)),
    slots              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(slots)),  -- merged slot listing (read-model cache)
    last_sync_check_at TEXT                         -- ISO-8601; NULL until the matrix is first evaluated
) STRICT;


-- -----------------------------------------------------------------------------
-- rom_save_files — FileSyncState value object: per-file sync baselines.
-- 1:N child of a ROM's save state. Composite key (rom_id, filename). This is
-- the relationship the epic always intended to back with a CASCADE FK; here it
-- is one of the per-ROM FKs. NOT NULL on tracked_save_id + last_sync_hash
-- enforces the aggregate's adopt_baseline invariant at the DB level: every
-- tracked file carries both a server save id and a hash baseline.
-- -----------------------------------------------------------------------------
CREATE TABLE rom_save_files (
    rom_id                      INTEGER NOT NULL REFERENCES roms(rom_id) ON DELETE CASCADE,
    filename                    TEXT    NOT NULL,
    tracked_save_id             INTEGER NOT NULL,   -- invariant: a tracked file has a server save id
    last_sync_hash              TEXT    NOT NULL,   -- invariant: ... and a hash baseline
    last_sync_at                TEXT    NOT NULL DEFAULT '',  -- ISO-8601, or '' = never-synced sentinel (matches aggregate)
    last_sync_server_updated_at TEXT    NOT NULL DEFAULT '',  -- ISO-8601, or '' = never-synced sentinel
    last_sync_server_save_id    INTEGER,
    last_sync_server_size       INTEGER,
    last_sync_local_mtime       REAL,               -- filesystem mtime (epoch s)
    last_sync_local_size        INTEGER,
    PRIMARY KEY (rom_id, filename)
) STRICT;


-- -----------------------------------------------------------------------------
-- platforms — Platform aggregate: per-platform state the plugin owns locally.
-- A real aggregate (ADR-0001), not a normalization lookup: it carries the
-- cached display name (survives RomM downtime) and the exclude-from-sync toggle.
-- Keyed by RomM slug. Referenced by roms / rom_installs / downloaded_bios /
-- firmware_cache via platform_slug, but with NO enforced FK (see header).
-- -----------------------------------------------------------------------------
CREATE TABLE platforms (
    slug               TEXT PRIMARY KEY,            -- RomM platform slug
    display_name       TEXT NOT NULL,               -- cached for QAM display during RomM downtime
    excluded_from_sync INTEGER NOT NULL DEFAULT 0 CHECK (excluded_from_sync IN (0, 1))  -- bool
) STRICT;


-- -----------------------------------------------------------------------------
-- downloaded_bios — BiosFile aggregate: one downloaded BIOS/firmware file.
-- Composite identity (platform_slug, file_name): a bare filename is unsafe —
-- two platforms can ship same-named BIOS. firmware_id is nullable RomM metadata,
-- NOT part of the identity. Tracked so a RetroDECK-home migration can relocate
-- the file.
-- -----------------------------------------------------------------------------
CREATE TABLE downloaded_bios (
    platform_slug TEXT    NOT NULL,                 -- logical ref (NO FK)
    file_name     TEXT    NOT NULL,
    file_path     TEXT    NOT NULL,
    downloaded_at TEXT    NOT NULL,                 -- ISO-8601
    firmware_id   INTEGER,                          -- RomM firmware id; metadata, not identity
    PRIMARY KEY (platform_slug, file_name)
) STRICT;


-- -----------------------------------------------------------------------------
-- firmware_cache — FirmwareCacheEntry aggregate: TTL-cached RomM firmware
-- inventory. Replaced wholesale on refresh; the 1-hour TTL check lives in the
-- service. id is nullable (legacy entries) so it cannot be the key — the natural
-- key is (platform_slug, name). cached_at is per-row and equal across a refresh
-- batch (matches the aggregate's per-entry field); the service reads it for the
-- whole-cache TTL.
-- -----------------------------------------------------------------------------
CREATE TABLE firmware_cache (
    platform_slug   TEXT    NOT NULL,               -- logical ref (NO FK)
    name            TEXT    NOT NULL,               -- firmware filename
    id              INTEGER,                        -- RomM firmware id; NULL for legacy entries
    file_size_bytes INTEGER NOT NULL,
    cached_at       REAL    NOT NULL,               -- Unix epoch (s); whole-cache TTL driver
    PRIMARY KEY (platform_slug, name)
) STRICT;


-- -----------------------------------------------------------------------------
-- sync_runs — SyncRun aggregate: one sync operation, as a state machine.
-- History table (one row per run), NOT a singleton: a 1-row table would lose the
-- distinction between the currently-running run and the last completed one (a
-- new run starting as 'running' would erase the displayable stats of the last
-- 'completed' run). "Last successful sync" = newest row WHERE status='completed';
-- "is a sync running" = any row WHERE status='running'. Growth is trivial
-- (~daily -> ~2k rows / 5yr); a retention prune is deferred until it matters.
-- -----------------------------------------------------------------------------
CREATE TABLE sync_runs (
    id                    TEXT PRIMARY KEY,         -- caller-injected uuid
    started_at            TEXT    NOT NULL,         -- ISO-8601
    status                TEXT    NOT NULL CHECK (status IN ('running', 'completed', 'cancelled', 'errored')),
    platforms_planned     INTEGER NOT NULL,
    roms_planned          INTEGER NOT NULL,
    finished_at           TEXT,                     -- ISO-8601; NULL while running
    platforms_completed   TEXT CHECK (platforms_completed IS NULL OR json_valid(platforms_completed)),    -- JSON array
    collections_completed TEXT CHECK (collections_completed IS NULL OR json_valid(collections_completed)), -- JSON array
    error                 TEXT                      -- NULL unless cancelled / errored
) STRICT;


-- -----------------------------------------------------------------------------
-- device — Device aggregate (singleton): the registered device identity.
-- Single-row table guarded by CHECK (id = 1). A singleton with invariants, so
-- per CONTEXT.md it gets its own typed table rather than untyped kv_config rows.
-- device_id collapses the old device_id / server_device_id JSON pair (always the
-- same server row id).
-- -----------------------------------------------------------------------------
CREATE TABLE device (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    device_id   TEXT NOT NULL,                      -- server-issued
    device_name TEXT                                -- NULL until the user names it
) STRICT;


-- -----------------------------------------------------------------------------
-- sync_settings — SyncSettings aggregate (singleton): save-sync feature knobs.
-- Single-row table guarded by CHECK (id = 1). Distinct from settings.json
-- (which stays JSON per the epic). autocleanup_limit CHECK mirrors the
-- aggregate's >= 0 invariant; default_slot NULL = "no slots" mode (meaningful).
-- -----------------------------------------------------------------------------
CREATE TABLE sync_settings (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    save_sync_enabled  INTEGER NOT NULL DEFAULT 0 CHECK (save_sync_enabled IN (0, 1)),  -- bool
    sync_before_launch INTEGER NOT NULL DEFAULT 1 CHECK (sync_before_launch IN (0, 1)),  -- bool
    sync_after_exit    INTEGER NOT NULL DEFAULT 1 CHECK (sync_after_exit IN (0, 1)),  -- bool
    default_slot       TEXT DEFAULT 'default',      -- NULL = "no slots" mode (meaningful, #6)
    autocleanup_limit  INTEGER NOT NULL DEFAULT 10 CHECK (autocleanup_limit >= 0)
) STRICT;


-- -----------------------------------------------------------------------------
-- kv_config — small singleton scalars that do not justify their own aggregate
-- (CONTEXT.md). One row per key; value is a scalar or JSON-encoded blob the app
-- interprets per key. Anything with its own lifecycle or invariants does NOT
-- belong here (it gets an aggregate table) — kv_config is for the truly
-- miscellaneous.
--
-- Known keys at cutover:
--   retrodeck_home_path           current RetroDECK home (TEXT)
--   retrodeck_home_path_previous  pending-migration previous home (TEXT)
--   save_sort_settings            {sort_by_content, sort_by_core} (JSON)
--   save_sort_settings_previous   pending save-sort change (JSON)
-- Schema version is NOT a kv_config key — it is owned by the migration framework
-- (#782), most likely PRAGMA user_version.
-- -----------------------------------------------------------------------------
CREATE TABLE kv_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                             -- scalar or JSON-encoded; app interprets per key
) STRICT;
