# decky-romm-sync — Decky Loader Plugin

## What This Is

A Decky Loader plugin that syncs a self-hosted RomM library into Steam as Non-Steam shortcuts. Games launch via RetroDECK. The QAM panel handles settings, sync, downloads, and BIOS management.

## Architecture

```
RomM Server <-HTTP-> Python Backend (main.py + services/ + adapters/)
                          | callable() / emit()
                   Frontend (TypeScript) <-> SteamClient.Apps API
                          |
                     Steam Library (shortcuts appear instantly)
                          |
                     bin/romm-launcher (bash) -> RetroDECK (flatpak)
```

**Backend layers** (dependency direction: services → protocols ← adapters):
- **`main.py`** — Plugin entry point, callable routing, composition root
- **`py_modules/services/`** — Business logic (9 services, depend on Protocols only)
- **`py_modules/adapters/`** — I/O boundaries (HTTP, persistence, Steam VDF, save API)
- **`py_modules/domain/`** — Domain logic (ES-DE config, RetroDECK path resolution)
- **`py_modules/models/`** — Domain dataclasses
- **`py_modules/lib/`** — Utilities (errors, certifi bundle)

**Frontend** (`src/`): SteamClient shortcut CRUD, QAM panel UI, game detail page injection

**Communication**: `callable()` for request/response, `decky.emit()` for backend-to-frontend events

**Layer rules** (enforced by import-linter in CI):
- Services must not import concrete adapter implementations (Protocols OK)
- Adapters must not import services
- Domain must not import services, adapters, or lib
- Utilities must not import services, adapters, or domain
- Services must be independent of each other

## Key Technical Constraints

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart; SteamClient API is instant.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT `ServerAPI.callPluginMethod()`).
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode). Paginated: `{"items": [...], "total": N}`.
- **AddShortcut timing**: Must wait 300-500ms after `AddShortcut()` before setting properties. Use 50ms delay between operations.
- **Large payloads**: Never send bulk base64 data through `decky.emit()` — WebSocket bridge has size limits. Use per-item callables instead.
- **SteamGridDB**: Requires `User-Agent` header — Python's default `Python-urllib` gets 403'd. Use `decky-romm-sync/0.1`.
- **AddShortcut ignores most params**: `SteamClient.Apps.AddShortcut(name, exe, startDir, launchOptions)` ignores startDir and launchOptions (confirmed by MoonDeck plugin). Must use `Set*` calls (`SetShortcutName`, `SetShortcutExe`, `SetShortcutStartDir`, `SetAppLaunchOptions`) after a 500ms delay. Do NOT pass quoted exe paths — the API handles quoting internally.
- **BIsModOrShortcut bypass DROPPED**: Phase 5.6 removed the bypass counter entirely. Shortcuts return `BIsModOrShortcut() = true` (natural state). We own the entire game detail UI via RomMPlaySection + future RomMGameInfoPanel. See `docs/game-detail-ui.md` section 2 for the rationale.
- **Shortcut property re-sync**: Changing exe, startDir, or launchOptions on existing shortcuts may not take effect reliably. Full delete + recreate (re-sync) is required for changes to launch config.
- **RomM 4.6.1 Save API**: `GET /api/saves/{id}/content` does not exist — use `download_path` from save metadata (URL-encode spaces/parens). No `content_hash` in SaveSchema — use hybrid timestamp + download-and-hash. `POST /api/saves` upserts by filename. `GET /api/roms/{id}/notes` returns 500 — read `all_user_notes` from ROM detail instead. `device_id` param is accepted but ignored. See wiki Save-File-Sync-Architecture for full details.
- **Decky callables must be async**: Even if the method body is synchronous, Decky's callable framework requires `async def`. Do not remove `async` from callable methods in main.py.

## File Structure

