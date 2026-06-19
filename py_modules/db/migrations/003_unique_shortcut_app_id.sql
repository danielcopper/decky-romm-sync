-- =============================================================================
-- 003_unique_shortcut_app_id.sql — one Steam appId is bound to at most one ROM
-- Issue #1036 (stale-removal wipes a freshly-synced shortcut on appId reuse)
-- =============================================================================
--
-- A Steam shortcut's appId is CRC32(exe + name); a server switch / re-import
-- reissues rom_ids for the same games, which produce the SAME appId (unchanged
-- exe + name). The sync UPSERT conflicts on rom_id (PK) only, so the new rom_id
-- INSERTs a fresh bound row while the old rom_id keeps the same appId — two
-- bound rows then share one appId. The stale pass flags the old row and emits
-- its still-live appId for removal, wiping the shortcut the run just created.
--
-- Fix: a partial UNIQUE index makes "two bound rows share an appId" impossible.
-- First de-dup any pre-existing collision so the index can build (keep the
-- newest binding — the higher, more recently server-issued rom_id — and unbind
-- the older colliding siblings; ADR-0007 retention: unbind NULLs the binding,
-- the row survives). The index is partial (WHERE shortcut_app_id IS NOT NULL)
-- so multiple unbound rows keep a NULL appId without colliding.
--
-- Transaction-safe DDL/DML only — the runner (adapters/sqlite_migrations.py)
-- wraps BEGIN/COMMIT and stamps PRAGMA user_version = 3.
-- -----------------------------------------------------------------------------

-- De-dup pre-existing collisions: keep MAX(rom_id) per appId, unbind the rest.
UPDATE roms SET shortcut_app_id = NULL
WHERE shortcut_app_id IS NOT NULL
  AND rom_id NOT IN (
    SELECT MAX(rom_id) FROM roms
    WHERE shortcut_app_id IS NOT NULL
    GROUP BY shortcut_app_id
  );

-- Bound appIds are now unique; multiple NULL (unbound) rows are still allowed.
CREATE UNIQUE INDEX idx_roms_shortcut_app_id
  ON roms(shortcut_app_id) WHERE shortcut_app_id IS NOT NULL;
