---
name: cosmic-python
description: Architecture rules enforced for backend (services / adapters / domain / lib / models). What's forbidden in services, what belongs in domain, how to spot regressions. Source of truth is CLAUDE.md "Architecture — Cosmic Python rules" section; this memory exists so the rules auto-inject when CLAUDE.md isn't loaded.
type: domain
---

# Cosmic Python rules — decky-romm-sync backend

Backend layout: `services/` (orchestration) / `adapters/` (I/O) / `domain/` (pure compute) / `lib/` (cross-cutting
utilities) / `models/` (data shapes). `import-linter` enforces direction.

## Services — what's forbidden

- Concrete adapter imports. Services depend on Protocols defined in `services/protocols.py`, never on adapter classes
  directly.
- Raw I/O: `os.*` (except pure path algebra: `realpath`, `relpath`, `join`, `splitext`, `basename`, `dirname`),
  `open(...)`, `pathlib.Path(...).read_*` / `write_*`, `fcntl.*`, `urllib.*`, `shutil.*`, `subprocess.*`,
  `hashlib.<x>(open(...))`.
- Hidden I/O: `time.time()`, `time.monotonic()`, `datetime.now()`, `uuid.uuid4()`, `asyncio.sleep()`, `random.*`
  directly. Inject `Clock` / `UuidGen` / `Sleeper` Protocols (see #294).
- Service-to-service concrete imports. Services are independent. Cross-service dependencies are Protocol-typed.
- Module-function imports from `domain/`. If tests need `patch("services.X.module_name.fn")`, that's the smell — wrap
  the module behind a Protocol and inject it (see #296 for `CoreInfoProvider` precedent).

## God-class signal

Services > ~600 LOC or `__init__` > 6 params (S107) — decompose into sub-services with constructor injection. Reference
pattern: `services/saves/` (`SaveService` façade + `StateService` + `SyncEngine` + `StatusService` + `SlotsService`).

## Adapters

Own all I/O. Never import from `services/`. Implement Protocols defined in `services/protocols.py`.

## Domain

Pure compute only. No I/O, no state mutation, no service or adapter imports. Functions take inputs, return outputs.

If a function in a service has no `self._<adapter>.*` calls and no state mutation, it belongs in `domain/`.

## Bootstrap

`bootstrap.py` is the only place where concrete adapters meet services. `WiringConfig` holds the wiring; protocols come
in, services come out.

## Smell → fix mapping (quick reference)

| Smell                                                                      | Fix                                                                                                                                                   |
| -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `import os` + `open(...)` in `services/X.py`                               | Extract to adapter, inject Protocol                                                                                                                   |
| `time.time()` / `datetime.now()` in service                                | Inject `Clock` (#294)                                                                                                                                 |
| `uuid.uuid4()` in service                                                  | Inject `UuidGen` (#294)                                                                                                                               |
| `asyncio.sleep(...)` in service                                            | Inject `Sleeper` (#294)                                                                                                                               |
| `Callable[..., None]` ctor param                                           | Replace with named Protocol (precedent: `StatePersister` at `services/protocols.py:288`)                                                              |
| `from domain import es_de_config` then calling its functions               | Wrap behind `CoreInfoProvider` Protocol (#296)                                                                                                        |
| Pure helper inside a service (no `self.*` access)                          | Move to `domain/` (#295 lists candidates)                                                                                                             |
| `tests/services/test_X.py` does `patch("services.X.os.path...")`           | Service has un-injected I/O; extract to adapter                                                                                                       |
| `ignore_imports = …` carve-out in `.importlinter` to allow a banned module | Redesign the Protocol so the import isn't needed (e.g. `UuidGen.uuid4() -> str`, not `-> uuid.UUID` — #294). Suppressions are the smell, not the fix. |

## Refactor wave plan

Tracked under #277. Wave 1 (#256 cross-cutting infrastructure) → Wave 2 (#295 domain promotions) → Wave 3 (#297-#302
per-service verticals, smallest-to-largest, LibraryService #300 last) → Wave 4 (#274 main.py + #277 final verification).
Saves vertical (#254) runs in parallel.

Full sequencing rationale lives in `CLAUDE.md` "Refactor wave plan" section.
