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
- **BIsModOrShortcut bypass DROPPED**: Phase 5.6 removed the bypass counter entirely. Shortcuts return `BIsModOrShortcut() = true` (natural state). We own the entire game detail UI via RomMPlaySection + future RomMGameInfoPanel. See `docs/game-detail-ui.md` section 2 for the rationale.
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
