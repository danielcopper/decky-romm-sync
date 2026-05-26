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
scope. Triggers for revisiting that scope are documented in `CLAUDE.md`.

### kv_config

A key-value table for small singleton configuration values that don't justify
their own aggregate or table. One row per key.

Used for misc state like the schema version, the RetroDECK home path snapshot,
the in-progress save-sort settings change. **Not** a dumping ground — anything
with its own lifecycle, invariants, or repeat-row potential gets its own
aggregate. `kv_config` is for the truly small, the truly singleton, and the
truly miscellaneous.

### Rom (aggregate) vs ROM (file) vs RomM (server)

Three things spell similarly; distinct meanings:

- **Rom** — the aggregate / domain entity owned by this plugin
  (`domain/rom.py`). Represents one ROM as the plugin tracks it locally:
  identity, sync metadata, the Steam shortcut binding.
- **ROM** (or "ROM file") — the actual playable game file on disk (e.g.
  `.iso`, `.cue`, `.gba`). What `RomInstall` records once a `Rom` has been
  downloaded.
- **RomM** — the upstream self-hosted server. The source of truth this plugin
  syncs *from*.

Convention: always write `Rom` (PascalCase) when referring to the aggregate.
Write "ROM file" when referring to the on-disk artifact.
