# decky-romm-sync — Decky Loader Plugin

## What This Is

A Decky Loader plugin that syncs a self-hosted RomM library into Steam as Non-Steam shortcuts. Games launch via RetroDECK. The QAM panel handles settings, sync, downloads, and BIOS management.

## Documentation

**Docs are updated in the same PR as the code change. This is not optional.** When a change affects architecture, data flows, feature behavior, or user-facing UI, the relevant page under `docs/` must be updated in the same PR. Documentation-debt-as-a-separate-follow-up-issue is forbidden — those follow-ups never land. If you're not sure whether a change needs docs, the default is "yes, it does." Enforced in CI by `.github/workflows/docs-check.yml`.

The docs live in `docs/` and are the canonical source for architecture, file structure, and feature documentation. Built with **Material for MkDocs** and published to GitHub Pages (<https://danielcopper.github.io/decky-romm-sync/>) by `.github/workflows/docs.yml` on every push to `main`. Layout mirrors the three nav tabs: `docs/user-guide/` (end users), `docs/architecture/` (how it works), `docs/contributing/` (dev setup). The old GitHub Wiki is retired — it only redirects to the published site. Preview locally with `mise run docs`.

For genuinely doc-irrelevant PRs (pure refactor with no user-visible change, no architecture shift, no new flow; tooling/CI changes; dependency bumps), set the `no-docs-change` label on the PR OR include `docs: N/A` (with a one-line reason) in the PR description. The default posture is "docs needed"; opting out is an explicit acknowledgement, not a silent omission. The CI check enforces this.

## Key Technical Constraints

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart; SteamClient API is instant.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT `ServerAPI.callPluginMethod()`).
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode). Paginated: `{"items": [...], "total": N}`.
- **AddShortcut timing**: Must wait 300-500ms after `AddShortcut()` before setting properties. Use 50ms delay between operations.
- **Large payloads**: Never send bulk base64 data through `decky.emit()` — WebSocket bridge has size limits. Use per-item callables instead.
- **User-Agent on outgoing HTTP**: SteamGridDB **and** RomM behind Cloudflare Tunnel reject the default `Python-urllib` UA with 403 (Bot Fight Mode at the edge). Every HTTP-talking adapter (`RommHttpAdapter`, `SteamGridDbAdapter`) takes a `user_agent: str` ctor param. Bootstrap reads `package.json` once via `PluginMetadataReader` and threads `decky-romm-sync/<version>` to both — single source of truth, no hardcoded version strings.
- **AddShortcut ignores most params**: `SteamClient.Apps.AddShortcut(name, exe, startDir, launchOptions)` ignores startDir and launchOptions (confirmed by MoonDeck plugin). Must use `Set*` calls (`SetShortcutName`, `SetShortcutExe`, `SetShortcutStartDir`, `SetAppLaunchOptions`) after a 500ms delay. Do NOT pass quoted exe paths — the API handles quoting internally.
- **BIsModOrShortcut bypass DROPPED**: Phase 5.6 removed the bypass counter entirely. Shortcuts return `BIsModOrShortcut() = true` (natural state). We own the entire game detail UI via RomMPlaySection + future RomMGameInfoPanel.
- **Shortcut property re-sync**: Changing exe, startDir, or launchOptions on existing shortcuts may not take effect reliably. Full delete + recreate (re-sync) is required for changes to launch config.
- **RomM minimum version**: Requires RomM >= 4.8.1. Hard-rejected in `test_connection()` — plugin is inert until server is updated. `_MIN_REQUIRED_VERSION` tuple in `main.py`.
- **Decky callables must be async**: Even if the method body is synchronous, Decky's callable framework requires `async def`. Do not remove `async` from callable methods in main.py.

## Current State

