-- =============================================================================
-- 012_add_platform_sync_state.sql — per-platform completion stamp table
-- Issue #1025 (chunked apply) / ADR-0023
-- =============================================================================
--
-- Backs the PlatformSyncState aggregate: one row per platform recording the
-- timestamp + server ROM count at which that platform *fully* synced (its last
-- apply chunk committed). The incremental-skip gate reads this as the platform's
-- own effective last_sync so a run that durably completed some platforms but was
-- cancelled/crashed before the whole run finished (leaving the library-wide
-- last_sync unadvanced) still skips those completed platforms on the next run.
--
-- Keyed by platform_slug (the denormalized RomM slug, same as roms/rom_installs;
-- no platforms table exists to reference — ADR-0003). A leaf table with no
-- cascade children, so the row is upserted with INSERT OR REPLACE and Force Full
-- Sync clears the whole table alongside the completed-run history.
--
-- completed_at is TEXT ISO-8601 (same shape + comparison basis as
-- sync_runs.finished_at, which the skip's delta query keys off). rom_count is a
-- plain non-negative INTEGER.
--
-- Transaction-safe DDL only — the runner (adapters/sqlite_migrations.py) wraps
-- BEGIN/COMMIT and stamps PRAGMA user_version = 12.
-- -----------------------------------------------------------------------------
CREATE TABLE platform_sync_state (
    platform_slug TEXT    NOT NULL PRIMARY KEY,
    completed_at  TEXT    NOT NULL,            -- ISO-8601; the platform's effective last_sync
    rom_count     INTEGER NOT NULL             -- server ROM count captured at completion
) STRICT;
