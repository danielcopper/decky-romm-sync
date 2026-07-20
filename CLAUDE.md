# decky-romm-sync — Decky Loader Plugin

## What This Is

A Decky Loader plugin that syncs a self-hosted RomM library into Steam as Non-Steam shortcuts. Games launch via
RetroDECK. The QAM panel handles settings, sync, downloads, and BIOS management.

## What belongs in this file

Three things, and nothing else:

1. **Traps** — where an agent would confidently do the wrong thing and have no reason to go look first.
2. **Cross-cutting invariants** — rules that span files, so no single diff shows the whole rule.
3. **Workflow** — how we work here; not derivable from the code.

Everything else is topic depth and lives in `docs/`. A rule with a mechanical check needs only its one-line statement
here (CI carries the enforcement); a rule without one needs its full statement here, because nothing else will catch it.

## Where the details live

Each page below is the current-truth owner of its area, and most carry their own ADR trail. Read the page before working
in the area. **Do not cite ADRs from this file** — an ADR is frozen history (and may be `Proposed` or superseded, which
is invisible at the citation site), so reach it through the page that owns the topic.

- Steam shortcuts — appIds, artwork, launch-option writes, removal churn —
  [steam-non-steam-shortcuts.md](docs/architecture/steam-non-steam-shortcuts.md)
- Save-file sync — slots, conflict resolution, negotiate transport, version history —
  [save-file-sync-architecture.md](docs/architecture/save-file-sync-architecture.md)
- Save-sync coverage matrix — [save-sync-coverage.md](docs/architecture/save-sync-coverage.md)
- Services, adapters, wiring; connection/token and settings-persistence internals —
  [backend-architecture.md](docs/architecture/backend-architecture.md)
- SQLite schema, aggregate roots, migrations — [database-design.md](docs/architecture/database-design.md)
- Emulator and core selection — [core-emulator-selection.md](docs/architecture/core-emulator-selection.md)
- RetroArch/ES-DE config parsing — [config-source-parsers.md](docs/architecture/config-source-parsers.md)
- Steam Remote Play — [steam-remote-play.md](docs/architecture/steam-remote-play.md)
- **End-user-facing behavior and UI** — setup, configuration, syncing, save-sync, BIOS, troubleshooting —
  `docs/user-guide/`
- Dev setup, dependency management, frontend loop — `docs/contributing/`

## Documentation

**Docs are updated in the same PR as the code change. This is not optional.** When a change affects architecture, data
flows, feature behavior, or user-facing UI, the relevant page under `docs/` must be updated in the same PR.
Documentation-debt-as-a-separate-follow-up-issue is forbidden — those follow-ups never land. If you're not sure whether
a change needs docs, the default is "yes, it does." Enforced in CI by `.github/workflows/docs-check.yml`.

For genuinely doc-irrelevant PRs (pure refactor with no user-visible change, no architecture shift, no new flow;
tooling/CI changes; dependency bumps), set the `no-docs-change` label on the PR OR include `docs: N/A` (with a one-line
reason) in the PR description. Opting out is an explicit acknowledgement, not a silent omission.

Docs are Material for MkDocs, published to GitHub Pages by `.github/workflows/docs.yml` on push to `main`. Preview
locally with `mise run docs`.

## Traps — non-obvious rules that bite silently

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart;
  SteamClient API is instant.
- **AddShortcut ignores most params**: `AddShortcut(name, exe, startDir, launchOptions)` ignores startDir and
  launchOptions. Must use `Set*` calls (`SetShortcutName`, `SetShortcutExe`, `SetShortcutStartDir`,
  `SetAppLaunchOptions`) once the new app's overview is registered. Do NOT pass quoted exe paths — the API quotes
  internally.
- **AddShortcut timing**: After `AddShortcut()`, wait for the new app's overview before setting properties — poll
  `appStore.GetAppOverviewByAppID(appId)` (`waitForAppOverview`), never a blind fixed delay. Use 50ms between operations
  in the apply loop.
- **Shortcut appId is assigned, not derived**: Steam assigns it at creation and it is stable for the shortcut's
  lifetime; the plugin records it in `roms.shortcut_app_id` and detects ownership by the exe path. Never re-derive it
  (the `CRC32(exe + appName)` formula is disproven). `launchOptions`/`startDir` changes are appId-safe; **exe/name**
  changes require delete + recreate.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT
  `ServerAPI.callPluginMethod()`).
