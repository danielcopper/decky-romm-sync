# Adopt Platform as an aggregate

## Status

Superseded by [ADR-0003](0003-json-sqlite-persistence-boundary.md). Platform is **not** adopted as an aggregate — it
reverts to a denormalised `platform_slug` string (this document's rejected option 1), and the platform sync-exclusion
toggle stays as `enabled_platforms` in `settings.json`. See ADR-0003 for the reasoning (YAGNI: `excluded_from_sync` was
dead code, `emulation_stack` / `manual_emulator_path` never existed, display-name caching is covered by
denormalisation). The decision below is retained as historical context.

## Context

The plugin syncs ROMs from RomM, which provides each ROM with a `platform_slug` and `platform_name`. Today these strings
are denormalized across `shortcut_registry`, `installed_roms`, and `downloaded_bios` — there is no `Platform` entity
locally.

When migrating to SQLite (#271), we considered three options for platform identity:

1. **No Platform aggregate.** Keep `platform_slug: str` denormalized on every row. Cheapest. Matches today.
2. **Singleton `kv_config` blob.** Stuff a `platform_display_names` JSON map into the key-value table. Works for cached
   names; fails when we need user-editable per-platform settings.
3. **Adopt `Platform` as a full aggregate.** New table, new Repository, new domain dataclass. Costs a sync-ordering
   constraint (platforms refresh before ROMs).

## Decision

Adopt Platform as a full aggregate.

The deciding factor was the standalone-emulator roadmap: extending the plugin beyond RetroDECK (EmuDeck,
manually-installed emulators) requires local state we genuinely own (`emulation_stack`, `manual_emulator_path`, future
`excluded_from_sync` toggle). RetroDECK's own configuration files (`es_systems.xml`, `gamelist.xml`) own most existing
per-platform state — but that doesn't cover the non-RetroDECK case, which is the explicit direction of travel.

Other rationale:

- `display_name` caching survives RomM downtime for QAM display
- A normalized `platforms` table is a cleaner home for "per-platform preferences we own" than scattering them across
  `kv_config` keys
- Bundled reference data (`bios_registry.json`, `core_defaults.json`, RetroDECK's `es_systems.xml`) stays where it is —
  Platform only carries state we genuinely own and mutate

## Consequences

- `Rom`, `RomInstall`, `BiosFile`, `RomSaveState` carry `platform_slug` as a reference into the `platforms` table — not
  a free-floating string. This is a **logical / join reference, not a DB-enforced `FOREIGN KEY` constraint**: the
  selective-FK rule (epic #271, finalized in [ADR-0002](0002-per-rom-table-per-aggregate-split.md)) deliberately keeps
  no FK on `platform_slug`, because an enforced constraint would force insert ordering and block platform pruning while
  ROMs exist — fighting the disk-truth-pruning model.
- `Rom` drops the denormalized `platform_name` column (resolve via JOIN).
- Sync should refresh `platforms` before refreshing ROMs so display names resolve — an application-level ordering
  preference, **not** a DB foreign-key dependency (there is no enforced constraint to violate).
- Bundled defaults (`core_defaults.json`) and RetroDECK-managed state (`gamelist.xml` overrides) stay outside the
  Platform aggregate. The aggregate is for state we own locally, not for caching read-only reference data.
- The Platform aggregate stays lean today (`slug`, `display_name`, `excluded_from_sync` once shipped, `emulation_stack`
  once shipped) — not all future fields land at once.