```
main.py                                   # Plugin entry point, callable routing, bootstrap
py_modules/
  bootstrap.py                            # Composition root — wires adapters and services
  services/
    protocols.py                          # Protocol interfaces (HttpAdapter, SteamConfigAdapter, SaveApiProtocol)
    library.py                            # LibraryService — fetch ROMs, create shortcuts, artwork staging
    saves.py                              # SaveService — upload/download .srm, conflict detection
    playtime.py                           # PlaytimeService — session recording, RomM notes
    downloads.py                          # DownloadService — ZIP extraction, M3U, queue, progress
    firmware.py                           # FirmwareService — registry, downloads, per-core filtering
    steamgrid.py                          # SteamGridService — SteamGridDB fetch, cache, icons
    metadata.py                           # MetadataService — ROM metadata caching (TTL, app_id mapping)
    achievements.py                       # AchievementsService — RetroAchievements progress, caching
    game_detail.py                        # GameDetailService — game detail page data aggregation
    migration.py                          # MigrationService — RetroDECK path change detection, file migration
    _util.py                              # Shared service utilities (run_api_sync)
  adapters/
    persistence.py                        # PersistenceAdapter — settings/state/cache JSON I/O (atomic writes)
    steam_config.py                       # SteamConfigAdapter — VDF read/write, grid dir, Steam Input
    retroarch_core_info.py                # RetroArchCoreInfoAdapter — reads RetroArch core .info files
    retrodeck_paths.py                    # RetroDeckPathsAdapter — reads retrodeck.json for path resolution
    retroarch_config.py                   # RetroArchConfigAdapter — reads retroarch.cfg for RetroArch runtime settings
    romm/
      http.py                             # RommHttpAdapter — HTTP client for RomM API
      version_router.py                   # VersionRouter — proxy selecting v46/v47 SaveApi by server version
      save_api/
        v46.py                            # SaveApiV46 — RomM 4.6.1 save API adapter
        v47.py                            # SaveApiV47 — RomM 4.7.0 save API adapter
  models/                                 # Domain dataclasses (currently empty — types inlined in services)
  domain/
    bios.py                               # BIOS status formatting and computation
    es_de_config.py                       # CoreResolver + GamelistXmlEditor classes (core resolution, gamelist.xml)
    retroarch_core_info.py                # Pure parser for RetroArch .info file format
    save_conflicts.py                     # Save file conflict detection and resolution logic
    state_migrations.py                   # Schema migration functions for state files
  lib/
    errors.py                             # Exception hierarchy (RommApiError, classify_error)
  vdf/                                    # Vendored VDF library (binary VDF read/write)
src/
  index.tsx                               # Plugin entry, event listeners, QAM router
  components/                             # React components (MainPage, ConnectionSettings, etc.)
  patches/                                # Steam UI patches (game detail page, metadata)
  api/backend.ts                          # callable() wrappers (typed)
  types/                                  # TypeScript interfaces + Steam type declarations
  utils/                                  # Shortcut management, sync, downloads, collections, sessions
bin/romm-launcher                         # Bash launcher for RetroDECK
defaults/config.json                      # 149 platform slug → RetroDECK system mappings
tests/
  conftest.py                             # Mock decky module, autouse fixture, test isolation
  test_plugin.py                          # Plugin callable tests (main.py)
  test_bootstrap.py                       # Bootstrap wiring tests
  test_plugin_saves.py                    # Plugin-level save sync integration tests
  fakes/                                  # Shared test doubles (FakeSaveApi)
  services/                              # Service unit tests (1:1 with py_modules/services/)
  adapters/                              # Adapter unit tests (1:1 with py_modules/adapters/)
  domain/                                # Domain unit tests (1:1 with py_modules/domain/)
  models/                                # Model unit tests (1:1 with py_modules/models/)
  lib/                                   # Lib unit tests (1:1 with py_modules/lib/)
```

## Current State

**Latest release**: v0.13.0 on main

Working:
- Full sync engine (fetch ROMs, create shortcuts, apply cover art)
- On-demand ROM downloads with progress tracking
- BIOS file management per platform with per-core annotations
- Game detail page injection (custom PlaySection, GameInfoPanel, metadata)
- SteamGridDB artwork (hero, logo, wide grid) — on-demand from game detail page
- SGDB API key management with verify button
- Per-platform sync toggles, per-platform removal
- Steam collections
- Toast notifications
- Bidirectional save file sync (RetroArch .srm saves)
- Three-way conflict detection with 4 resolution modes
- Game session detection and playtime tracking (via RomM notes)
- Save sync settings QAM page
- Per-platform and per-game core switching (ES-DE gamelist.xml integration)
- RetroDECK path migration (internal SSD ↔ SD card)
- Native Steam metadata display (descriptions, genres, release date, controller support)
- RetroAchievements integration with tabbed game detail page

Roadmap and open work tracked on the [GitHub Projects board](https://github.com/users/danielcopper/projects/2).

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
- **pytest-cov**: Branch coverage reported to SonarCloud.

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