- **Decky callables must be async**: Even if the body is synchronous, Decky's callable framework requires `async def`.
  Do not remove `async` from callable methods in `main.py`.
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode).
  Paginated: `{"items": [...], "total": N}`. List calls page via `lib/romm_paging.py` and append
  `&with_char_index=false&with_filter_values=false` to skip aggregations the server otherwise computes on every request.
- **RomM minimum version**: Requires RomM >= 4.9.0, hard-rejected in `test_connection()` (`_MIN_REQUIRED_VERSION` in
  `main.py`) — the plugin is inert until the server is updated.
- **User-Agent on outgoing HTTP**: SteamGridDB **and** RomM behind Cloudflare Tunnel reject the default `Python-urllib`
  UA with 403. Every HTTP-talking adapter takes a `user_agent: str` ctor param; bootstrap threads
  `decky-romm-sync/<version>` from `package.json` — no hardcoded version strings.
- **Large payloads**: Never send bulk base64 through `decky.emit()` — the WebSocket bridge has size limits. Use per-item
  callables, and chunk bulk lists (the library apply emits shortcuts in batches; the metadata cache loads page-by-page).
- **No `BIsModOrShortcut` bypass**: the bypass counter was removed deliberately. Shortcuts return `true` (natural
  state); we own the game detail UI. Do not reintroduce a bypass.

## Current State

