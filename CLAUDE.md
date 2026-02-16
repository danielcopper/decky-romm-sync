# decky-romm-sync — Decky Loader Plugin

## What This Is

A Decky Loader plugin that syncs a self-hosted RomM library into Steam as Non-Steam shortcuts. Games launch via RetroDECK. The QAM panel handles settings, sync, downloads, and BIOS management.

## Architecture

```
RomM Server <-HTTP-> Python Backend (main.py)
                          | callable() / emit()
                   Frontend (TypeScript) <-> SteamClient.Apps API
                          |
                     Steam Library (shortcuts appear instantly)
                          |
                     bin/romm-launcher (bash) -> RetroDECK (flatpak)
```

- **Backend** (`main.py`): RomM API, SteamGridDB API, ROM/BIOS/artwork downloads, state persistence
- **Frontend** (`src/`): SteamClient shortcut CRUD, QAM panel UI, game detail page injection
- **Communication**: `callable()` for request/response, `decky.emit()` for backend-to-frontend events

## Key Technical Constraints

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart; SteamClient API is instant.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT `ServerAPI.callPluginMethod()`).
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode). Paginated: `{"items": [...], "total": N}`.
- **AddShortcut timing**: Must wait 300-500ms after `AddShortcut()` before setting properties. Use 50ms delay between operations.
- **Large payloads**: Never send bulk base64 data through `decky.emit()` — WebSocket bridge has size limits. Use per-item callables instead.
- **SteamGridDB**: Requires `User-Agent` header — Python's default `Python-urllib` gets 403'd. Use `decky-romm-sync/0.1`.

## File Structure

```
main.py                              # Python backend (RomM API, SGDB, downloads, state)
src/index.tsx                        # Plugin entry, event listeners, QAM router
src/components/MainPage.tsx          # Status, sync button, navigation
src/components/ConnectionSettings.tsx # RomM connection, SGDB API key, controller settings
src/components/PlatformSync.tsx      # Per-platform enable/disable toggles
src/components/DangerZone.tsx        # Per-platform and bulk removal
src/components/DownloadQueue.tsx     # Active/completed downloads
src/components/BiosManager.tsx       # Per-platform BIOS file status and downloads
src/components/GameDetailPanel.tsx   # Injected into game detail page (download, artwork, BIOS)
src/patches/gameDetailPatch.tsx      # Route patch for /library/app/:appid
src/api/backend.ts                   # callable() wrappers (typed)
src/types/index.ts                   # Shared TypeScript interfaces
src/types/steam.d.ts                 # SteamClient/collectionStore/appStore type declarations
src/utils/steamShortcuts.ts          # addShortcut, removeShortcut, getExistingRomMShortcuts
src/utils/syncManager.ts             # Listens for sync_apply, orchestrates shortcut creation
src/utils/syncProgress.ts            # Module-level sync progress store
src/utils/downloadStore.ts           # Module-level download state store
src/utils/collections.ts             # Steam collection management
bin/romm-launcher                    # Bash launcher for RetroDECK
defaults/config.json                 # 149 platform slug -> RetroDECK system mappings
tests/test_main.py                   # Backend unit tests (110 tests)
tests/conftest.py                    # Mock decky module for test isolation
```

## Current State

**Latest release**: v0.1.6 on main
**Active branch**: `feat/phase-4a-artwork` — SteamGridDB artwork integration

Working:
- Full sync engine (fetch ROMs, create shortcuts, apply cover art)
- On-demand ROM downloads with progress tracking
- BIOS file management per platform
- Game detail page injection (download/uninstall, BIOS status, artwork refresh)
- SteamGridDB artwork (hero, logo, wide grid) — on-demand from game detail page
- SGDB API key management with verify button
- Per-platform sync toggles, per-platform removal
- Steam collections
- Toast notifications

See PLAN.md for the full roadmap (Phases 1-3 done, 4A in progress, 4B-8 planned).

## Development

- **Build**: `pnpm build` (Rollup -> dist/index.js)
- **Tests**: `python -m pytest tests/ -q` or `mise run test`
- **Setup**: `mise run setup` (installs JS + Python dependencies)
- **Dev reload**: `mise run dev` (build + restart plugin_loader)
- **Tooling**: mise manages node, pnpm, python. Venv auto-activates via `_.python.venv` in mise.toml.

## Testing

Every backend feature or callable where testing makes sense MUST have unit tests. Cover:
- **Happy path**: Normal successful operation
- **Bad path**: Invalid input, missing data, API errors, network failures
- **Edge cases**: Empty strings, None values, masked values ("••••"), boundary conditions

Tests live in `tests/test_main.py` with mocks in `tests/conftest.py`.

## Security

- NEVER read or use credentials from settings files (`~/homebrew/settings/`) without explicit user permission
- NEVER pass credentials to agents — if API calls are needed, ask the user to run them and provide output
- NEVER log secrets (passwords, API keys) — mask them in any log output

## Working Style

Use team-swarm agents for everything beyond trivial single-file edits — including research, exploration, and implementation. Keep main context clean by delegating to agents. Refer to PLAN.md for the full phase roadmap.
