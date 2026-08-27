---
paths:
  - "py_modules/services/**"
---

# Services — Cosmic Python rules

Cosmic Python ("Architecture Patterns with Python", Percival & Gregory) is our north star, adapted for a single-user
Decky plugin domain. `[CP]` marks a canonical Cosmic Python rule — a hard rule, breaking it is an architectural
regression. `[ours]` marks a project convention layered on top — flag deviations in review, but the rule itself can be
debated.

Backend layout: `services/` (orchestration) / `adapters/` (I/O) / `domain/` (pure compute) / `lib/` (cross-cutting
utilities) / `models/` (data shapes). `import-linter` enforces direction. `[CP]`

## Rules

- `[CP]` Depend on Protocols (defined in `services/protocols/`, imported as `from services.protocols import X`), never
  on concrete adapter classes. Carve-out: sub-services within one bounded context (e.g. all of `services/saves/`) may
  hold concrete peer-service refs in their `*ServiceConfig` when they share an aggregate. `[ours]` A method a peer calls
  is part of that peer's **public** surface — no leading underscore.
- `[CP]` No raw I/O. `[ours]` Forbidden in `services/`: `os.*` (except pure path algebra: `relpath`, `join`, `splitext`,
  `basename`, `dirname`), `open(...)`, `pathlib.Path(...).read_*` / `write_*`, `fcntl.*`, `urllib.*`, `shutil.*`,
  `subprocess.*`, `hashlib.<x>(open(...))`.
- `[CP]` No clocks or randomness — inject `Clock` / `UuidGen` / `Sleeper`. `time.time()` / `time.monotonic()` /
  `datetime.now()` / `uuid.uuid4()` / `asyncio.sleep()` / `random.*` are banned at the call site.
- `[CP]` No service-to-service concrete imports — services are independent; cross-service deps are Protocol-typed.
- `[ours]` Module functions from `domain/` are still a coupling — if tests need `patch("services.X.module_name.fn")`,
  wrap the module behind a Protocol and inject it.
- `[ours]` **Constructor shape: every service takes a single `config: XxxServiceConfig` keyword argument.** Frozen
  dataclass. Outer services keep the `Service` token in both names (`SteamGridService` + `SteamGridServiceConfig`);
  sub-services may use role-based names (`SyncEngine` + `SyncEngineConfig`). All deps live in the config: Protocol-typed
  adapters, infrastructure (loop, logger, clock, uuid_gen, sleeper), persistence callbacks, settings-derived values. No
  bare-param ctors, no mixed ctors.
- `[ours]` **Debug logging: inject the `DebugLogger` Protocol.** No per-service `_log_debug` that re-reads settings at
  call time; no `decky.logger.info` to bypass log-level filtering.
- `[ours]` God-class signal: services > ~1000 LOC — decompose into sub-services with constructor injection
  (`services/saves/` is the reference). Enforced by `scripts/check_module_size.py`: a new module may not cross the
  threshold at all, and the modules that predate the gate are pinned at their exact size and may not grow. A pin goes up
  only for a change that adds no code — a rename, a reformat — with the reason recorded at its `ALLOWLIST` entry.

## Reference shape for new service-level work

A Protocol (in `services/protocols/`) + an adapter implementing it + a `FakeXxxAdapter` in `conftest` + `*ServiceConfig`
ctor decomposition. `services/saves/` and `services/library/` are the reference decompositions for shared-state
sub-services. Sequencing for a new vertical: cross-cutting Protocols first, domain extraction next, the biggest service
last.

If a refactor breaks a `[CP]` rule, that's an architectural regression — call it out and fix it in the same PR or open a
follow-up. `[ours]` deviations should be flagged in review but can be debated.
