# Per-ROM table-per-aggregate split

## Status

Accepted

## Context

Epic [#271](https://github.com/danielcopper/decky-romm-sync/issues/271) proposed a "hybrid 5-table" layout with **one
`roms` mega-table** holding all per-ROM state, as a _starting point_ — it explicitly deferred the final layout to the
schema sub-issue (#780). The aggregate set locked in #788 has five per-ROM aggregates: `Rom`, `RomInstall`,
`RomMetadata`, `Playtime`, `RomSaveState` (plus the `FileSyncState` 1:N child). #780 had to choose how they sit in
tables.

Two options for the per-ROM cluster:

1. **Mega-table.** One `roms` row carries identity + install + metadata + playtime + save-state scalars. `RomRepository`
   inserts the row; the four secondary repositories `UPDATE` their own column slices.
2. **Table-per-aggregate.** Each per-ROM aggregate gets its own table keyed by `rom_id` (`roms`, `rom_installs`,
   `rom_metadata`, `rom_playtime`, `rom_save_states`), with `rom_save_files` as the save child.

The foreign-key policy is entangled with this choice. The epic locked "**selective FK only** — one FK,
`rom_save_files → roms ON DELETE CASCADE`, no others." That rule was written for the mega-table world, where the only FK
candidates were `platform_slug` (a cross-aggregate reference) and the single `rom_save_files` child.

## Decision

Adopt **table-per-aggregate** for the per-ROM cluster, and give the per-ROM child tables **`ON DELETE CASCADE` foreign
keys to `roms`**.

The deciding factor is **integrity, not performance**. Read performance is a non-issue at single-user scale (point reads
in microseconds, joins in milliseconds — established during the #271 research), so it did not decide the layout. What
decides it: the per-ROM aggregates are **all-or-nothing groups**. An install is either fully present (a launch file, an
install dir, a timestamp) or absent; metadata is cached or not. As loose nullable columns in a mega-table, the schema
**cannot express that** — a half-set install (`file_path` set, `installed_at` NULL) is a representable, invalid state,
and `roms` becomes a ~35-column grab-bag where most columns are NULL for any given ROM. As separate tables, the row's
**existence** means "this group is present and complete" (`NOT NULL` within), and "not every user has saves / metadata /
a download" becomes simply "no row."

Secondary factors:

- **One Repository per aggregate** (the CONTEXT.md rule) maps 1:1 onto one table per aggregate — no multiple
  repositories writing column slices of a shared row, and no implicit "the `roms` row must exist before any secondary
  write" ordering baked into the persistence layer.
- **Legibility / AI-navigability** — "where is `RomMetadata` persisted?" answers itself (`rom_metadata`). The mega-table
  would need column-name disambiguation (`system` appears on both `RomInstall` and `RomSaveState`).

On foreign keys: the split introduces a category that did not exist when the "one FK only" rule was written — **per-ROM
child tables that are true parent-child** (per-ROM state is owned by the ROM and meaningless without it). They take
`ON DELETE CASCADE`, exactly like the one FK the epic already sanctioned. Cross-aggregate references (`platform_slug`)
keep **no** FK. This is the same intent as the locked rule — FK for ownership, no FK for cross-references — applied to
the new tables. "Playtime survives shortcut removal" is preserved: shortcut removal does not delete the `roms` row; only
a deliberate full prune does, and cascading per-ROM state when the ROM is genuinely gone is correct.

## Consequences

- ~13 tables instead of the mega-table's ~9.
- A full game-detail read (all per-ROM aggregates for one ROM) becomes a few point reads / a join rather than one row
  read — sub-millisecond and rare. In exchange, the hot library-list scan and the frequent playtime write touch
  **narrower** rows.
- A library prune is a single `DELETE FROM roms WHERE …` — the CASCADE FKs clear every per-ROM child. No app-level
  multi-table cleanup, no orphan risk.
- Foreign-key count goes 1 → 5. `PRAGMA foreign_keys=ON` (already locked by the epic, set per-connection by the adapter)
  is required for the cascades to fire.
- This **deviates from the epic's literal wording** ("one `roms` mega-table", "no other FKs"). Both were explicit
  starting points written before the split; #780 is the sanctioned place to finalize the layout, so this is a
  resolution, not a relitigation. ADR-0001's "`platform_slug` as an FK" wording is clarified there to mean a logical
  reference, not a DB constraint.
- It is **not a one-way door.** Merging back to a mega-table (or splitting a table further) later is an internal
  numbered migration plus a repository SQL rewrite — not a user-facing breaking change.

## Alternatives considered

- **Mega-table** (rejected). Fewer tables, one point read for game-detail, and consistent with the literal "one FK"
  rule. Rejected because it cannot enforce the all-or-nothing groups (many nullable columns; half-set states are
  representable) and turns `roms` into a wide grab-bag. The integrity cost outweighs a read-speed win that is irrelevant
  at single-user scale.
- **Middle-ground hybrid** — keep the light, always-present state (identity, playtime) in `roms` and split out only the
  bulky/independent `rom_metadata` and `rom_save_states`. Rejected: it has no clean rule ("which fields live where?")
  and is less legible than the uniform full split.