Latest release and shipped features: see `git tag --sort=-v:refname` and GitHub Releases. Roadmap and open work:
[GitHub Projects board](https://github.com/users/danielcopper/projects/2).

## Development

- **Build**: `pnpm build` (Rollup -> dist/index.js)
- **Tests**: `python -m pytest tests/ -q` or `mise run test`
- **Coverage**: `python -m pytest tests/ -q --cov=py_modules --cov=main --cov-report=term --cov-branch`
- **Gate**: `mise run gate` (the full CI battery in one command — mirrors every PR check; slow. Run before pushing.)
- **Setup**: `mise run setup` (installs JS + Python dependencies)
- **Dev reload**: `mise run dev [display]` (build + restart plugin_loader; a display like `dp4` / `internal` also opens
  windowed BPM on it after the deploy)
- **Frontend live dev**: `mise run dev:watch [display]` (one-time `mise run dev:setup`) — hot-reloads the **frontend**
  into windowed Big Picture on every save, no loader restart. **Backend** changes need `mise run dev:push-backend`. Lost
  the Decky UI after leaving BPM: `mise run dev:bpm-reset [display]`. Guide: `docs/contributing/frontend-dev-loop.md`
- **Tooling**: mise manages node, pnpm, python, uv; venv auto-creates at `.venv`. Python deps are pinned in
  `requirements-*.lock`, compiled from `requirements-*.txt` by `uv pip compile`; regenerate with `mise run lock-update`
  after editing a source or bumping a pin.
- **Pre-commit hook** (`.githooks/pre-commit`): formats staged files — `ruff format` + `ruff check` (Python),
  `prettier --write` (TS/TSX), `deno fmt` (Markdown). Stays fast (<2s); heavy validation is CI-only. Do not re-introduce
  heavy checks here.

## Code Quality

CI runs SonarCloud (Quality Gate: 80% coverage on new code, 0 bugs, 0 vulnerabilities), Ruff, basedpyright,
import-linter, pytest-cov branch coverage, and the repo's `scripts/check_*` gates. **The per-rule checks and what each
one enforces are listed in the invariant register below** — that table is the single inventory; do not duplicate it
here.

## Invariant register — cross-cutting safety rules

The audit's clearest pattern: every rule with a mechanical check held; every rule that lived in prose or in a reviewer's
head drifted. This register is the single inventory of the cross-cutting safety rules — the ones that span files, so no
diff-scoped review (human or agent) sees the whole rule — plus the current enforcement tier of each. It is a **map of
the enforcement surface, not the enforcement itself**: a `check`/`test` rule is enforced by the named artifact; a
`prompt-only` rule is not yet mechanized and is injected here so review carries it verbatim until a check exists. The
moment a `prompt-only` rule gets a mechanical check it moves to the `check` tier — a rule is never weakened to stay
green, and a real drift is a finding to triage, never an exemption. `[ours]`

Format: **invariant** — tier — enforced by.

- **Callable failures use `{success, reason, message}` (never `error` / `error_code`)** — check —
  `scripts/check_failure_shape.py --check`
- **Frontend↔backend callable parity (names + arity)** — check — `scripts/check_callable_manifest.py`
- **Every backend `emit` event name has a frontend listener, and vice versa** — check — `scripts/check_event_parity.py`
- **`settings.json` is written only by its owner (`adapters/persistence.py`)** — check —
  `scripts/check_settings_owner.py`
- **Sync run-lifecycle (`sync_state` / `current_sync_id`) written only via `LibrarySyncStateBox` verbs** — check —
  `scripts/check_sync_lifecycle_owner.py`
- **Aggregate state mutated only via verb-named methods (no field assignment)** — check —
  `scripts/check_aggregate_field_assignment.py`
- **No UoW-opening seam (ActiveCoreResolver, RelaunchOptionsResolver, uow_factory) is called while a UoW is open on the
  same path** — check — `scripts/check_uow_seam_nesting.py`
- **Services never call clocks / sleep / uuid / random directly (inject the Protocol)** — check —
  `scripts/check_cosmic_call_bans.sh`
- **Service-independence contract list stays complete** — check — `scripts/check_service_independence_contract.py`
- **Layer import direction (services ↛ adapters, adapters ↛ services, …)** — check — `.importlinter` (`lint-imports`)
- **No bare `# type: ignore` / blanket suppressions** — check — `scripts/check_no_bare_ignores.sh`
- **Every pinned version in `requirements-*.lock` satisfies its `requirements-*.txt` source constraint** — check —
  `scripts/check_lock_sync.py`
- **Server-supplied path components pass `safe_join` (`lib/path_safety.py`)** — test + prompt-only — traversal tests per
  path builder; new call sites are prompt-only
- **No sentinel objects on the wire — explicit JSON-representable tagged values only** — prompt-only — mechanize with
  #1032 (after tagged values replace the sentinels)
- **Every destructive op has backup-or-confirm; never delete data that exists nowhere else** — prompt-only — save-file
  removals route through `MatrixExecutor.quarantine_local_file` (the `.romm-backup` funnel — #965/#1058 done); mechanize
  the rest via the #794 delete-path fixes (#974 / #1005 / #1062)
- **Every read-mutate-write of a `RomSaveSyncState` runs under `SyncEngine.rom_lock(rom_id)`** — prompt-only — sync
  paths + `get_save_status` + the four slot mutations hold the lock (#1057); mechanize via a `rom_save_sync_states.save`
  call-site audit
- **Per-slot server reads/deletes go through `domain/save_slot.py` (legacy omits `&slot=`, client-filters)** —
  prompt-only — `get_slot_saves` / `get_slot_delete_info` / `delete_slot` / `list_file_versions` / `rollback_to_version`
  use `slot_query_param` + `save_in_slot` (#1061); RomM can't address `slot:null` via the param, so legacy MUST omit
  it + filter client-side (legacy delete refused up-front, #1478)
- **Every save-sync decision comes from `compute_sync_action` (via `list_saves`), never the `negotiate` op list; every
  automatic upload POSTs `overwrite=false` (409-backstopped); `overwrite=true` only from an explicit `keep_local`** —
  test + prompt-only — domain property tests (`resolve_upload_conflict`, row-11 split, Inv7/Inv8) + contract 409 tests
  (`tests/contract/`); new upload/dispatch call sites are prompt-only (#1276)
- **`applied_launch_options` is written only by the five recorded-state writer sites (sync ack-commit,
  download-complete, uninstall, home-migration, version-switch), each recording the exact command the frontend wrote;
  excluded from the sync UPSERT; the only sanctioned reset is Force Full Sync's clear-to-NULL (a wrong recorded value is
  the only path to a wrong delta-skip)** — test + prompt-only — each writer site carries a value-exact test; new
  launch-options write paths are prompt-only — mechanize via a `set_applied_launch_options` /
  `record_applied_launch_options` call-site audit
- **An abandoned-chunk stash's whole-unit apply staging (`pending_sync` / `pending_all_roms` / `pending_cover_sources`)
  is never mutated while the stash is pending (box IDLE) — every run-entry path passes `try_begin_run`, which clears the
  stash before any staging write (#1367)** — prompt-only — verified closed in #1367 review; mechanize via a
  staging-writer call-site audit

When a change applies a guard / sanitize / backup / grouping pattern, sweep for sibling sites of the same pattern — the
register is what that sweep checks against.

## Architecture — Cosmic Python rules

Cosmic Python ("Architecture Patterns with Python", Percival & Gregory) is our north star, adapted for a single-user
Decky plugin domain. Each rule carries a tag:

- `[CP]` — Canonical Cosmic Python. Hard rule. Breaking it is an architectural regression.
- `[ours]` — Project convention layered on top. Flag deviations in review, but the rule itself can be debated.

Backend layout: `services/` (orchestration) / `adapters/` (I/O) / `domain/` (pure compute) / `lib/` (cross-cutting
utilities) / `models/` (data shapes). `import-linter` enforces direction. `[CP]`

**Services**:

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
- `[ours]` God-class signal: services > ~700 LOC — decompose into sub-services with constructor injection
  (`services/saves/` is the reference).

**Adapters**: `[CP]` Own all I/O. Never import from `services/`. Implement Protocols defined in `services/protocols/`.

**Domain**: `[CP]` Pure compute only. No I/O, no state mutation, no service or adapter imports. Anything stateless and
I/O-free currently in a service belongs here.

**Aggregates**: the aggregate roots, their tables, and the enforcement layers are canonical in
[database-design.md](docs/architecture/database-design.md). Config-shaped toggles live in `settings.json`, not SQLite.
Rules for the relational state that _does_ live in SQLite:

- `[CP]` One Repository Protocol per aggregate root, not per table.
- `[CP]` Aggregate methods are the **only** mutation API for the aggregate's state. No external field assignment from
  services. Field access for reads is fine.
- `[ours]` **Mutation methods are verb-named after the domain event they represent.** `adopt_baseline(filename, hash)`
  not `update_baseline(...)`; `mark_installed(path)` not `set_installed(...)`; `promote_slot(slot, source)` not
  `update_slot_source(...)`. The method name becomes the implicit event name if events are added later.
- `[ours]` Domain events + message bus are out of scope. Revisit when handler diversity ≥3 kinds for the same state
  change, or a non-Steam consumer becomes concrete, or telemetry needs to subscribe.

**Bootstrap (`bootstrap.py`)**: `[CP]` The composition root — the only place concrete adapters meet services. `[ours]`
`WiringConfig` holds the wiring; protocols in, services out. Adapter instantiation never happens in `main.py` — a
Protocol-wrapped persister is built in `bootstrap()` and passed through `CallbackBundle`.

**Vendored deps (`_vendor/`)**: `[ours]` Third-party runtime deps are vendored under `py_modules/_vendor/<package>/`
(Decky has no plugin-level package manager) and imported as `from _vendor import <package>`. Only adapters import
`_vendor.*`; services/domain/lib stay third-party-free (`domain-stdlib-only` contract in `.importlinter`). `_vendor/` is
excluded from ruff, basedpyright, and Sonar. Every vendored package ships its upstream `LICENSE` and a provenance entry
in [`_vendor/README.md`](py_modules/_vendor/README.md). Compiled binaries (no source in this repo) are vendored under
`py_modules/native/` instead (inside one of the fixed directories the Decky CLI packs into the plugin zip) — downloaded
verbatim from an upstream release with a pinned SHA-256 (CI re-verifies it; the release smoke test asserts the artifact
ships in the zip), loaded by an adapter via `ctypes` with no Python fallback; provenance and the update procedure live
in [`native/README.md`](py_modules/native/README.md).

**Process boundaries — `main.py` vs `bootstrap.py`**: `[ours]` `main.py` owns the Decky lifecycle (`_main`, `_unload`)
and the callable surface (one `async def` per `@callable`). `bootstrap.py` owns adapter instantiation and service
wiring. The split is binding — no callables in `bootstrap.py`, no service wiring in `main.py`. Both files grow with the
surface they describe; that is unavoidable density, not god-class. Split `bootstrap.py` into
`bootstrap/{adapters,services}.py` only past ~700 LOC.

**Reference shape for new service-level work**: a Protocol (in `services/protocols/`) + an adapter implementing it + a
`FakeXxxAdapter` in `conftest` + `*ServiceConfig` ctor decomposition. `services/saves/` and `services/library/` are the
reference decompositions for shared-state sub-services. Sequencing for a new vertical: cross-cutting Protocols first,
domain extraction next, the biggest service last.

If a refactor breaks a `[CP]` rule, that's an architectural regression — call it out and fix it in the same PR or open a
follow-up. `[ours]` deviations should be flagged in review but can be debated.

## Protocol naming — suffix by shape `[ours]`

- `…Reader` — object-shaped Protocols with multiple methods (`RetroArchConfigReader`).
- `…Provider` / `…Fn` — call-shaped (`__call__`-only) Protocols (`RetroArchSaveSortingProvider`, `CoreNameProviderFn`).
- `…Store` — file-store Protocols (`CoverArtFileStore`).
- `…Cache` — cache Protocols (`SgdbArtworkCache`).
- `…Persister` — persistence Protocols (`SettingsPersister`).
- Bare names — pervasive cross-cutting primitives (`Clock`, `Sleeper`, `UuidGen`, `DebugLogger`).

A sibling set that mixes suffixes reflects a shape difference, not an inconsistency.

## Async/sync method naming `[ours]`

- Async methods carry the bare domain-verb name — no `_async` / `Async` suffix.
- A **synchronous twin** (typically a lock-free worker run via `run_in_executor` that a peer calls directly to avoid
  re-entering a lock) is named `do_<verb>` if public/peer-called, `_<verb>_io` if private/internal-only. The async
  public method keeps the bare verb.

The two idioms coexist by access level; unification is tracked in #813.

## Callable response shapes `[ours]`

Callables returning a plain `dict` that can fail use `{success: False, reason: ErrorCode | str, message: str}`. Both
`reason` and `message` are **required**. Reuse `lib.list_result.ErrorCode` for coarse categories; bespoke guards
(`config_error`, `sync_disabled`, `not_installed`, …) stay plain-string reasons. Transport failures collapse onto
`SERVER_UNREACHABLE`; 401 and 403 collapse onto `AUTH_FAILED` (same slug, distinct `message`). The legacy `error_code`
key and a second `error` key are **forbidden**. Enforced by `scripts/check_failure_shape.py --check`.

Two carve-outs (pattern-exempt in the gate):

- **Discriminated-status unions** (`status: "ok" | "server_unreachable" | …`, used by the saves version-history
  callables) keep the `status` discriminant instead of `success` — more than two outcomes. Failure branches still carry
  `message: str`.
- **Partial-success responses** returning a full payload alongside a failure flag (`get_save_status`'s
  `server_query_failed: bool`, `get_save_setup_info`'s `recommended_action`) keep the additive flag.

Full convention paragraph: the `lib/list_result.py` module docstring.

## Subfolder layout `[ours]`

Layer top-level folders are flat by default — one file per concept. A subfolder is justified **only when the modules
within share an internal type, helper, or state**, not when they share a brand-name prefix. `adapters/romm/` qualifies
(`http.py` is the internal transport for the public `romm_api.py`); `adapters/retroarch/` would not (a config reader and
a core lookup share only a brand name). Service decompositions with shared state qualify — `services/saves/`,
`services/library/`.

## Sub-package `__init__.py` `[ours]`

- **Top-level layer namespace** (`adapters/`, `services/`, `domain/`, `lib/`, `models/`): empty (docstring optional).
  Consumers deep-import.
- **Consumed via package import** (`from package import X`): contract-style module docstring, re-exports of the public
  class(es), optional `__all__`. Examples: `services/saves/`, `services/saves/sync_engine/`.
- **Consumed only via deep-import**: empty or docstring, no re-exports. Example: `adapters/romm/`.

Implementation never lives in `__init__.py` — it is a namespace marker plus re-exports.

## Docstrings — intent over behavior

**Module and class docstrings** describe **what belongs here** (the contract), not what is currently in the file
(behavior). Behavior listings rot when methods change; contracts don't.

- Bad (class): `"""Owns save_sync_state.json — persistence, migrations, default construction."""` (rots when a 4th
  responsibility lands)
- Good (class): `"""Owns save_sync_state.json — single source of truth for on-disk save-sync state."""`

**Method docstrings are different** — one method's contract is naturally scoped, so describing behavior, parameters, and
return value is fine and stays in sync with the signature.

Avoid: "mechanical extraction from X", "during the transition", "moved from Y", "added for the Z flow", "see PR #123" —
commit-message content that rots in source.

## Testing

Every backend feature or callable where testing makes sense MUST have unit tests: **happy path**, **bad path** (invalid
input, missing data, API errors, network failures), and **edge cases** (empty strings, None, masked values `"••••"`,
boundaries). Tests mirror the source structure (`tests/services/`, `tests/adapters/`, …), one test file per source
module. Shared mocks live in `tests/conftest.py`.

### Property-based tests — pure decision kernels (hypothesis)

The pure save-sync decision kernels (`domain/sync_action.py`, `domain/save_path.py`, `domain/iso_time.py`) carry a
property tier on top of hand-enumerated cases, in `tests/domain/test_*_property.py`. Properties state the safety
invariant directly (no destructive action without a recovery source; decisions stable under timestamp-format variation;
replay determinism). `hypothesis` is dev-only and never ships.

**A property states the TRUE invariant, never a watered-down one.** If the invariant's fix is still open, the property
FAILS today — pin it `@pytest.mark.xfail(strict=True, reason="#<issue>: <one-line>")`. When the fix lands the property
passes → XPASS → CI fails → the marker must be removed. A property is never weakened to go green.

### Contract tests — real `Plugin` over real `bootstrap`

`tests/contract/` crosses the frontend↔backend wire: it builds the **real** `Plugin` through the **real** `bootstrap()`
and `wire_services()` (real settings dict, real SQLite + migrations, real file-store adapters, all under `tmp_path`) and
drives the actual `main.py` callables. Only the outermost edges are faked (`romm_api`, `sgdb_adapter`,
Clock/UuidGen/Sleeper, `emit`, and `http_adapter.with_retry` as a single-attempt pass-through). Harness lives in
`tests/contract/_harness.py`; seeding helpers in `tests/contract/_seed.py`.

- **Call callables exactly as the frontend does** — positional, JSON-shaped arguments with the arg types declared in
  `src/api/backend.ts` (literal `None` where the TS type says `null`).
- **Assert the response SHAPE + behavior, not delegation.** Pin the literal dict keys, the canonical failure shape, the
  discriminated-status union, and the partial-success carve-outs. Where a callable has a server-reachable failure mode,
  exercise BOTH the happy path AND the failure path.
- The `harness` fixture is **async** so it binds the test's running event loop. Each test gets a fresh `tmp_path`.

`scripts/check_callable_manifest.py` derives the frontend surface from every `callable<[Args], Return>("name")` in
`src/**/*.ts` and the backend surface from the public `async def` methods on `Plugin`, failing on any name or arity
divergence. It runs standalone in CI and inside pytest via `tests/contract/test_callable_manifest.py`.

### Gavel conformance vectors — vendored contract tier

Two [romm-gavel](https://github.com/danielcopper/romm-gavel) vector families run against the production save-sync
kernels: **ladder** against `domain/sync_action.resolve_upload_conflict`, **decision-table** against
`compute_sync_action`. The vectors are vendored verbatim under `tests/domain/gavel_vectors/` at a pinned upstream commit
(no submodule, no network in CI), so a contract change lands as a reviewable diff. **Never edit a vector to make the
kernel pass** — updating means deliberately re-copying the JSON and bumping the commit reference in that folder's
`README.md`.

### Frontend component tests — `@decky/api` event harness

`src/test-utils/decky-api-mock.ts` exposes an in-memory event bus that `addEventListener` / `removeEventListener` route
through. Tests dispatch backend events via `emitDeckyEvent` instead of mocking `@decky/api` per file;
`src/components/CustomPlayButton.test.tsx` is the reference shape. The bus resets between tests; use
`deckyEventListenerCount(name)` to assert `useEffect` cleanup ran. DOM-level `globalThis.dispatchEvent` flows bypass the
harness — happy-dom handles them natively. Prefer the harness over extracting listener bodies into `src/utils/*.ts`
purely for testability.

**Catch coverage assertions must be non-vacuous.** A test claiming `.catch` coverage MUST assert the post-catch state —
the fallback return value, the toast body, the `debugLog` message, the surfaced status. Asserting only that the
rejecting call was invoked is vacuous: it passes with or without the `.catch`.

## Security

- NEVER read or use credentials from settings files (`~/homebrew/settings/`) without explicit user permission
- NEVER pass credentials to agents — if API calls are needed, ask the user to run them and provide output
- NEVER log secrets (passwords, API keys) — mask them in any log output

## Working Style

- **Research before implementing.** When encountering an unknown (how a third-party tool works, where files are stored,
  what APIs exist), STOP and research first. Present findings and agree on an approach before implementation.
- **Discuss architecture decisions.** This is not a vibe coding project. Non-trivial changes require discussion before
  code is written. When you find a problem, explain it and propose options — don't just start fixing.
- **Use agents** for everything beyond trivial single-file edits — research, exploration, implementation. Keep main
  context on architecture and coordination.
- **Sequential agent discipline.** Each agent's prompt MUST include: "When done, report back and wait for shutdown. Do
  NOT pick up other tasks from the task list."
- **Preserve context.** Get alignment first, then implement cleanly in one pass.
- **Sub-issue policy**: epic bodies do **not** carry markdown sub-issue lists — open work is tracked via GitHub's native
  Sub-Issues panel. Link new sub-issues natively; don't add body bullets.
- Roadmap: [GitHub Projects board](https://github.com/users/danielcopper/projects/2).
