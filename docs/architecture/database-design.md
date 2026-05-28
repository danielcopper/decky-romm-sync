# Database Design

## Overview

This page is the canonical home for the **aggregate domain model** behind the SQLite persistence migration (epic [#271](https://github.com/danielcopper/decky-romm-sync/issues/271)). The migration replaces the current JSON state files with a SQLite database whose tables back a set of Cosmic Python aggregates.

The migration is phased. As of the first PR of [#788](https://github.com/danielcopper/decky-romm-sync/issues/788), the **enforcement infrastructure** documented below is in place — the decorator, the linters, and the type-check rule that keep aggregates honest. The **aggregate set itself** (the 11 aggregate roots, their fields, and their mutation methods) lands in subsequent PRs of #788, and the SQLite schema and per-aggregate Repository Protocols land in the downstream sub-issues. Those are not documented here yet; see [Coming in later PRs](#coming-in-later-prs).

In other words: the rails are laid, the trains haven't arrived. A reader who comes looking for the aggregate table will find it forthcoming, not missing by mistake.

## What an aggregate is here

An **aggregate** is a cluster of domain objects treated as a single unit for data consistency, with one root entity that owns all invariants and is the only external entry point. The full definition — root, identity, transaction boundary, by-id references, mutation-via-methods — lives in the [`Aggregate` glossary entry in `CONTEXT.md`](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md). This page uses that vocabulary; it does not re-derive the Cosmic Python theory.

Aggregate boundaries are **invariant boundaries, not storage boundaries** — one aggregate may be backed by several tables, and table layout is a downstream decision. The first concrete aggregate-boundary decision, adopting `Platform` as a full aggregate rather than a denormalized string, is recorded in [ADR-0001](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0001-adopt-platform-aggregate.md).

## Standards shared across all aggregates

Every aggregate in this codebase follows the same rules, so the enforcement layers below can be uniform:

- **Declared via the `@cosmic_aggregate` decorator.** This is the canonical form — not a transitional flag. The decorator marks the class as an aggregate root and is the marker the field-assignment check looks for.
- **Mutation only via verb-named methods on the root.** No external field assignment (`aggregate.field = value`) from services. Methods are named after the domain event that occurred (`adopt_baseline(...)`, `mark_installed(...)`, `confirm_slot(...)`) — per-field verbs even when slightly forced (`set_autocleanup_limit(10)`), consistency over expressiveness. Field access for reads is fine.
- **Cross-aggregate references by id only** — never by holding a Python reference to another aggregate's internals. `RomInstall` carries `rom_id: int`, not `rom: Rom`.
- **No `extra: dict[str, Any]` forward-compat hedge.** Schema migrations carry the model forward; aggregates do not hold an open-ended JSON dict against future change.

## CP enforcement layers

Four mechanisms keep the aggregate rules from drifting. They are layered — each catches a different class of violation, and together they make "mutate an aggregate's fields from a service" fail before it merges.

| Layer | Mechanism | What it catches |
| --- | --- | --- |
| 1 | `@cosmic_aggregate` decorator | Declares the root; gives it `__slots__` so unknown fields can't be set |
| 2 | AST field-assignment check | `aggregate.field = value` in `services/` |
| 3 | import-linter domain contracts | Non-stdlib / non-self imports into `domain/` |
| 4 | basedpyright `reportPrivateUsage` | Access to `_`-prefixed internals from production code |

### 1. The `@cosmic_aggregate` decorator

`py_modules/domain/_aggregate.py` defines the single canonical way to declare an aggregate root:

```python
from domain._aggregate import cosmic_aggregate

@cosmic_aggregate
class Rom:
    rom_id: int
    platform_slug: str
    # ...
```

The decorator applies `@dataclass(slots=True)`, so the root gets `__init__`, `__repr__`, `__eq__`, and `__slots__` for free. `__slots__` matters for enforcement: a slotted dataclass rejects assignment to any attribute not declared as a field, so typos and ad-hoc field additions fail at runtime, not silently. It is also the marker the AST check (layer 2) scans for — `@cosmic_aggregate` is how a class opts into the mutation-via-methods rule.

**Value Objects do not use this decorator.** Immutable members of an aggregate (e.g. `FileSyncState`, `BiosFileEntry`) use a plain `@dataclass(frozen=True, slots=True)` — they are immutable by construction and have no mutation surface to police, so they need neither the marker nor the verb-method discipline. The decorator is for roots only.

### 2. AST field-assignment check

`scripts/check_aggregate_field_assignment.py` is a small custom linter, wired into CI alongside the cosmic call bans. It enforces the **mutation-only-via-methods** rule that a type checker cannot express directly.

How it works:

1. It walks `py_modules/domain/`, parses every file, and collects the class names decorated with `@cosmic_aggregate`.
2. It walks `py_modules/services/` and flags every assignment whose target is `<receiver>.<field> = ...` where the receiver's variable name matches an aggregate class name (exact snake_case identifier match — variable `rom` matches aggregate `Rom`, `rom_state` does not). It skips `self.x = ...` (method-body internals) and subscript receivers (`d["k"].x = ...`).

The heuristic is conservative by design — a guardrail, not a prover. It can false-positive (a variable named `rom` holding something else) and false-negative (assignment through a complex expression). The escape hatch is a trailing comment on the offending line:

```python
rom.cover_path = path  # pragma: no aggregate-check
```

**It is a no-op until aggregates exist.** No class carries `@cosmic_aggregate` yet, so the aggregate-name set is empty and the check finds nothing. It activates automatically as the aggregate roots land in later PRs — the moment a `@cosmic_aggregate` class appears, any `aggregate.field = ...` in a service starts failing CI. Old JSON-era containers (e.g. `SaveSyncState`) don't carry the decorator, so they keep working until the cutover wave replaces them.

### 3. import-linter — domain is stdlib + self only

Two `.importlinter` contracts confine `domain/` to the standard library and itself:

```ini
# Domain must not import services, adapters, lib, or models
[importlinter:contract:domain-independence]
type = forbidden
source_modules =
    domain
forbidden_modules =
    services
    adapters
    lib
    models

# Domain must not import vendored third-party packages
[importlinter:contract:domain-stdlib-only]
type = forbidden
source_modules =
    domain
forbidden_modules =
    _vendor
```

Together these say: **domain = stdlib + self only.** `domain-independence` forbids every sibling first-party layer (note `lib` and `models` are now in the forbidden list — domain depends on no other internal layer); `domain-stdlib-only` forbids the `_vendor` namespace, which is the codebase's only entry point for non-stdlib runtime code. This is the CP doctrine that the domain model has no external runtime dependencies, mechanically enforced.

A consequence of `lib` being forbidden: anything domain needs from "shared utilities" lives inside `domain` itself. ISO-8601 timestamp parsing (`parse_iso` / `parse_iso_to_epoch`) moved from `lib/iso_time.py` to `domain/iso_time.py` for exactly this reason.

### 4. basedpyright `reportPrivateUsage = "error"`

`pyproject.toml` sets `reportPrivateUsage = "error"`, so accessing a `_`-prefixed name from outside its owning class is a hard type error, not a convention nobody enforces. This makes the underscore-prefix convention real: production code cannot reach into an aggregate's (or any class's) internals.

Tests are exempt via an execution-environment override:

```toml
[[tool.basedpyright.executionEnvironments]]
root = "tests"
extraPaths = ["py_modules"]
reportPrivateUsage = "none"
```

White-box testing — inspecting and rebinding the private state of the system under test — is a deliberate, accepted pattern here. The guardrail targets production encapsulation, not test setup.

One corollary worth stating: a method that one sub-service calls on a peer is part of that peer's **public** surface and carries **no** leading underscore. The `_` prefix is reserved for genuinely class-internal helpers, which keeps `reportPrivateUsage` coherent with the saves-style peer-injection carve-out — peers call public methods, never private ones.

## Coming in later PRs

As #788's phased PRs land, this page grows to document:

- **The 11-aggregate set** — `Rom`, `Platform`, `RomInstall`, `RomMetadata`, `RomSaveState`, `Playtime`, `Device`, `SyncSettings`, `BiosFile`, `FirmwareCacheEntry`, `SyncRun` — each with its carried fields, identity, and the rationale for its boundary.
- **Per-aggregate Repository Protocols** — one Repository per aggregate root (not per table), defined downstream in [#782](https://github.com/danielcopper/decky-romm-sync/issues/782).
- **The SQLite schema** — table layouts that back the aggregates (one aggregate may span several tables), designed in [#780](https://github.com/danielcopper/decky-romm-sync/issues/780).

Chapter 8+ of the Cosmic Python book (domain events + message bus) is explicitly out of scope for this epic; the triggers for revisiting that scope are recorded in `CLAUDE.md`.

## See also

- [Backend Architecture](backend-architecture.md) — the four-layer split, the `XxxServiceConfig` pattern, and the boundary-enforcement layers that aggregates build on.
- [`CONTEXT.md`](https://github.com/danielcopper/decky-romm-sync/blob/main/CONTEXT.md) — the `Aggregate`, `kv_config`, and `Rom`/`ROM`/`RomM` glossary entries.
- [ADR-0001](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0001-adopt-platform-aggregate.md) — the decision to adopt `Platform` as a full aggregate.