Latest release and shipped features: see `git tag --sort=-v:refname` and GitHub Releases.
Roadmap and open work: [GitHub Projects board](https://github.com/users/danielcopper/projects/2).

## Development

- **Build**: `pnpm build` (Rollup -> dist/index.js)
- **Tests**: `python -m pytest tests/ -q` or `mise run test`
- **Coverage**: `python -m pytest tests/ -q --cov=py_modules --cov=main --cov-report=term --cov-branch`
- **Setup**: `mise run setup` (installs JS + Python dependencies)
- **Dev reload**: `mise run dev` (build + restart plugin_loader)
- **Tooling**: mise manages node, pnpm, python. Venv auto-activates via `_.python.venv` in mise.toml.

## Code Quality

- **SonarCloud**: CI-based analysis on every PR + push to main. Quality Gate enforces 80% coverage on new code, 0 bugs, 0 vulnerabilities.
- **Ruff**: Python linting in CI.
- **basedpyright**: Type checking in CI.
- **import-linter**: Layer boundary enforcement in CI (services ↛ adapters, adapters ↛ services, services independent).
- **Cosmic Python call bans**: `scripts/check_cosmic_call_bans.sh` — services may not call `datetime.now()` / `asyncio.sleep()` / `time.time()` / `time.monotonic()` / `uuid.uuid4()` / `random.*` directly (use the corresponding Protocol).
- **pytest-cov**: Branch coverage reported to SonarCloud.

## Architecture — Cosmic Python rules

Cosmic Python ("Architecture Patterns with Python", Percival & Gregory) is our north star, adapted for a single-user Decky plugin domain. The rules below mix canonical CP principles with project conventions we layered on top. Each rule carries a tag:

- `[CP]` — Canonical Cosmic Python. Hard rule. Breaking it is an architectural regression.
- `[ours]` — Project convention layered on top of CP. Implements CP, not prescribed by it. Style/consistency rule — deviations should be flagged in review but are not architectural regressions; the project rule itself can be debated and softened.

Backend layout: `services/` (orchestration) / `adapters/` (I/O) / `domain/` (pure compute) / `lib/` (cross-cutting utilities) / `models/` (data shapes). `import-linter` enforces direction. `[CP]`

**Services**:

- `[CP]` Depend on Protocols (defined in `services/protocols.py`), never on concrete adapter classes. (Canonical dependency inversion.) Carve-out: sub-services within a single bounded context (e.g. all of `services/saves/`) may hold concrete peer-service refs in their `*ServiceConfig` dataclass when they share an aggregate (e.g. `SaveSyncState`). The `[CP]` Protocol rule applies to services across bounded contexts and to adapters.
- `[CP]` No raw I/O.
  - `[ours]` Concrete allow/deny list: forbidden in `services/`: `os.*` (except pure path algebra: `relpath`, `join`, `splitext`, `basename`, `dirname`), `open(...)`, `pathlib.Path(...).read_*` / `write_*`, `fcntl.*`, `urllib.*`, `shutil.*`, `subprocess.*`, `hashlib.<x>(open(...))`. (Our enforcement surface; CP says "no I/O" without spelling out the call list.)
- `[CP]` No clocks or randomness — inject side-effecting deps via abstractions.
  - `[ours]` Specific Protocols: `Clock` / `UuidGen` / `Sleeper`. `time.time()` / `time.monotonic()` / `datetime.now()` / `uuid.uuid4()` / `asyncio.sleep()` / `random.*` banned at the call site.
- `[CP]` No service-to-service concrete imports — services are independent. Cross-service deps are Protocol-typed.
- `[ours]` Module functions from `domain/` are still a coupling — if tests need `patch("services.X.module_name.fn")`, wrap the module behind a Protocol and inject it. (Our enforcement tactic; CP doesn't prescribe Protocol-wrapping every module function.)
- `[ours]` **Constructor shape: every service takes a single `config: XxxServiceConfig` keyword argument.** Frozen dataclass, named `<ServiceName>Config` — outer services keep the `Service` token in both class and config name (`SteamGridConfig` is wrong, `SteamGridService` + `SteamGridServiceConfig` is right). Sub-services may use role-based class names without the token (`SyncEngine` + `SyncEngineConfig`, `SyncOrchestrator` + `SyncOrchestratorConfig`) when the role name reads more naturally than the suffixed form. All deps live in the config: Protocol-typed adapters, infrastructure (loop, logger, clock, uuid_gen, sleeper), persistence callbacks, settings-derived values. No bare-param ctors, no mixed (some-explicit + some-in-config) ctors. Test setup is uniform: build `XxxServiceConfig(...)`, pass `XxxService(config=...)`. (Project pattern. CP allows explicit ctor params; this is our consistency choice.)
- `[ours]` **Debug logging: inject the `DebugLogger` Protocol.** Don't add per-service `_log_debug` methods that re-read settings at call time, and don't reach for `decky.logger.info` to bypass log-level filtering. The Protocol's wiring decision is the only knob.
- `[ours]` God-class signal: services > ~700 LOC — decompose into sub-services with constructor injection (see `services/saves/` for the reference pattern). Matches the `bootstrap.py` split threshold below. The S107 ctor-param threshold no longer fires because all Protocol-typed deps live in the config. (Our taste/threshold. Earlier wording said ~600 LOC; raised after audit #485 found 5 stable cohesive files in the 656-749 range — fetcher, sync_orchestrator, migration, slots/service, sync_engine/matrix.)

**Adapters**: `[CP]` Own all I/O. Never import from `services/`. Implement Protocols defined in `services/protocols.py`. (Canonical ports-and-adapters.)

**Domain**: `[CP]` Pure compute only. No I/O, no state mutation, no service or adapter imports. Functions take inputs, return outputs. Anything stateless and I/O-free that's currently in a service belongs here. (Canonical domain-model purity.)

**Aggregates** (CP chapters 1–7 scope — locked in #788, applies as the SQLite migration #271 lands):

- `[CP]` One Repository Protocol per aggregate root, not per table. Aggregate boundaries are domain-modeling decisions; table layout is downstream and may need multiple tables to back one aggregate.
- `[CP]` Aggregate methods are the **only** mutation API for the aggregate's state. No external field assignment (`aggregate.field = value`) from services. Services call methods; methods enforce invariants and update internal state. Field access for reads is fine.
- `[ours]` **Mutation methods are verb-named after the domain event they conceptually represent.** `adopt_baseline(filename, hash)` not `update_baseline(...)`. `mark_installed(path)` not `set_installed(...)`. `promote_slot(slot, source)` not `update_slot_source(...)`. Why: intent-revealing names encode what *happened*, not which fields changed; the method name becomes the implicit event name (`BaselineAdopted`, `Installed`, `SlotPromoted`) if/when chapter 8+ events get added in a follow-up epic. Free refactor seam, zero cost now.
- `[ours]` Chapter 8+ (domain events + message bus) is explicitly **out of scope** for the current SQLite epic. Trigger for revisiting: handler diversity ≥3 kinds for the same aggregate state change, OR a non-Steam consumer (CLI/web/etc.) becomes concrete, OR a telemetry/analytics layer needs to subscribe.

**Bootstrap (`bootstrap.py`)**: `[CP]` The composition root — the only place where concrete adapters meet services. (Canonical CP composition root.)

- `[ours]` `WiringConfig` holds the wiring; protocols come in, services come out. Adapter instantiation never happens in `main.py` — if a service needs a Protocol-wrapped persister, the wrapper adapter is built in `bootstrap()` and passed through `CallbackBundle`. (Our concrete shape for the composition root.)

**Process boundaries — `main.py` vs `bootstrap.py`**: `[ours]` `main.py` owns the Decky lifecycle (`_main`, `_unload`) and the callable surface (one `async def` method per `@callable` exposed to the frontend). `bootstrap.py` owns adapter instantiation and service wiring. The split is binding — no callables in `bootstrap.py`, no service wiring in `main.py`. Both files grow with the surface they describe (callables for `main.py`, services for `bootstrap.py`); this is unavoidable density, not god-class. Split `bootstrap.py` into `bootstrap/{adapters,services}.py` only when it exceeds ~700 LOC. (Decky-plugin-specific; not a CP concept.)

If a refactor breaks a `[CP]` rule, that's an architectural regression — call it out and fix it in the same PR or open a follow-up. `[ours]` deviations should be flagged in review but can be debated (we can choose to soften the project rule rather than change the code).

## Protocol naming — suffix by shape

Protocol names carry a suffix that signals shape, so the call site reads correctly without jumping to the definition. `[ours]`

- `…Reader` — object-shaped Protocols with multiple methods (e.g. `RetroArchConfigReader`, `RetroArchCoreInfoReader`).
- `…Provider` or `…Fn` — call-shaped Protocols (`__call__`-only) (e.g. `RetroArchSaveSortingProvider`, `CoreNameProviderFn`).
- `…Store` — file-store Protocols (e.g. `CoverArtFileStore`).
- `…Cache` — cache Protocols (e.g. `SgdbArtworkCache`).
- `…Persister` — persistence Protocols (e.g. `StatePersister`, `FirmwareCachePersister`).
- Bare names — pervasive cross-cutting primitives (`Clock`, `Sleeper`, `UuidGen`, `DebugLogger`).

When a sibling Protocol set mixes shapes (e.g. `RetroArchConfigReader` next to `RetroArchSaveSortingProvider`), that mix is intentional and reflects the shape difference, not a naming inconsistency.

## Callable response shapes — canonical failure shape

Decky callables that return a plain `dict` and can fail use the canonical failure shape `{success: False, reason: ErrorCode | str, message: str}`. Reuse `lib.list_result.ErrorCode` for server-reachability failures (`ErrorCode.SERVER_UNREACHABLE`). Never duplicate `reason` into a second `error` field; never replace `message` with `error`. `[ours]`

Two carve-outs:

- **Discriminated-status unions** (the `status: "ok" | "server_unreachable" | "version_deleted" | …` shape used by the saves version-history callables) keep the `status` discriminant — they carry more than two outcomes, so a binary `success` boolean would erase the routing slug. Failure branches still carry `message: str`, not `error: str`.
- **Partial-success responses** that return a full payload alongside a failure flag (e.g. `get_save_status`'s additive `server_query_failed: bool`, `get_save_setup_info`'s `recommended_action: "server_unreachable" | ...`) keep the additive flag. The call has half-broken half-working semantics that the binary boolean would erase.

Full convention paragraph lives in the `lib/list_result.py` module docstring.

## Refactor wave plan (live — see #277 for current status)

The full Cosmic Python migration is tracked under [#277](https://github.com/danielcopper/decky-romm-sync/issues/277) (umbrella). Order is chosen to minimize rework: cross-cutting Protocols first, then domain promotions, then per-service vertical refactors smallest-to-largest.

- **Wave 1 — Cross-cutting infrastructure** ([#256](https://github.com/danielcopper/decky-romm-sync/issues/256)) — **complete except for deferred CI gate**
  Protocols, persisters, bootstrap cleanup. Done first so every later vertical consumes the Protocols defined here.
  Done: ~~#294~~ (Clock/UuidGen/Sleeper), ~~#289~~ (FirmwareCachePersister), ~~#292~~ (ArtworkRemover), ~~#296~~ (CoreInfoProvider, shipped as #310), ~~#205~~ (es_de_config I/O split, shipped as #311), ~~#168~~ (sync_state_box dead-code removal, shipped as #312), ~~#169~~ (WiringConfig split, shipped as #313), ~~#303~~ (call-site clock/sleep ban, shipped as #314).
  Deferred: #259 (SonarCloud arch rules — waiting on SonarCloud Python support).
- **Wave 2 — Domain promotions** ([#295](https://github.com/danielcopper/decky-romm-sync/issues/295)) — **complete**
  Pure logic extracted from non-saves services into `domain/`.
  Done: ~~#315~~ (firmware paths), ~~#316~~ (achievements), ~~#317~~ (path safety + mise lint bundle), ~~#318~~ (filename resolution), ~~#319~~ (sync_diff cluster).
- **Wave 3 — Per-service verticals** (smallest-to-largest, after Waves 1+2) — **complete**
  Every backend service refactored: I/O behind Protocol-typed adapters, Clock/UuidGen/Sleeper injected, pure logic in `domain/`, ctors decomposed via frozen `*ServiceConfig` dataclasses where they exceeded S107.
  - ~~#299~~ ArtworkService + SteamGridService — shipped as #321 (`CoverArtFileStore`) + #322 (`SgdbArtworkCache` + `SgdbApiError` + `SteamGridDirMissingError` + `write_shortcut_icon` on `SteamConfigAdapter`; `SteamGridConfig` decomposition + `PendingSyncReader` Protocol).
  - ~~#297~~ DownloadService — shipped as #323 (`DownloadFileAdapter` for filesystem; ZIP-slip protection; ctor 13 → 5 via `DownloadServiceConfig`).
  - ~~#298~~ FirmwareService — shipped as #324 (`FirmwareFileAdapter` with `checksum_md5` using `usedforsecurity=False`; closed #170 — `_enrich_firmware_file` returns new dict).
  - ~~#301~~ GameDetailService — closed as superseded. All scope (Clock + CoreInfoProvider) wired in Wave 1.
  - ~~#302~~ MigrationService — shipped as #325 (`MigrationFileAdapter` with cross-device `move` (`shutil.move`) vs same-fs `rename` (`os.replace`) distinction; ctor 13 → 2 via `MigrationServiceConfig`; closed discussion #293 with "extract" verdict).
  - ~~#300~~ LibraryService — shipped as #326 (ctor 17 → 8 via `LibraryServiceConfig`; no I/O extraction — Waves 1+2 had already removed all violations).
- **Wave 4 — Close-out** — **complete**
  - ~~#274~~ shipped as #328 + #329 + #330 + #331 (callable thinness audit)
  - ~~#277~~ closed: all 11 Cosmic Python compliance items ticked. Final prereqs in #333 (`RomFileAdapter` for `RomRemovalService` raw I/O, `FirmwareServiceConfig` ctor decomposition, `check_cosmic_call_bans.sh` false-positive fix).

**Saves vertical** ([~~#254~~](https://github.com/danielcopper/decky-romm-sync/issues/254)) — **complete**. Five sub-issues shipped after Wave 4: ~~#272~~ (helpers → domain, #336), ~~#242~~ (`SaveFileAdapter` for local I/O, #337), ~~#307~~ (peer-inject sub-services + collapse SyncEngine forwarder, #338), ~~#273~~ (typed `SaveSyncState` aggregate, #339), ~~#275~~ (`test_saves.py` split per-sub-service, #340). Saves package: zero raw I/O leaks, typed aggregate over the on-disk state, 6 focused test files mirroring the source layout.

**Why the order chosen**: doing #294 (Clock/UuidGen/Sleeper) before any per-service vertical meant every later PR was "drop the import, inject the Protocol" — mechanical. Doing #295 (domain extraction) before LibraryService shrunk the scariest service before lifting it. LibraryService last because it had the largest blast radius — by the time it was lifted, only ctor decomposition remained.

The Cosmic Python migration is complete (modulo deferred #259 — SonarCloud arch rules, blocked on SonarCloud Python support). Wave 3 sister-PR patterns (Protocol + adapter + `FakeXxxAdapter` in conftest + `*ServiceConfig` decomposition) remain the canonical reference for any future service-level work.

**Sub-issue policy**: Epic bodies do **not** carry markdown sub-issue lists — open work is tracked via GitHub's native Sub-Issues panel on each epic. If a new sub-issue is needed, link it natively (don't add a body bullet).

## Subfolder layout — when a subfolder is justified

Layer top-level folders (`services/`, `adapters/`, `domain/`, `lib/`, `models/`) are flat by default — one file per concept. A subfolder is justified **only when the modules within share an internal type, helper, or state**, not when they share a brand-name prefix.

- `adapters/romm/` qualifies: `http.py` is the internal HTTP transport for `romm_api.py`; the two share types and only `romm_api.py` is the public surface.
- `services/saves/` qualifies: facade + sub-services (`sync_engine/`, `slots/`, `status/`, `versions.py`) share a `SaveSyncState` aggregate.
- `adapters/retroarch/` would NOT qualify: `retroarch_config.py` (RetroArch.cfg reader) and `retroarch_core_info.py` (core lookup) share nothing but a brand name. False cohesion.
- `adapters/steam/` would NOT qualify: would mix Steam (`steam_config.py`) with SteamGridDB (`steamgriddb.py`, `sgdb_artwork_cache.py`) — different vendor, different concern.

When a service-level decomposition produces sub-services with shared state (e.g. a future `services/library/` decomposition with shared preview-delta state), a subfolder is the right home. Until then, file-level layout is the default.

## Sub-package `__init__.py` — when populated, when empty

Decision rule by how the package is consumed:

- **Top-level layer namespace** (`adapters/`, `services/`, `domain/`, `lib/`, `models/`): `__init__.py` is empty (a docstring is acceptable but not required). These exist as namespace markers; consumers always deep-import (`from adapters.romm.romm_api import RommApiAdapter`).
- **Sub-package consumed via package import** (consumers write `from package import X`): `__init__.py` holds the package's contract-style module docstring, re-exports of the public class(es), and optional `__all__`. Examples: `services/saves/`, `services/saves/sync_engine/`, `services/saves/slots/`, `services/saves/status/`.
- **Sub-package only consumed via deep-import** (consumers always write `from package.module import X`): empty or just docstring, no re-exports. Example: `adapters/romm/` — `bootstrap` deep-imports `from adapters.romm.romm_api import RommApiAdapter`.

Implementation never lives in `__init__.py`. Don't put 500+ LOC class definitions there — that obscures the package's public surface and breaks the "init = namespace marker + re-export" Python convention.

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

**Module and class docstrings** describe **what belongs here** (the contract), not what's currently in the file/class (the behavior). Behavior listings and method enumerations rot when methods get added/changed/removed; contracts don't.

- Bad (module): `"""Version history listing and rollback flow. 1. Download. 2. PUT. 3. confirm_download."""`
- Good (module): `"""Save version history reads and the destructive version-switch flow. Anything that lists, fetches, or rolls back to an older save version lives here. Mutations of the active save record outside the rollback flow belong in SyncEngine or StatusService, not here."""`
- Bad (class): `"""Owns save_sync_state.json — persistence, migrations, default construction."""` (rots when a 4th responsibility is added)
- Good (class): `"""Owns save_sync_state.json — single source of truth for on-disk save-sync state."""`

**Method docstrings are different.** A method docstring describes one specific contract (this method's behavior, parameters, return value, non-obvious how) — that contract is naturally scoped, so describing behavior is fine and stays in sync with the signature. Numpy-style parameter sections on a class's `__init__` count as method-like for this purpose.

Avoid all of: "mechanical extraction from X", "during the transition", "moved from Y", "added for the Z flow", "see PR #123" — that's commit-message content that rots in source.

## Testing

Every backend feature or callable where testing makes sense MUST have unit tests. Cover:

- **Happy path**: Normal successful operation
- **Bad path**: Invalid input, missing data, API errors, network failures
- **Edge cases**: Empty strings, None values, masked values ("••••"), boundary conditions

Tests mirror the source structure: `tests/services/`, `tests/adapters/`, `tests/domain/`, `tests/models/`, `tests/lib/`. Each test file maps 1:1 to a source module. Shared mocks live in `tests/conftest.py`.

### Frontend component tests — `@decky/api` event harness

`src/test-utils/decky-api-mock.ts` exposes an in-memory event bus that `addEventListener` / `removeEventListener` route through (wired in `src/test-setup.ts`). Tests dispatch backend events via `emitDeckyEvent` instead of mocking `@decky/api` per-file. `src/components/CustomPlayButton.test.tsx` is the reference shape:

```tsx
import { emitDeckyEvent } from "../test-utils/decky-api-mock";

act(() => {
  emitDeckyEvent<[DownloadFailedEvent]>("download_failed", { rom_id: 42, ... });
});
await findByText("Download"); // assert visible side effect
```

The bus is reset between tests by `afterEach` in `test-setup.ts`. Use `deckyEventListenerCount(name)` to assert that `useEffect` cleanup ran on unmount. DOM-level `globalThis.dispatchEvent(new CustomEvent(...))` flows (e.g. `romm_data_changed`) bypass the harness — happy-dom handles them natively.

Prefer the harness over extracting listener bodies into `src/utils/*.ts` purely for testability. Helper extraction stays valid for genuinely-reusable logic.

**Catch coverage assertions must be non-vacuous.** Tests that claim `.catch` coverage MUST assert the post-catch state — the fallback return value, the toast body, the `debugLog` message, the surfaced status string. Asserting only that the rejecting call was invoked is vacuous: it passes with or without the `.catch` because the rejection happens after the call returns. If you can't observe the catch's side effect, the catch either needs an observable effect or the test isn't earning its coverage.

## Security

- NEVER read or use credentials from settings files (`~/homebrew/settings/`) without explicit user permission
- NEVER pass credentials to agents — if API calls are needed, ask the user to run them and provide output
- NEVER log secrets (passwords, API keys) — mask them in any log output

## Working Style

- **Research before implementing.** When encountering an unknown (e.g. how a third-party tool works, where files are stored, what APIs exist), STOP and research first. Do not start writing code based on assumptions. Present findings to the user and agree on an approach before any implementation.
- **Discuss architecture decisions.** This is not a vibe coding project. Non-trivial changes require discussion before code is written. When you find a problem, explain it and propose options — don't just start fixing.
- **Use team-swarm agents** for everything beyond trivial single-file edits — including research, exploration, and implementation. Keep main context clean and focused on architecture and coordination by delegating to agents.
- **Sequential agent discipline.** When running agents sequentially, each agent's prompt MUST include: "When done, report back and wait for shutdown. Do NOT pick up other tasks from the task list." This prevents agents from grabbing the next unblocked task before the lead can shut them down and spawn a dedicated agent.
- **Preserve context.** Avoid back-and-forth code changes in the main conversation. Get alignment first, then implement cleanly in one pass (via agents).
- Refer to the [GitHub Projects board](https://github.com/users/danielcopper/projects/2) for the roadmap.
