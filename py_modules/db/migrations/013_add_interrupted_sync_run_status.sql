-- =============================================================================
-- 013_add_interrupted_sync_run_status.sql — 'interrupted' terminal run status
-- Issue #1025 (chunked apply)
-- =============================================================================
--
-- Extends the sync_runs status CHECK with 'interrupted': a run ended by an
-- external death (the frontend stopped heartbeating — a steamwebhelper crash —
-- or the backend was restarted mid-run) rather than by the user's Cancel.
-- Previously both wrote 'cancelled', so a crash showed up as "(cancelled)" in
-- the last-attempt line and blamed the user for a failure they didn't cause.
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt in place:
-- create the widened twin, copy every row unchanged, drop the old table, rename.
-- sync_runs has no indexes and no incoming foreign keys, so the rename is the
-- whole story. Existing rows keep their historical 'cancelled' status — no data
-- rewrite; only runs terminated after this migration carry the new value.
--
-- Transaction-safe DDL only — the runner (adapters/sqlite_migrations.py) wraps
-- BEGIN/COMMIT and stamps PRAGMA user_version = 13.
-- -----------------------------------------------------------------------------
CREATE TABLE sync_runs_new (
    id                    TEXT PRIMARY KEY,         -- caller-injected uuid
    started_at            TEXT    NOT NULL,         -- ISO-8601
    status                TEXT    NOT NULL CHECK (status IN ('running', 'completed', 'cancelled', 'interrupted', 'errored')),
    platforms_planned     INTEGER NOT NULL,
    roms_planned          INTEGER NOT NULL,
    finished_at           TEXT,                     -- ISO-8601; NULL while running
    platforms_completed   TEXT CHECK (platforms_completed IS NULL OR json_valid(platforms_completed)),    -- JSON array
    collections_completed TEXT CHECK (collections_completed IS NULL OR json_valid(collections_completed)), -- JSON array
    error                 TEXT                      -- NULL unless cancelled / interrupted / errored
) STRICT;

INSERT INTO sync_runs_new (
    id, started_at, status, platforms_planned, roms_planned,
    finished_at, platforms_completed, collections_completed, error
)
SELECT id, started_at, status, platforms_planned, roms_planned,
       finished_at, platforms_completed, collections_completed, error
FROM sync_runs;

DROP TABLE sync_runs;

ALTER TABLE sync_runs_new RENAME TO sync_runs;
