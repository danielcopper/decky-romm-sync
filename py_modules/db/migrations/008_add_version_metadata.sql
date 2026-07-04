-- =============================================================================
-- 008_add_version_metadata.sql — sibling-group key + version dimensions on Rom
-- Issue #1295 (first slice of #1267 — capture RomM version metadata) / ADR-0019
-- =============================================================================
--
-- RomM models one game as several dumps (region/language/revision/tag variants)
-- and coalesces siblings by a shared external-metadata id, scoped per platform.
-- The plugin dropped every one of these fields on fetch; these columns capture
-- them on the Rom aggregate (its anchor table, so they survive uninstall per
-- ADR-0007). Unlike the user-pin columns emulator_override / selected_disc,
-- these are SERVER-DERIVED facts: they ride the sync UPSERT and refresh every
-- sync.
--
-- sibling_group_key is nullable — an existing row is backfilled by the next
-- sync (a bound ROM whose key is still NULL forces its platform's incremental
-- skip to fall through to a full fetch, so one sync backfills every row). The
-- version dimensions default to empty (JSON '[]' arrays / '' revision / 0
-- is_main_sibling) so a pre-migration row reads as "no version metadata yet"
-- until that backfill lands. The roms table is STRICT and has no native BOOL,
-- so is_main_sibling is an INTEGER 0/1; the JSON arrays are TEXT guarded by
-- json_valid() to match the rom_metadata pattern.
--
-- Transaction-safe DDL only — the runner (adapters/sqlite_migrations.py) wraps
-- BEGIN/COMMIT and stamps PRAGMA user_version = 8.
-- -----------------------------------------------------------------------------
ALTER TABLE roms ADD COLUMN sibling_group_key TEXT;  -- "{source}:{id}:{platform_id}"; NULL until backfilled
ALTER TABLE roms ADD COLUMN regions TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(regions));      -- JSON array of str
ALTER TABLE roms ADD COLUMN languages TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(languages));  -- JSON array of str
ALTER TABLE roms ADD COLUMN revision TEXT NOT NULL DEFAULT '';
ALTER TABLE roms ADD COLUMN tags TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags));            -- JSON array of str
ALTER TABLE roms ADD COLUMN is_main_sibling INTEGER NOT NULL DEFAULT 0;  -- RomM rom_user.is_main_sibling (0/1)
