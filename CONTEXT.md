# CONTEXT.md — decky-romm-sync domain glossary

This file is a glossary. It defines the canonical meaning of project-specific
terms so that conversations, issues, PRs, and code stay aligned. It is *not* a
spec or design doc — implementation docs live in `docs/architecture/`, and
architectural decisions live in `docs/adr/`.

When a term resolves during a discussion, it gets added here. When a term's
meaning changes, the entry gets rewritten — not appended to.

## Terms

### Aggregate

A cluster of domain objects treated as a single unit for data consistency. Per
Cosmic Python chapters 1–7:

- Has **one root** entity (a dataclass) that is the only external entry point.
- Enforces all of its own **invariants** — outside code cannot violate them.
- Is the **transaction boundary**: saved atomically as a unit.
- Has exactly one **identity**. Other aggregates reference it by ID only, never
  by holding a Python reference to its internals.
- Mutation is **only via methods on the root**, named after the domain event
  that occurred (`adopt_baseline(...)`, `confirm_slot(...)`,
  `mark_installed(...)`). Direct field assignment from services is forbidden.
- Has exactly **one Repository** Protocol. The Repository's job is "give me
  this aggregate by ID, save this aggregate" — it may touch multiple tables
  under the hood.

What an aggregate is *not*: a DTO sent to the frontend, a query projection, or
"stuff that happens to live in the same file." Aggregate boundaries are
**invariant boundaries**, not storage boundaries.

Chapters 8+ of the CP book (domain events + message bus) are explicitly out of
scope. Triggers for revisiting that scope are documented in `CLAUDE.md`. The
concrete aggregate set, its tables, and the enforcement layers live in
`docs/architecture/database-design.md` — this entry defines the term, not the
inventory.

### Value Object

An immutable member of an aggregate, built whole and never mutated in place —
a `@dataclass(frozen=True, slots=True)`, not a `@cosmic_aggregate` root. It has
no identity of its own and no mutation surface to police, so it carries neither
the decorator nor the verb-method discipline an aggregate root does.
`FileSyncState` (inside `RomSaveState`) is the canonical example. The foil to
**Aggregate**: a root has identity and methods; a value object has neither.

### Persistence boundary (settings.json / save_sync_state.json / SQLite)

Where a piece of persisted state lives is a deliberate decision driven by what
the data *is*, not which file it historically lived in. Per
[ADR-0003](docs/adr/0003-json-sqlite-persistence-boundary.md), three buckets,
and the bucket picks the store:

1. **User-intent config** — flat, no relationships, the user sets it →
   **`settings.json`**. Includes credentials, scalar toggles, the sync-selection
   lists (`enabled_platforms`, `enabled_collections`), the save-sync feature
   toggles (`save_sync_enabled`, `sync_before_launch`, `sync_after_exit`,
   `default_slot`, `autocleanup_limit`), and `device_name`.
2. **Observed / derived-from-an-external-source** — read, not set → **read live
   by default**; persisted (in `kv_config`) only as a last-seen marker for
   cross-run change detection. The RetroDECK home path and save-sort settings
   markers live here.
3. **Synced / derived relational state with real invariants** — per-ROM groups,
   history, caches of remote data → **SQLite aggregates**.

`save_sync_state.json` is the *legacy* JSON home for bucket-3 save state and
for `device_id`; it is replaced by SQLite at the cutover. As of #822 it no
longer holds the save-sync toggles or `device_name` (those moved to
`settings.json`, settings schema v4) — only `device_id` remains until the
cutover folds it into `kv_config`.

### Cutover

The hard cut from JSON state files to SQLite ([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)).
"Hard" means **SQLite starts empty** — the JSON state is not migrated into it,
and the JSON-era domain classes are deleted in the same wave. Bucket-1 config
(`settings.json`) and the change-detection markers are unaffected by the
cutover; only the relational save/library/playtime state is re-derived from
scratch (re-synced from RomM, re-pruned against disk). The JSON→JSON moves that
*precede* the cutover (e.g. #822) are explicitly **not** cutover work — they
ship independently because they touch no SQLite.

### kv_config

A key-value table for small singleton configuration values that don't justify
their own aggregate or table. One row per key.

Intended residents (per [ADR-0003](docs/adr/0003-json-sqlite-persistence-boundary.md);
the table is created-but-unused until the cutover): the RetroDECK home path
marker (`retrodeck_home_path` + its pending-migration `_previous`), the
save-sort settings markers (`save_sort_settings` + `_previous`), and `device_id`
(server-issued identity, folded in at the cutover). The schema version is
**not** a `kv_config` key — it lives in `PRAGMA user_version`.

**Not** a dumping ground: anything with its own lifecycle, invariants, or
repeat-row potential gets its own aggregate. `kv_config` is for the truly small,
the truly singleton, and the truly miscellaneous.

### Rom (aggregate) vs ROM (file) vs RomM (server)

Three things spell similarly; distinct meanings:

- **Rom** — the aggregate / domain entity owned by this plugin
  (`domain/rom.py`). Represents one ROM as the plugin tracks it locally:
  identity, the denormalized `platform_slug`, sync metadata, the Steam shortcut
  binding.
- **ROM** (or "ROM file") — the actual playable game file on disk (e.g.
  `.iso`, `.cue`, `.gba`). What `RomInstall` records once a `Rom` has been
  downloaded.
- **RomM** — the upstream self-hosted server. The source of truth this plugin
  syncs *from*.

Convention: always write `Rom` (PascalCase) when referring to the aggregate.
Write "ROM file" when referring to the on-disk artifact.

### platform_slug (denormalized)

The RomM platform identifier (e.g. `gba`, `psx`) carried as a plain string on
the rows that need it (`roms`, `rom_installs`, `downloaded_bios`,
`firmware_cache`). There is **no local `Platform` aggregate** — that was
proposed in ADR-0001 and **dropped by [ADR-0003](docs/adr/0003-json-sqlite-persistence-boundary.md)**
on YAGNI grounds. Platform display names resolve live from RomM; sync exclusion
is the user-intent `enabled_platforms` config in `settings.json`, not
per-platform local state. A `Platform` aggregate is reintroduced only when a
concrete need lands (the standalone-emulator roadmap), not speculatively.

### Save-sync slot

A named channel for a ROM's saves (e.g. `default`). The active slot for a ROM
is recorded on its `RomSaveState`; `default_slot` (a `settings.json` config
value, per #822) is the slot a newly-tracked ROM starts on. Slots let the same
ROM carry distinct save sets without clobbering one another. Confirming a slot
(`confirm_slot(...)`) is an explicit user/flow decision — the plugin never
silently adopts a foreign slot.

### Baseline

The last-synced reference point recorded for a tracked save file — captured in
a `FileSyncState` value object (filename + hash + `tracked_save_id`). The
newest-wins matrix diffs the current local and remote state against the baseline
to detect drift on the next sync. Adopting a baseline (`adopt_baseline(...)`) is
how a file becomes tracked.

### Newest-wins matrix

The save-sync conflict-resolution model (`services/saves/sync_engine/`). For
each tracked file it evaluates local vs remote vs **baseline** and resolves to
whichever side is newer, rather than blindly mirroring one direction. The
"matrix" is the per-file evaluation table that drives the actual upload /
download / no-op dispatch for a ROM's sync run.
