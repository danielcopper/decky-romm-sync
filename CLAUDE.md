# decky-romm-sync — Decky Loader Plugin

## What This Is

A Decky Loader plugin that syncs a self-hosted RomM library into Steam as Non-Steam shortcuts. Games launch via
RetroDECK. The QAM panel handles settings, sync, downloads, and BIOS management.

## Documentation

**Docs are updated in the same PR as the code change. This is not optional.** When a change affects architecture, data
flows, feature behavior, or user-facing UI, the relevant page under `docs/` must be updated in the same PR.
Documentation-debt-as-a-separate-follow-up-issue is forbidden — those follow-ups never land. If you're not sure whether
a change needs docs, the default is "yes, it does." Enforced in CI by `.github/workflows/docs-check.yml`.

The docs live in `docs/` and are the canonical source for architecture, file structure, and feature documentation. Built
with **Material for MkDocs** and published to GitHub Pages (<https://danielcopper.github.io/decky-romm-sync/>) by
`.github/workflows/docs.yml` on every push to `main`. Layout mirrors the three nav tabs: `docs/user-guide/` (end users),
`docs/architecture/` (how it works), `docs/contributing/` (dev setup). The old GitHub Wiki is retired — it only
redirects to the published site. Preview locally with `mise run docs`.

For genuinely doc-irrelevant PRs (pure refactor with no user-visible change, no architecture shift, no new flow;
tooling/CI changes; dependency bumps), set the `no-docs-change` label on the PR OR include `docs: N/A` (with a one-line
reason) in the PR description. The default posture is "docs needed"; opting out is an explicit acknowledgement, not a
silent omission. The CI check enforces this.

## Key Technical Constraints

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart;
  SteamClient API is instant.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT
  `ServerAPI.callPluginMethod()`).
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode).
  Paginated: `{"items": [...], "total": N}`.
- **AddShortcut timing**: Must wait 300-500ms after `AddShortcut()` before setting properties. Use 50ms delay between
  operations.
- **Large payloads**: Never send bulk base64 data through `decky.emit()` — WebSocket bridge has size limits. Use
  per-item callables instead.
- **User-Agent on outgoing HTTP**: SteamGridDB **and** RomM behind Cloudflare Tunnel reject the default `Python-urllib`
  UA with 403 (Bot Fight Mode at the edge). Every HTTP-talking adapter (`RommHttpAdapter`, `SteamGridDbAdapter`) takes a
  `user_agent: str` ctor param. Bootstrap reads `package.json` once via `PluginMetadataReader` and threads
  `decky-romm-sync/<version>` to both — single source of truth, no hardcoded version strings.
- **AddShortcut ignores most params**: `SteamClient.Apps.AddShortcut(name, exe, startDir, launchOptions)` ignores
  startDir and launchOptions (confirmed by MoonDeck plugin). Must use `Set*` calls (`SetShortcutName`, `SetShortcutExe`,
  `SetShortcutStartDir`, `SetAppLaunchOptions`) after a 500ms delay. Do NOT pass quoted exe paths — the API handles
  quoting internally.
- **BIsModOrShortcut bypass DROPPED**: Phase 5.6 removed the bypass counter entirely. Shortcuts return
  `BIsModOrShortcut() = true` (natural state). We own the entire game detail UI via RomMPlaySection + future
  RomMGameInfoPanel.
