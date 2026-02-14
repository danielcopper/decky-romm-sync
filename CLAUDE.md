# romm-library — Decky Loader Plugin

## What This Is

A Decky Loader plugin that connects to a self-hosted RomM server and makes all ROM games appear as Non-Steam shortcuts in the Steam Library. Games launch via RetroDECK on a Bazzite HTPC.

## Architecture

```
RomM Server (http://192.168.178.83:8085/) <-HTTP-> Python Backend (main.py)
                                                      | callable() / emit()
                                            Frontend (TypeScript) <-> SteamClient.Apps API
                                                      |
                                                 Steam Library (shortcuts appear instantly)
                                                      |
                                                 bin/romm-launcher (bash)
                                                      |
                                                 RetroDECK (flatpak)
```

- **Backend** (`main.py`): RomM API calls, ROM/artwork downloads, state persistence, file I/O
- **Frontend** (`src/`): SteamClient shortcut CRUD, collections, QAM panel UI
- **Communication**: `callable()` for request/response, `decky.emit()` for backend-to-frontend events

## Key Technical Constraints

- **Shortcuts**: Must use `SteamClient.Apps.AddShortcut()` from frontend JS, NOT VDF writes. VDF edits require Steam restart; SteamClient API is instant.
- **Frontend API**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`). Use `callable()` (NOT `ServerAPI.callPluginMethod()`).
- **RomM API quirks**: Filter param is `platform_ids` (plural). Cover URLs have unencoded spaces (must URL-encode). Paginated: `{"items": [...], "total": N}`.
- **AddShortcut timing**: Must wait 300-500ms after `AddShortcut()` before setting properties. Use 50ms delay between operations to avoid corrupting Steam state.
- **Large payloads**: Never send bulk base64 data through `decky.emit()` — WebSocket bridge has size limits. Use per-item callables instead.

## Sync Flow

1. Backend `_do_sync()` fetches ROMs per enabled platform, downloads artwork to staging files
2. Backend emits `sync_apply` event with shortcut data (name, exe, launch_options, platform)
3. Frontend `syncManager.ts` receives event, creates shortcuts via `SteamClient.Apps.AddShortcut()`
4. Frontend calls `reportSyncResults()` with `{rom_id: steam_app_id}` mapping
5. Backend renames artwork staging files to `{steam_app_id}p.png`, updates registry, emits `sync_complete`
6. Frontend shows toast notification

## File Structure

```
main.py                          # Python backend (all RomM API, downloads, state)
src/index.tsx                    # Plugin entry, event listeners, QAM router
src/components/MainPage.tsx      # Status, sync button, navigation
src/components/ConnectionSettings.tsx
src/components/PlatformSync.tsx  # Per-platform enable/disable toggles
src/components/DangerZone.tsx    # Per-platform and bulk removal
src/api/backend.ts               # callable() wrappers
src/types/index.ts               # Shared TypeScript interfaces
src/types/steam.d.ts             # SteamClient/collectionStore type declarations
src/utils/steamShortcuts.ts      # addShortcut, removeShortcut, getExistingRomMShortcuts
src/utils/syncManager.ts         # Listens for sync_apply, orchestrates shortcut creation
src/utils/collections.ts         # Steam collection management (NEEDS REWRITE)
bin/romm-launcher                # Bash launcher for RetroDECK
defaults/config.json             # 149 platform slug -> RetroDECK system mappings
tests/test_main.py               # 45 unit tests for backend
```

## Current State

**Branch**: `feat/phase-2-sync-shortcuts`
**Phase 2 (Sync + Steam Shortcuts)**: In progress — see PLAN.md for detailed status.
- Sync and delete: Working
- Spinner/progress UI: Working
- Artwork display: Broken (needs per-item callable instead of bulk base64 event)
- Collections: Broken (wrong API — needs rewrite using correct collectionStore methods)

Both broken items have complete research findings and fix instructions in PLAN.md under Phase 2 "Remaining" sections.

## Environment

- **Distrobox container `dev`** (Fedora 43) on Bazzite HTPC. Home dir shared with host.
- **SSH for git push**: `SSH_AUTH_SOCK=/tmp/ssh-XXXXXXmfc8dP/agent.59410 git push` — socket path may change between sessions. If push fails, find the socket with `find /tmp -name "agent.*" -type s` and ask Daniel to run `ssh-add ~/.ssh/id_ed25519_github`.
- **RomM server**: http://192.168.178.83:8085/ (creds: romm-library / asdf123) on Unraid NAS
- **Tests**: `python -m pytest tests/ -q`
- **Build**: `pnpm build` (Rollup -> dist/index.js)
- **Project root owned by `nobody`** (Distrobox UID mapping) — git works via `safe.directory`, but new files in root may need ownership fix.

## Working Style

Daniel is product owner. Claude leads agent orchestration using team-swarm mode (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`). Use Explore agents for research, general-purpose agents for implementation. Keep main context clean by delegating to agents. Refer to PLAN.md for the full 6-phase roadmap.
