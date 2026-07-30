---
paths:
  - "py_modules/adapters/**"
  - "py_modules/domain/**"
  - "py_modules/lib/**"
  - "py_modules/models/**"
---

# Adapters, domain, and aggregates

`[CP]` marks a canonical Cosmic Python rule — a hard rule, breaking it is an architectural regression. `[ours]` marks a
project convention layered on top — flag deviations in review, but the rule itself can be debated.

Backend layout: `services/` (orchestration) / `adapters/` (I/O) / `domain/` (pure compute) / `lib/` (cross-cutting
utilities) / `models/` (data shapes). `import-linter` enforces direction. `[CP]`

**Adapters**: `[CP]` Own all I/O. Never import from `services/`. Implement Protocols defined in `services/protocols/`.

**Domain**: `[CP]` Pure compute only. No I/O, no state mutation, no service or adapter imports. Anything stateless and
I/O-free currently in a service belongs here.

## Aggregates

The aggregate roots, their tables, and the enforcement layers are canonical in
[database-design.md](../../docs/architecture/database-design.md). Config-shaped toggles live in `settings.json`, not
SQLite. Rules for the relational state that _does_ live in SQLite:

- `[CP]` One Repository Protocol per aggregate root, not per table.
- `[CP]` Aggregate methods are the **only** mutation API for the aggregate's state. No external field assignment from
  services. Field access for reads is fine.
- `[ours]` **Mutation methods are verb-named after the domain event they represent.** `adopt_baseline(filename, hash)`
  not `update_baseline(...)`; `mark_installed(path)` not `set_installed(...)`; `promote_slot(slot, source)` not
  `update_slot_source(...)`. The method name becomes the implicit event name if events are added later.
- `[ours]` Domain events + message bus are out of scope. Revisit when handler diversity ≥3 kinds for the same state
  change, or a non-Steam consumer becomes concrete, or telemetry needs to subscribe.