- **Shortcut property updates**: A shortcut's appId is derived from `exe + appName` (CRC32), so
  `launchOptions`/`startDir` changes are **appId-safe** (same shortcut, binding/artwork/collections survive) and
  `SetAppLaunchOptions`-on-existing is **reliable** (hardware-validated in #827: in-session + restart + churn). Use the
  fire-then-poll-`AppDetails` confirm (`setLaunchOptionsConfirmed`) since `Set*` returns void. Delete + recreate
  (re-sync) is only needed for **exe/name** changes, which produce a different appId. The real hazard is removal-churn
  corrupting Steam's in-memory shortcut state (a restart clears it).
- **Launcher + launch_options model**: `bin/rom-launcher` (renamed from `bin/romm-launcher`, #778) is a pure `exec "$@"`
  wrapper — no state, no path resolution, no emulator knowledge. The Steam shortcut's `launch_options` carries the FULL
  launch command (`flatpak run net.retrodeck.retrodeck "<rom-path>"`) for installed ROMs, or `""` (placeholder) for
  uninstalled. The emulator invocation is a build-time variable (`resolve_emulator_invocation`, RetroDECK today — the
  #129 seam). The old `romm:<rom_id>` marker is GONE: ownership is detected by the **exe path** (`…/bin/rom-launcher`);
  rom_id↔appId comes from the backend `get_app_id_rom_id_map()` (`roms.shortcut_app_id`). launch_options is written at
  sync (installed ROMs), at download-complete, and re-resolved on RetroDECK-home migration (`migration_relaunch_options`
  event). See [ADR-0009](docs/adr/0009-launcher-pure-exec-wrapper-baked-launch-options.md).
- **RomM minimum version**: Requires RomM >= 4.8.1. Hard-rejected in `test_connection()` — plugin is inert until server
  is updated. `_MIN_REQUIRED_VERSION` tuple in `main.py`.
- **Token-host binding**: A Client API Token is bound to the server origin it was minted against
  (`romm_api_token_origin` — canonical `scheme://host[:port]` via `lib/url_host`; `https://h` and `http://h` are
  different origins). The bearer is sent only when `romm_url`'s origin matches; a mismatch raises
  `TokenHostMismatchError` in `RommHttpAdapter.auth_header` (non-retryable → `config_error`, "sign in again"), so a
  wrong/hostile host never receives the credential. Sign-in (`establish_token`) validates the URL, holds the candidate
  URL in memory while probing (the old token is cleared in memory first so it never leaks to the candidate host), and
  persists URL+SSL+token+id+origin in a single atomic save only on success — a failed sign-in restores the previous
  working state, never clobbering disk. The old-token DELETE on re-auth is fired only when the old origin matches the
  new one (#1038). A legacy token with origin `None` is un-bound: attached, never blocked, until the next sign-in stamps
  it. See [ConnectionService notes](docs/architecture/backend-architecture.md#connectionservice-notes).
- **Decky callables must be async**: Even if the method body is synchronous, Decky's callable framework requires
  `async def`. Do not remove `async` from callable methods in main.py.
- **Settings durability**: `settings.json` is written crash-safe — write-tmp → `fsync(tmp)` → `os.replace()` →
  `fsync(dir)` (the Steam Deck's ext4 can otherwise leave a truncated file on power loss, and boot rewrites the file
  every run). A corrupt/unparseable `settings.json` is **never silently factory-reset**: it is logged loudly, backed up
  to `settings.json.corrupt-<ts>` (`<ts>` from the injected `Clock`), and the adapter sets a transient `corrupt_reset`
  flag before defaults are written — so the original bytes survive for recovery. Bootstrap reads that flag after
  migration and folds it into the settings dict as a **persistent** `_settings_reset_notice` marker (set post-migration,
  pre-save, so it lands in the fresh `settings.json` and survives a plugin reload). The frontend reads it via the
  non-consuming `get_settings_reset_notice` callable and surfaces a **persistent notice** — a QAM banner
  (`SettingsResetBanner`, with a **Dismiss** button) plus a game-detail card (`SettingsResetCard`, informational only —
  its copy points the user to the QAM to dismiss), mirroring the migration-notice pattern, **not a toast** — so the user
  knows to re-enter the server URL and sign in. The marker is cleared **only by an explicit user ACK in the QAM**: the
  Dismiss button calls `dismiss_settings_reset_notice` (→ `SettingsService.dismiss_settings_reset_notice`), which pops
  `_settings_reset_notice` and persists; the frontend then clears the shared `settingsResetStore` so the banner and
  every game-detail card disappear at once. Sign-in does **not** clear it. The settings `version` is stamped
  `max(stored, _SETTINGS_VERSION)` on write — a file from a newer plugin is **never down-stamped**. `PersistenceAdapter`
  takes an injected `clock` (bootstrap threads the shared `SystemClock`).

## Current State

Latest release and shipped features: see `git tag --sort=-v:refname` and GitHub Releases. Roadmap and open work:
[GitHub Projects board](https://github.com/users/danielcopper/projects/2).

## Development

- **Build**: `pnpm build` (Rollup -> dist/index.js)
- **Tests**: `python -m pytest tests/ -q` or `mise run test`
- **Coverage**: `python -m pytest tests/ -q --cov=py_modules --cov=main --cov-report=term --cov-branch`
- **Setup**: `mise run setup` (installs JS + Python dependencies)
- **Dev reload**: `mise run dev` (build + restart plugin_loader)
- **Tooling**: mise manages node, pnpm, python, uv. Venv auto-creates at `.venv` (via `_.python.venv` in mise.toml)
  using uv as the underlying tool; `mise run setup` installs Python deps via `uv pip install` (uv is the canonical
  Python package manager in this project). Python deps are pinned in `requirements-dev.lock` / `requirements-docs.lock`,
  compiled from the `requirements-*.txt` sources by `uv pip compile`; `mise run setup` and CI install from the lock, and
  `mise run lock-update` regenerates it after editing a source or bumping a pin.
- **Pre-commit hook** (`.githooks/pre-commit`, wired by `mise run setup` via `core.hooksPath`): formats staged files —
  `ruff format` + `ruff check` (Python), `prettier --write` (TS/TSX), and `deno fmt` (Markdown). Stays fast (<2s) so
  commits don't become friction — heavy validation (basedpyright, lint-imports, cosmic bans, pytest) is CI-only on PR
  push, never in the commit hook. CI + branch protection enforces correctness; don't re-introduce heavy checks here.

## Code Quality

- **SonarCloud**: CI-based analysis on every PR + push to main. Quality Gate enforces 80% coverage on new code, 0 bugs,
  0 vulnerabilities.
- **Ruff**: Python linting in CI.
- **basedpyright**: Type checking in CI.
- **import-linter**: Layer boundary enforcement in CI (services ↛ adapters, adapters ↛ services, services independent).
- **Cosmic Python call bans**: `scripts/check_cosmic_call_bans.sh` — services may not call `datetime.now()` /
  `asyncio.sleep()` / `time.time()` / `time.monotonic()` / `uuid.uuid4()` / `random.*` directly (use the corresponding
  Protocol).
- **Aggregate field-assignment ban**: `scripts/check_aggregate_field_assignment.py` — AST check that fails CI if
  `services/` assigns `aggregate.field = value` on a `@cosmic_aggregate` root (mutation must go through verb-named
  methods). Enforces the Aggregates `[CP]` rule.
- **Service-independence contract self-check**: `scripts/check_service_independence_contract.py` — derives the expected
  service list from `py_modules/services/` and fails CI if `.importlinter`'s `service-independence` contract omits a
  service or carries a stale entry, keeping the hand-maintained `modules` list self-healing.
- **Callable-manifest parity gate**: `scripts/check_callable_manifest.py` — derives the frontend callable surface from
  every `callable<[Args], Return>("name")` in `src/**/*.ts` and the backend surface from the public `async def` methods
  on the `Plugin` class in `main.py`, and fails CI if they diverge: a name on one side only (either direction) or a
  matching name whose arity (positional param count) differs. Arg TYPES are out of scope — Python signatures carry no
  hints, so arity is the only mechanically checkable shape (the contract tier exercises types by driving real values).
- **Failure-shape dialect gate**: `scripts/check_failure_shape.py --check` — AST check that fails CI if any
  `success: False` return in `services/` is missing the canonical `reason` + `message` keys or carries the forbidden
  `error` / `error_code` key. The two documented carve-outs (discriminated-status unions, partial-success payloads) are
  pattern-exempt. Enforces the "Callable response shapes" convention below.
- **pytest-cov**: Branch coverage reported to SonarCloud.

## Architecture — Cosmic Python rules

Cosmic Python ("Architecture Patterns with Python", Percival & Gregory) is our north star, adapted for a single-user
Decky plugin domain. The rules below mix canonical CP principles with project conventions we layered on top. Each rule
carries a tag:

- `[CP]` — Canonical Cosmic Python. Hard rule. Breaking it is an architectural regression.
- `[ours]` — Project convention layered on top of CP. Implements CP, not prescribed by it. Style/consistency rule —
  deviations should be flagged in review but are not architectural regressions; the project rule itself can be debated
  and softened.

Backend layout: `services/` (orchestration) / `adapters/` (I/O) / `domain/` (pure compute) / `lib/` (cross-cutting
utilities) / `models/` (data shapes). `import-linter` enforces direction. `[CP]`

**Services**:

- `[CP]` Depend on Protocols (defined in the `services/protocols/` package — re-exported from `__init__`, topically
  split across `transport`/`determinism`/`persistence`/`paths`/`infra`/`files`/`cross_service`; import via
  `from services.protocols import X`), never on concrete adapter classes. (Canonical dependency inversion.) Carve-out:
  sub-services within a single bounded context (e.g. all of `services/saves/`) may hold concrete peer-service refs in
  their `*ServiceConfig` dataclass when they share an aggregate (e.g. `RomSaveState`). The `[CP]` Protocol rule applies
  to services across bounded contexts and to adapters. `[ours]` A method that one sub-service calls on a peer is part of
  that peer's **public** surface — no leading underscore. The `_` prefix is reserved for genuinely class-internal
  helpers, so `reportPrivateUsage` stays coherent with this carve-out: peers call public methods, not private ones.
- `[CP]` No raw I/O.
  - `[ours]` Concrete allow/deny list: forbidden in `services/`: `os.*` (except pure path algebra: `relpath`, `join`,
    `splitext`, `basename`, `dirname`), `open(...)`, `pathlib.Path(...).read_*` / `write_*`, `fcntl.*`, `urllib.*`,
    `shutil.*`, `subprocess.*`, `hashlib.<x>(open(...))`. (Our enforcement surface; CP says "no I/O" without spelling
    out the call list.)
- `[CP]` No clocks or randomness — inject side-effecting deps via abstractions.
  - `[ours]` Specific Protocols: `Clock` / `UuidGen` / `Sleeper`. `time.time()` / `time.monotonic()` / `datetime.now()`
    / `uuid.uuid4()` / `asyncio.sleep()` / `random.*` banned at the call site.
- `[CP]` No service-to-service concrete imports — services are independent. Cross-service deps are Protocol-typed.
- `[ours]` Module functions from `domain/` are still a coupling — if tests need `patch("services.X.module_name.fn")`,
  wrap the module behind a Protocol and inject it. (Our enforcement tactic; CP doesn't prescribe Protocol-wrapping every
  module function.)
- `[ours]` **Constructor shape: every service takes a single `config: XxxServiceConfig` keyword argument.** Frozen
  dataclass, named `<ServiceName>Config` — outer services keep the `Service` token in both class and config name
  (`SteamGridConfig` is wrong, `SteamGridService` + `SteamGridServiceConfig` is right). Sub-services may use role-based
  class names without the token (`SyncEngine` + `SyncEngineConfig`, `SyncOrchestrator` + `SyncOrchestratorConfig`) when
  the role name reads more naturally than the suffixed form. All deps live in the config: Protocol-typed adapters,
  infrastructure (loop, logger, clock, uuid_gen, sleeper), persistence callbacks, settings-derived values. No bare-param
  ctors, no mixed (some-explicit + some-in-config) ctors. Test setup is uniform: build `XxxServiceConfig(...)`, pass
  `XxxService(config=...)`. (Project pattern. CP allows explicit ctor params; this is our consistency choice.)
- `[ours]` **Debug logging: inject the `DebugLogger` Protocol.** Don't add per-service `_log_debug` methods that re-read
  settings at call time, and don't reach for `decky.logger.info` to bypass log-level filtering. The Protocol's wiring
  decision is the only knob.
- `[ours]` God-class signal: services > ~700 LOC — decompose into sub-services with constructor injection (see
  `services/saves/` for the reference pattern). Matches the `bootstrap.py` split threshold below. The S107 ctor-param
  threshold no longer fires because all Protocol-typed deps live in the config. (Our taste/threshold. Earlier wording
  said ~600 LOC; raised after audit #485 found 5 stable cohesive files in the 656-749 range — fetcher,
  sync_orchestrator, migration, slots/service, sync_engine/matrix.)

**Adapters**: `[CP]` Own all I/O. Never import from `services/`. Implement Protocols defined in the
`services/protocols/` package. (Canonical ports-and-adapters.)

**Domain**: `[CP]` Pure compute only. No I/O, no state mutation, no service or adapter imports. Functions take inputs,
return outputs. Anything stateless and I/O-free that's currently in a service belongs here. (Canonical domain-model
purity.)

**Aggregates** (CP chapters 1–7 scope — locked in #788, refined by
[ADR-0003](docs/adr/0003-json-sqlite-persistence-boundary.md)). The aggregate roots, their tables, and the enforcement
layers live in `docs/architecture/database-design.md` (canonical — 8 roots after ADR-0003). Persistence boundary:
config-shaped toggles (`save_sync_enabled`, `sync_before_launch`, `sync_after_exit`, `default_slot`,
`autocleanup_limit`, `device_name`, `enabled_platforms`) live in `settings.json`, not SQLite —
`SyncSettings`/`Platform`/`Device` were considered as aggregates and dropped. The rules below apply to the relational
state that _does_ live in SQLite:

- `[CP]` One Repository Protocol per aggregate root, not per table. Aggregate boundaries are domain-modeling decisions;
  table layout is downstream and may need multiple tables to back one aggregate.
- `[CP]` Aggregate methods are the **only** mutation API for the aggregate's state. No external field assignment
  (`aggregate.field = value`) from services. Services call methods; methods enforce invariants and update internal
  state. Field access for reads is fine.
- `[ours]` **Mutation methods are verb-named after the domain event they conceptually represent.**
  `adopt_baseline(filename, hash)` not `update_baseline(...)`. `mark_installed(path)` not `set_installed(...)`.
  `promote_slot(slot, source)` not `update_slot_source(...)`. Why: intent-revealing names encode what _happened_, not
  which fields changed; the method name becomes the implicit event name (`BaselineAdopted`, `Installed`, `SlotPromoted`)
  if/when chapter 8+ events get added in a follow-up epic. Free refactor seam, zero cost now.
- `[ours]` Chapter 8+ (domain events + message bus) is explicitly **out of scope** for the current SQLite epic. Trigger
  for revisiting: handler diversity ≥3 kinds for the same aggregate state change, OR a non-Steam consumer (CLI/web/etc.)
  becomes concrete, OR a telemetry/analytics layer needs to subscribe.

**Bootstrap (`bootstrap.py`)**: `[CP]` The composition root — the only place where concrete adapters meet services.
(Canonical CP composition root.)

- `[ours]` `WiringConfig` holds the wiring; protocols come in, services come out. Adapter instantiation never happens in
  `main.py` — if a service needs a Protocol-wrapped persister, the wrapper adapter is built in `bootstrap()` and passed
  through `CallbackBundle`. (Our concrete shape for the composition root.)

**Vendored deps (`_vendor/`)**: `[ours]` Decky Loader has no plugin-level package manager, so third-party runtime deps
are vendored under `py_modules/_vendor/<package>/` and imported as `from _vendor import <package>`. Only adapters import
from `_vendor.*`; services/domain/lib stay third-party-free. The whole `_vendor/` namespace is excluded from ruff,
basedpyright, and Sonar (analysis + coverage) so any future vendored package is automatically out of scope — it's not
our code, we don't lint or coverage-track it, but we may patch it (e.g. fix self-imports broken by the move into
`_vendor/`). Ruff's isort lists `_vendor` under `known-third-party` so the imports group alongside other third-party
deps. `import-linter` enforces a `domain-stdlib-only` contract that forbids `domain` from importing `_vendor.*` (domain
stays stdlib-only); no other layer forbids `_vendor`, since adapters legitimately import it. Every vendored package
ships its upstream `LICENSE` (the release zip redistributes `_vendor/`, and MIT/BSD-style licenses require preserving
the copyright notice on redistribution) and a provenance entry in `_vendor/README.md` — upstream URL, pinned
version/commit, and the list of local patches — so updating a vendored dep is a deliberate diff, not "diff and pray".

**Process boundaries — `main.py` vs `bootstrap.py`**: `[ours]` `main.py` owns the Decky lifecycle (`_main`, `_unload`)
and the callable surface (one `async def` method per `@callable` exposed to the frontend). `bootstrap.py` owns adapter
instantiation and service wiring. The split is binding — no callables in `bootstrap.py`, no service wiring in `main.py`.
Both files grow with the surface they describe (callables for `main.py`, services for `bootstrap.py`); this is
unavoidable density, not god-class. Split `bootstrap.py` into `bootstrap/{adapters,services}.py` only when it exceeds
~700 LOC. (Decky-plugin-specific; not a CP concept.)

If a refactor breaks a `[CP]` rule, that's an architectural regression — call it out and fix it in the same PR or open a
follow-up. `[ours]` deviations should be flagged in review but can be debated (we can choose to soften the project rule
rather than change the code).

## Protocol naming — suffix by shape

Protocol names carry a suffix that signals shape, so the call site reads correctly without jumping to the definition.
`[ours]`

- `…Reader` — object-shaped Protocols with multiple methods (e.g. `RetroArchConfigReader`, `RetroArchCoreInfoReader`).
- `…Provider` or `…Fn` — call-shaped Protocols (`__call__`-only) (e.g. `RetroArchSaveSortingProvider`,
  `CoreNameProviderFn`).
- `…Store` — file-store Protocols (e.g. `CoverArtFileStore`).
- `…Cache` — cache Protocols (e.g. `SgdbArtworkCache`).
- `…Persister` — persistence Protocols (e.g. `SettingsPersister`).
- Bare names — pervasive cross-cutting primitives (`Clock`, `Sleeper`, `UuidGen`, `DebugLogger`).

When a sibling Protocol set mixes shapes (e.g. `RetroArchConfigReader` next to `RetroArchSaveSortingProvider`), that mix
is intentional and reflects the shape difference, not a naming inconsistency.

## Async/sync method naming `[ours]`

- Async methods carry the bare domain-verb name — no `_async` / `Async` suffix. `await` marks them at the call site
  (Python norm; unlike .NET).
- When an async method needs a **synchronous twin** — typically a lock-free worker run via `run_in_executor` that a peer
  must call directly to avoid re-entering a lock the async path already holds — name the sync worker:
  - `do_<verb>` if it's **public / peer-called** (e.g. `do_download_save`, `do_upload_save`, `do_sync_rom_saves`).
  - `_<verb>_io` if it's **private / internal-only** (e.g. `_remove_rom_io`, `_uninstall_all_roms_io`).
- The async public method keeps the bare verb (`sync_rom_saves`); never disambiguate by marking the async side.

The two sync-worker idioms (`do_` prefix for public, `_io` suffix for private) coexist by access level — that split is
the current state, not a settled ideal. Unification (converge `do_` onto `_io`) is tracked in #813.

## Callable response shapes — canonical failure shape

Decky callables that return a plain `dict` and can fail use the canonical failure shape
`{success: False, reason: ErrorCode | str, message: str}`. Both `reason` and `message` are **required**. Reuse
`lib.list_result.ErrorCode` (the Lean enum: `SERVER_UNREACHABLE`, `AUTH_FAILED`, `NOT_FOUND`, `UNSUPPORTED`, `UNKNOWN`,
plus the frontend-routed `VERSION_ERROR` / `STALE_CONFLICT` / `STALE_PREVIEW`) for the coarse categories; bespoke
non-server-reachability guards (`config_error`, `sync_disabled`, `not_installed`, `active_slot`, …) stay plain-string
`reason` values — the `ErrorCode | str` union allows it. Transport failures collapse onto `SERVER_UNREACHABLE`; 401 and
403 collapse onto `AUTH_FAILED` (same slug, but the `message` stays distinct so a Cloudflare bot-fight 403 reads
differently from wrong credentials). The legacy `error_code` key and a second `error` key are **forbidden** — never
duplicate `reason` into `error`, never replace `message` with `error`. `[ours]`

`scripts/check_failure_shape.py --check` enforces this in CI (`mise run lint` + the CI gate step): every
`success: False` return in `services/` must carry `reason` + `message` and must not carry `error` / `error_code`. In
this repo, conventions with a mechanical check stay true; conventions in prose drift.

Two carve-outs (also pattern-exempt in the gate):

- **Discriminated-status unions** (the `status: "ok" | "server_unreachable" | "version_deleted" | …` shape used by the
  saves version-history callables) keep the `status` discriminant — a dict with `status` and no `success`. They carry
  more than two outcomes, so a binary `success` boolean would erase the routing slug. Failure branches still carry
  `message: str`, not `error: str`.
- **Partial-success responses** that return a full payload alongside a failure flag (e.g. `get_save_status`'s additive
  `server_query_failed: bool`, `get_save_setup_info`'s `recommended_action: "server_unreachable" | ...`) keep the
  additive flag. The call has half-broken half-working semantics that the binary boolean would erase.

Full convention paragraph lives in the `lib/list_result.py` module docstring.

## Cosmic Python migration — status & reference pattern

The full Cosmic Python migration (umbrella [#277](https://github.com/danielcopper/decky-romm-sync/issues/277)) is
**complete**: every backend service has I/O behind Protocol-typed adapters, Clock/UuidGen/Sleeper injected, pure logic
in `domain/`, and ctors decomposed via frozen `*ServiceConfig` dataclasses. The blow-by-blow (Waves 1–4 + the saves
vertical) lives in closed issues #294–#340 and the git log; the only deferred item is #259 (SonarCloud arch rules,
blocked on SonarCloud Python support). The separate SQLite persistence epic (#271) is ongoing — tracked via the
Aggregates section above + `docs/architecture/database-design.md`.

**Why that order** (kept as the playbook for future verticals): cross-cutting Protocols (Clock/UuidGen/Sleeper, #294)
first, so every later vertical was a mechanical "drop the import, inject the Protocol"; domain extraction (#295) before
LibraryService, to shrink the scariest service before lifting it; LibraryService last (largest blast radius — by then
only ctor decomposition remained).

**Canonical reference for any future service-level work**: the Wave 3 sister-PR shape — a Protocol (in
`services/protocols/`) + an adapter implementing it + a `FakeXxxAdapter` in `conftest` + `*ServiceConfig` ctor
decomposition. `services/saves/` and `services/library/` are the reference decompositions for shared-state sub-services.

**Sub-issue policy**: Epic bodies do **not** carry markdown sub-issue lists — open work is tracked via GitHub's native
Sub-Issues panel on each epic. If a new sub-issue is needed, link it natively (don't add a body bullet).

## Subfolder layout — when a subfolder is justified

Layer top-level folders (`services/`, `adapters/`, `domain/`, `lib/`, `models/`) are flat by default — one file per
concept. A subfolder is justified **only when the modules within share an internal type, helper, or state**, not when
they share a brand-name prefix.

- `adapters/romm/` qualifies: `http.py` is the internal HTTP transport for `romm_api.py`; the two share types and only
  `romm_api.py` is the public surface.
- `services/saves/` qualifies: facade + sub-services (`sync_engine/`, `slots/`, `status/`, `versions.py`) share a
  `RomSaveState` aggregate.
- `adapters/retroarch/` would NOT qualify: `retroarch_config.py` (RetroArch.cfg reader) and `retroarch_core_info.py`
  (core lookup) share nothing but a brand name. False cohesion.
- `adapters/steam/` would NOT qualify: would mix Steam (`steam_config.py`) with SteamGridDB (`steamgriddb.py`,
  `sgdb_artwork_cache.py`) — different vendor, different concern.

When a service-level decomposition produces sub-services with shared state, a subfolder is the right home —
`services/saves/` and `services/library/` (fetcher / sync_orchestrator / reporter sharing preview-delta state via
`_state.py`) both qualify. Absent shared state, file-level layout is the default.

## Sub-package `__init__.py` — when populated, when empty

Decision rule by how the package is consumed:

- **Top-level layer namespace** (`adapters/`, `services/`, `domain/`, `lib/`, `models/`): `__init__.py` is empty (a
  docstring is acceptable but not required). These exist as namespace markers; consumers always deep-import
  (`from adapters.romm.romm_api import RommApiAdapter`).
- **Sub-package consumed via package import** (consumers write `from package import X`): `__init__.py` holds the
  package's contract-style module docstring, re-exports of the public class(es), and optional `__all__`. Examples:
  `services/saves/`, `services/saves/sync_engine/`, `services/saves/slots/`, `services/saves/status/`.
- **Sub-package only consumed via deep-import** (consumers always write `from package.module import X`): empty or just
  docstring, no re-exports. Example: `adapters/romm/` — `bootstrap` deep-imports
  `from adapters.romm.romm_api import RommApiAdapter`.

Implementation never lives in `__init__.py`. Don't put 500+ LOC class definitions there — that obscures the package's
public surface and breaks the "init = namespace marker + re-export" Python convention.

Example of a re-export-only `__init__.py`:

```python
# services/saves/sync_engine/__init__.py
"""Newest-wins matrix executor ..."""

from services.saves.sync_engine.engine import SyncEngine

__all__ = ["SyncEngine"]
```

```python
# services/saves/sync_engine/engine.py
from __future__ import annotations
# ... imports, then the SyncEngine class
```

## Docstrings — intent over behavior

**Module and class docstrings** describe **what belongs here** (the contract), not what's currently in the file/class
(the behavior). Behavior listings and method enumerations rot when methods get added/changed/removed; contracts don't.

- Bad (module): `"""Version history listing and rollback flow. 1. Download. 2. PUT. 3. confirm_download."""`
- Good (module):
  `"""Save version history reads and the destructive version-switch flow. Anything that lists, fetches, or rolls back to an older save version lives here. Mutations of the active save record outside the rollback flow belong in SyncEngine or StatusService, not here."""`
- Bad (class): `"""Owns save_sync_state.json — persistence, migrations, default construction."""` (rots when a 4th
  responsibility is added)
- Good (class): `"""Owns save_sync_state.json — single source of truth for on-disk save-sync state."""`

**Method docstrings are different.** A method docstring describes one specific contract (this method's behavior,
parameters, return value, non-obvious how) — that contract is naturally scoped, so describing behavior is fine and stays
in sync with the signature. Numpy-style parameter sections on a class's `__init__` count as method-like for this
purpose.

Avoid all of: "mechanical extraction from X", "during the transition", "moved from Y", "added for the Z flow", "see PR
#123" — that's commit-message content that rots in source.

## Testing

Every backend feature or callable where testing makes sense MUST have unit tests. Cover:

- **Happy path**: Normal successful operation
- **Bad path**: Invalid input, missing data, API errors, network failures
- **Edge cases**: Empty strings, None values, masked values ("••••"), boundary conditions

Tests mirror the source structure: `tests/services/`, `tests/adapters/`, `tests/domain/`, `tests/models/`, `tests/lib/`.
Each test file maps 1:1 to a source module. Shared mocks live in `tests/conftest.py`.

### Property-based tests — pure decision kernels (hypothesis)

The pure save-sync decision kernels (`domain/sync_action.py`, `domain/save_path.py`, `domain/iso_time.py`) carry a
property-test tier on top of the hand-enumerated cases, in `tests/domain/test_*_property.py`. Properties state the
safety invariant directly (no destructive action without a recovery source; decisions stable under timestamp-format
variation; canonical-target grouping never mixes targets; replay determinism) and Hypothesis searches a generated input
space for a counterexample. `hypothesis` is a **dev-only** dependency (`requirements-dev.txt` → `requirements-dev.lock`
via `mise run lock-update`); it never ships in the plugin. A CI-safe profile in `tests/conftest.py` sets `deadline=None`
and a fixed `max_examples`; the example DB writes to gitignored `.hypothesis/`.

**Convention — pinning a property that encodes an open bug:** a property states the TRUE invariant, never a watered-down
one. If the invariant's fix is still open, the property FAILS today — pin it
`@pytest.mark.xfail(strict=True,
reason="#<issue>: <one-line>")`. `strict=True` means that the day the fix lands the
property passes → the run reports XPASS → CI fails → the marker must be removed, and the property then guards against
regression. So a property never gets weakened to go green: it either passes live (a regression guard) or is
`xfail`-pinned to its open bug.

### Contract tests — real `Plugin` over real `bootstrap`, callables driven frontend-shaped

`tests/contract/` is a tier that crosses the frontend↔backend wire. The unit tests exercise each side against its own
mocked idea of the other; the contract tier builds the **real** `Plugin` through the **real** `bootstrap()` +
`wire_services()` (real settings dict, real SQLite + migrations, real file-store adapters, all rooted under `tmp_path`)
and drives the actual `main.py` callable methods. Only the outermost edges are faked: `romm_api` → `FakeRommApi`,
`sgdb_adapter` → `FakeSteamGridDbApi`, the Clock/UuidGen/Sleeper seams → the deterministic fakes, `emit` → an
`AsyncMock`, and `http_adapter.with_retry` → a single-attempt pass-through (so a failure-injection test pays no backoff
sleep). The harness + its `harness` fixture live in `tests/contract/_harness.py` / `conftest.py`; shared
relational/server seeding helpers live in `tests/contract/_seed.py`.

Rules for this tier:

- **Call callables exactly as the frontend does** — positional, JSON-shaped arguments with the arg TYPES declared in
  `src/api/backend.ts` (literal `None` where the TS type says `null`, e.g. `get_installed_rom` returning `None`).
- **Assert the response SHAPE + behavior (the contract), not delegation.** Pin the literal dict keys, the canonical
  failure shape (`{success: False, reason, message}`), the discriminated-status union (`status: "ok" | ...`), and the
  partial-success carve-outs (`server_query_failed: bool`, `recommended_action`). Where a callable has a
  server-reachable failure mode, exercise BOTH the happy path AND the failure path — the failure-shape assertions are
  what guard the #1009/#1004-class bugs.
- The `harness` fixture is **async** so it binds the test's running event loop (the callables `await` on it; a
  mismatched loop raises "got Future attached to a different loop"). Each test gets a fresh `tmp_path`, so
  real-bootstrap state never leaks between tests.
- A wiring drift (a renamed/added service) fails the fixture loudly via the bound-attribute assert, not as a confusing
  mid-test `AttributeError`.

Phase 2 — the callable-manifest parity gate — is built: `scripts/check_callable_manifest.py` derives the frontend
surface from every `callable<[Args], Return>("name")` in `src/**/*.ts` (not just `backend.ts` — one declaration lives in
`utils/cachedGameDetailStore.ts`) and the backend surface from the public `async def` methods on `Plugin` in `main.py`,
then fails on any divergence: a name declared on only one side (either direction) or a matching name whose arity
(positional-param count, `self` dropped) differs. Arg TYPES stay out of scope — Python method signatures carry no hints,
so arity is the only mechanically checkable shape. The gate runs standalone in CI (`mise run lint` + a CI step) and is
also surfaced inside the pytest run by `tests/contract/test_callable_manifest.py`, which imports the same two parser
functions and asserts live parity — so a renamed/added/removed callable or an arity drift breaks both the lint gate and
the test run.

### Frontend component tests — `@decky/api` event harness

`src/test-utils/decky-api-mock.ts` exposes an in-memory event bus that `addEventListener` / `removeEventListener` route
through (wired in `src/test-setup.ts`). Tests dispatch backend events via `emitDeckyEvent` instead of mocking
`@decky/api` per-file. `src/components/CustomPlayButton.test.tsx` is the reference shape:

```tsx
import { emitDeckyEvent } from "../test-utils/decky-api-mock";

act(() => {
  emitDeckyEvent<[DownloadFailedEvent]>("download_failed", { rom_id: 42, ... });
});
await findByText("Download"); // assert visible side effect
```

The bus is reset between tests by `afterEach` in `test-setup.ts`. Use `deckyEventListenerCount(name)` to assert that
`useEffect` cleanup ran on unmount. DOM-level `globalThis.dispatchEvent(new CustomEvent(...))` flows (e.g.
`romm_data_changed`) bypass the harness — happy-dom handles them natively.

Prefer the harness over extracting listener bodies into `src/utils/*.ts` purely for testability. Helper extraction stays
valid for genuinely-reusable logic.

**Catch coverage assertions must be non-vacuous.** Tests that claim `.catch` coverage MUST assert the post-catch state —
the fallback return value, the toast body, the `debugLog` message, the surfaced status string. Asserting only that the
rejecting call was invoked is vacuous: it passes with or without the `.catch` because the rejection happens after the
call returns. If you can't observe the catch's side effect, the catch either needs an observable effect or the test
isn't earning its coverage.

## Security

- NEVER read or use credentials from settings files (`~/homebrew/settings/`) without explicit user permission
- NEVER pass credentials to agents — if API calls are needed, ask the user to run them and provide output
- NEVER log secrets (passwords, API keys) — mask them in any log output

## Working Style

- **Research before implementing.** When encountering an unknown (e.g. how a third-party tool works, where files are
  stored, what APIs exist), STOP and research first. Do not start writing code based on assumptions. Present findings to
  the user and agree on an approach before any implementation.
- **Discuss architecture decisions.** This is not a vibe coding project. Non-trivial changes require discussion before
  code is written. When you find a problem, explain it and propose options — don't just start fixing.
- **Use team-swarm agents** for everything beyond trivial single-file edits — including research, exploration, and
  implementation. Keep main context clean and focused on architecture and coordination by delegating to agents.
- **Sequential agent discipline.** When running agents sequentially, each agent's prompt MUST include: "When done,
  report back and wait for shutdown. Do NOT pick up other tasks from the task list." This prevents agents from grabbing
  the next unblocked task before the lead can shut them down and spawn a dedicated agent.
- **Preserve context.** Avoid back-and-forth code changes in the main conversation. Get alignment first, then implement
  cleanly in one pass (via agents).
- Refer to the [GitHub Projects board](https://github.com/users/danielcopper/projects/2) for the roadmap.
