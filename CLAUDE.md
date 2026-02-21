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

- **Backend** (`main.py` + `lib/`): RomM API, SteamGridDB API, ROM/BIOS/artwork downloads, state persistence
- **Frontend** (`src/`): SteamClient shortcut CRUD, QAM panel UI, game detail page injection
- **Communication**: `callable()` for request/response, `decky.emit()` for backend-to-frontend events

## Key Technical Constraints

- **Shortcuts**: Use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart; SteamClient API is instant.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT `ServerAPI.callPluginMethod()`).
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode). Paginated: `{"items": [...], "total": N}`.
- **AddShortcut timing**: Must wait 300-500ms after `AddShortcut()` before setting properties. Use 50ms delay between operations.
- **Large payloads**: Never send bulk base64 data through `decky.emit()` — WebSocket bridge has size limits. Use per-item callables instead.
- **SteamGridDB**: Requires `User-Agent` header — Python's default `Python-urllib` gets 403'd. Use `decky-romm-sync/0.1`.
- **AddShortcut ignores most params**: `SteamClient.Apps.AddShortcut(name, exe, startDir, launchOptions)` ignores startDir and launchOptions (confirmed by MoonDeck plugin). Must use `Set*` calls (`SetShortcutName`, `SetShortcutExe`, `SetShortcutStartDir`, `SetAppLaunchOptions`) after a 500ms delay. Do NOT pass quoted exe paths — the API handles quoting internally.
- **BIsModOrShortcut bypass counter**: Patching `BIsModOrShortcut()` to return false makes metadata display but BREAKS game launches (Steam skips the shortcut launch path). Must use the MetaDeck bypass counter pattern: default state returns false (metadata shows), temporarily returns true during launch via counter hooks on `GetGameID`, `GetPrimaryAppID`, `BHasRecentlyLaunched`, `GetPerClientData`. See `src/patches/metadataPatches.ts`.
- **Shortcut property re-sync**: Changing exe, startDir, or launchOptions on existing shortcuts may not take effect reliably. Full delete + recreate (re-sync) is required for changes to launch config.
- **RomM 4.6.1 Save API**: `GET /api/saves/{id}/content` does not exist — use `download_path` from save metadata (URL-encode spaces/parens). No `content_hash` in SaveSchema — use hybrid timestamp + download-and-hash. `POST /api/saves` upserts by filename. `GET /api/roms/{id}/notes` returns 500 — read `all_user_notes` from ROM detail instead. `device_id` param is accepted but ignored. See `.romm-api-verified.md` for full details.

## File Structure

```
main.py                              # Plugin entry point, composes mixin classes from lib/
lib/                                 # Backend mixin modules (state, sync, downloads, etc.)
lib/save_sync.py                     # Save sync backend (device registration, upload/download, conflict detection)
src/index.tsx                        # Plugin entry, event listeners, QAM router
src/components/MainPage.tsx          # Status, sync button, navigation
src/components/ConnectionSettings.tsx # RomM connection, SGDB API key, controller settings
src/components/PlatformSync.tsx      # Per-platform enable/disable toggles
src/components/DangerZone.tsx        # Per-platform and bulk removal
src/components/DownloadQueue.tsx     # Active/completed downloads
src/components/BiosManager.tsx       # Per-platform BIOS file status and downloads
src/components/CustomPlayButton.tsx  # Custom Play/Download button with dropdown menu (replaces native PlaySection for RomM games)
src/components/SaveSyncSettings.tsx  # Save sync settings QAM page
src/patches/gameDetailPatch.tsx      # Route patch for /library/app/:appid
src/patches/metadataPatches.ts       # BIsModOrShortcut bypass counter for metadata display + launch
src/api/backend.ts                   # callable() wrappers (typed)
src/types/index.ts                   # Shared TypeScript interfaces
src/types/steam.d.ts                 # SteamClient/collectionStore/appStore type declarations
src/utils/steamShortcuts.ts          # addShortcut, removeShortcut, getExistingRomMShortcuts
src/utils/syncManager.ts             # Listens for sync_apply, orchestrates shortcut creation
src/utils/syncProgress.ts            # Module-level sync progress store
src/utils/downloadStore.ts           # Module-level download state store
src/utils/collections.ts             # Steam collection management
src/utils/sessionManager.ts          # Game session detection and playtime tracking
bin/romm-launcher                    # Bash launcher for RetroDECK
defaults/config.json                 # 149 platform slug -> RetroDECK system mappings
tests/test_*.py                      # Per-module backend tests (164 tests)
tests/test_save_sync.py              # Save sync backend tests
tests/conftest.py                    # Mock decky module for test isolation
```

## Current State

**Latest release**: v0.2.1 on main

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

In progress (Phase 5):
- Bidirectional save file sync (RetroArch .srm saves)
- Three-way conflict detection with 4 resolution modes
- Game session detection and playtime tracking
- Save sync settings QAM page

See PLAN.md for the full roadmap (Phases 1-4.5 done, Phase 5 in progress, 6-8 planned).

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

Tests are split per module in `tests/test_*.py` with shared mocks in `tests/conftest.py`.

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
- Refer to PLAN.md for the full phase roadmap.
