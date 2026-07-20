-- =============================================================================
-- 019_add_collection_sync_state.sql — per-collection completion stamp table
-- Issue #742 (collection-level incremental skip) / ADR-0023
-- =============================================================================
--
-- Backs the CollectionSyncState aggregate: one row per synced user/smart
-- collection recording the collection's server updated_at, our own completion
-- timestamp, the server rom_count, and the full member set at the point it
-- *fully* synced (its last apply chunk committed). The collection sibling of
-- platform_sync_state (012): the incremental-skip gate reads updated_at as the
-- membership-stable signal (and completed_at as the reference for the scoped
-- updated_after member-content probe) so an unchanged collection is not
-- re-paginated on the next run.
--
-- Keyed by (collection_id, collection_kind) — a user collection id and a smart
-- collection id can collide (both small ints on the server), so the kind is part
-- of the identity. collection_id is TEXT for uniformity with the RomM ids the
-- work queue threads (a user/smart id is numeric, stringified). Only 'user' and
-- 'smart' kinds are ever stored; franchise/virtual collections are never stamped.
--
-- member_rom_ids is a JSON array of the collection's member rom ids at completion
-- (json_valid-checked TEXT, like sync_runs.platforms_completed). A collection has
-- no local membership column to reconstruct from — roms.platform_slug is
-- per-platform — so the skip replays this stored set into the run's synced_rom_ids
-- and Steam-collection membership map, resolving each id through the registry.
--
-- updated_at is the collection row's server updated_at (TEXT); rom_count is a
-- plain non-negative INTEGER. A leaf table with no cascade children, so the row
-- is upserted with INSERT OR REPLACE and Force Full Sync clears the whole table.
--
-- Transaction-safe DDL only — the runner (adapters/sqlite_migrations.py) wraps
-- BEGIN/COMMIT and stamps PRAGMA user_version = 19.
-- -----------------------------------------------------------------------------
CREATE TABLE collection_sync_state (
    collection_id   TEXT    NOT NULL,             -- RomM collection id (user/smart), stringified
    collection_kind TEXT    NOT NULL,             -- 'user' | 'smart'
    updated_at      TEXT    NOT NULL,             -- collection's server updated_at at completion (membership signal)
    completed_at    TEXT    NOT NULL,             -- ISO-8601; our sync time — the updated_after member-probe reference
    rom_count       INTEGER NOT NULL,             -- server ROM count captured at completion
    member_rom_ids  TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(member_rom_ids)),  -- JSON array of member rom ids
    PRIMARY KEY (collection_id, collection_kind)
) STRICT;
