# decky-romm-sync — Decky Loader Plugin

## What This Is

A Decky Loader plugin that syncs a self-hosted RomM library into Steam as Non-Steam shortcuts. Games launch via RetroDECK. The QAM panel handles settings, sync, downloads, and BIOS management.

## Documentation

The **GitHub Wiki** is the canonical source for architecture, file structure, and feature documentation. The wiki repo is checked out at `../decky-romm-sync.wiki/`. When making changes that affect architecture, data flows, or feature behavior, update the relevant wiki pages too.

## Key Technical Constraints

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart; SteamClient API is instant.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT `ServerAPI.callPluginMethod()`).
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode). Paginated: `{"items": [...], "total": N}`.
- **AddShortcut timing**: Must wait 300-500ms after `AddShortcut()` before setting properties. Use 50ms delay between operations.
- **Large payloads**: Never send bulk base64 data through `decky.emit()` — WebSocket bridge has size limits. Use per-item callables instead.
- **SteamGridDB**: Requires `User-Agent` header — Python's default `Python-urllib` gets 403'd. Use `decky-romm-sync/0.1`.
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

Backend layout: `services/` (orchestration) / `adapters/` (I/O) / `domain/` (pure compute) / `lib/` (cross-cutting utilities) / `models/` (data shapes). `import-linter` enforces direction.

**Services**:

- Depend on Protocols (defined in `services/protocols.py`), never on concrete adapter classes.
- No raw I/O. Forbidden in `services/`: `os.*` (except pure path algebra: `realpath`, `relpath`, `join`, `splitext`, `basename`, `dirname`), `open(...)`, `pathlib.Path(...).read_*` / `write_*`, `fcntl.*`, `urllib.*`, `shutil.*`, `subprocess.*`, `hashlib.<x>(open(...))`.
- No clocks or randomness: no `time.time()`, `time.monotonic()`, `datetime.now()`, `uuid.uuid4()`, `asyncio.sleep()`, `random.*` directly. Inject `Clock` / `UuidGen` / `Sleeper` Protocols.
- No service-to-service concrete imports — services are independent. Cross-service deps are Protocol-typed.
- Module functions from `domain/` are still a coupling — if tests need `patch("services.X.module_name.fn")`, wrap the module behind a Protocol and inject it.
- God-class signal: services > ~600 LOC or `__init__` > 6 params (S107) — decompose into sub-services with constructor injection (see `services/saves/` for the reference pattern).

**Adapters**: own all I/O. Never import from `services/`. Implement Protocols defined in `services/protocols.py`.

**Domain**: pure compute only. No I/O, no state mutation, no service or adapter imports. Functions take inputs, return outputs. Anything stateless and I/O-free that's currently in a service belongs here.

**Bootstrap (`bootstrap.py`)**: the only place where concrete adapters meet services. `WiringConfig` holds the wiring; protocols come in, services come out.

If a refactor breaks one of these rules, that's a Cosmic Python regression — call it out and fix it in the same PR or open a follow-up.

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
  - ~~#297~~ DownloadService — shipped as #323 (`DownloadFileAdapter` for filesystem + `DownloadQueueAdapter` for fcntl-locked queue; ZIP-slip protection; ctor 13 → 5 via `DownloadServiceConfig`).
  - ~~#298~~ FirmwareService — shipped as #324 (`FirmwareFileAdapter` with `checksum_md5` using `usedforsecurity=False`; closed #170 — `_enrich_firmware_file` returns new dict).
  - ~~#301~~ GameDetailService — closed as superseded. All scope (Clock + CoreInfoProvider) wired in Wave 1.
  - ~~#302~~ MigrationService — shipped as #325 (`MigrationFileAdapter` with cross-device `move` (`shutil.move`) vs same-fs `rename` (`os.replace`) distinction; ctor 13 → 2 via `MigrationServiceConfig`; closed discussion #293 with "extract" verdict).
  - ~~#300~~ LibraryService — shipped as #326 (ctor 17 → 8 via `LibraryServiceConfig`; no I/O extraction — Waves 1+2 had already removed all violations).
- **Wave 4 — Close-out** — **complete**
  - ~~#274~~ shipped as #328 + #329 + #330 + #331 (callable thinness audit)
  - ~~#277~~ closed: all 11 Cosmic Python compliance items ticked. Final prereqs in #333 (`RomFileAdapter` for `RomRemovalService` raw I/O, `FirmwareServiceConfig` ctor decomposition, `check_cosmic_call_bans.sh` false-positive fix).

**Saves vertical** ([#254](https://github.com/danielcopper/decky-romm-sync/issues/254)) runs in parallel — independent of the waves above.

**Why the order chosen**: doing #294 (Clock/UuidGen/Sleeper) before any per-service vertical meant every later PR was "drop the import, inject the Protocol" — mechanical. Doing #295 (domain extraction) before LibraryService shrunk the scariest service before lifting it. LibraryService last because it had the largest blast radius — by the time it was lifted, only ctor decomposition remained.

The Cosmic Python migration is complete (modulo deferred #259 — SonarCloud arch rules, blocked on SonarCloud Python support). Wave 3 sister-PR patterns (Protocol + adapter + `FakeXxxAdapter` in conftest + `*ServiceConfig` decomposition) remain the canonical reference for any future service-level work.

**Sub-issue policy**: Epic bodies do **not** carry markdown sub-issue lists — open work is tracked via GitHub's native Sub-Issues panel on each epic. If a new sub-issue is needed, link it natively (don't add a body bullet).

## Sub-package layout — `__init__.py` is re-export only

For sub-packages (e.g. `services/saves/status/`, `services/saves/sync_engine/`), `__init__.py` holds only:

1. The package's contract-style module docstring (describing what belongs in the package)
2. Re-exports of the public class(es) from named implementation modules
3. Optional `__all__`

Implementation lives in named modules (`engine.py`, `service.py`, `builders.py`, etc.). Don't put 500+ LOC class definitions directly in `__init__.py` — that obscures the package's public surface and breaks the "init = namespace marker + re-export" Python convention.

Example:

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

**Top-level package `__init__.py`** (e.g. `services/saves/__init__.py`) may contain primary code because it IS the public API of the parent package — that's where `SaveService` lives.

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
