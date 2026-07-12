-- =============================================================================
-- 014_add_paused_sync_run_status.sql — 'paused' terminal run status
-- Issue #1383 (session budget)
-- =============================================================================
--
-- Extends the sync_runs status CHECK with 'paused': a run the session-budget
-- gate stopped deliberately at a chunk boundary because Steam's renderer is near
-- its per-session heap budget. Distinct from 'interrupted' (an external death —
-- a frontend crash / backend restart) so the UI can say "(paused)" and offer a
-- resume-with-restart flow, never blaming the stop on a crash or the user's
-- Cancel. Both 'paused' and 'interrupted' are resumable; the split is purely the
-- reason shown to the user.
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt in place:
-- create the widened twin, copy every row unchanged, drop the old table, rename.
-- sync_runs has no indexes and no incoming foreign keys, so the rename is the
-- whole story. Existing rows keep their historical status — no data rewrite;
-- only runs terminated after this migration can carry the new value.
--
-- Transaction-safe DDL only — the runner (adapters/sqlite_migrations.py) wraps
-- BEGIN/COMMIT and stamps PRAGMA user_version = 14.
-- -----------------------------------------------------------------------------
CREATE TABLE sync_runs_new (
    id                    TEXT PRIMARY KEY,         -- caller-injected uuid
    started_at            TEXT    NOT NULL,         -- ISO-8601
    status                TEXT    NOT NULL CHECK (status IN ('running', 'completed', 'cancelled', 'interrupted', 'paused', 'errored')),
    platforms_planned     INTEGER NOT NULL,
    roms_planned          INTEGER NOT NULL,
    finished_at           TEXT,                     -- ISO-8601; NULL while running
    platforms_completed   TEXT CHECK (platforms_completed IS NULL OR json_valid(platforms_completed)),    -- JSON array
    collections_completed TEXT CHECK (collections_completed IS NULL OR json_valid(collections_completed)), -- JSON array
    error                 TEXT                      -- NULL unless cancelled / interrupted / paused / errored
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
