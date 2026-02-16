# decky-romm-sync

Decky Loader plugin that syncs a RomM server's game library to Steam as non-steam shortcuts, launched via RetroDECK.

## Architecture

- **Backend** (`main.py`): Python — RomM API calls, ROM/BIOS downloads, VDF manipulation, state persistence
- **Frontend** (`src/`): TypeScript — SteamClient shortcut CRUD, QAM panel UI, game detail page patch
- **Communication**: `callable()` for request/response, `decky.emit()` for backend-to-frontend events
- **Launcher** (`bin/romm-launcher`): Bash script called by Steam shortcuts, launches RetroDECK

## File Structure

```
main.py                              # All backend logic (single file)
src/index.tsx                        # Plugin entry, event listeners, QAM router
src/components/MainPage.tsx          # Status, sync button, navigation
src/components/ConnectionSettings.tsx # Server URL, credentials, Steam Input, RetroArch diagnostic
src/components/PlatformSync.tsx      # Per-platform enable/disable toggles
src/components/GameDetailPanel.tsx   # Patched onto each game's Steam detail page
src/components/BiosManager.tsx       # BIOS/firmware download UI
src/components/DangerZone.tsx        # Per-platform and bulk shortcut removal
src/api/backend.ts                   # callable() wrappers
src/types/index.ts                   # Shared TypeScript interfaces
src/utils/steamShortcuts.ts          # addShortcut, removeShortcut helpers
src/utils/syncManager.ts             # Listens for sync_apply event, orchestrates shortcut creation
bin/romm-launcher                    # Bash launcher script
defaults/config.json                 # Platform slug → RetroDECK system mappings
```

## Sync Flow

1. Backend `_do_sync()` fetches ROMs per enabled platform, downloads artwork
2. Backend emits `sync_apply` with shortcut data
3. Frontend `syncManager.ts` receives event, creates shortcuts via `SteamClient.Apps.AddShortcut()`
4. Frontend calls `reportSyncResults()` with `{rom_id: steam_app_id}` mapping
5. Backend renames artwork files to `{steam_app_id}p.png`, updates registry, emits `sync_complete`

## Key Constraints

- Shortcuts must use `SteamClient.Apps.AddShortcut()` from frontend JS, not VDF writes (VDF needs Steam restart, API is instant)
- Use `@decky/api` callable() — not the deprecated `ServerAPI.callPluginMethod()`
- Wait 300-500ms after `AddShortcut()` before setting properties; 50ms between operations
- Never send bulk base64 through `decky.emit()` — WebSocket has size limits
- RomM API: filter param is `platform_ids` (plural), cover URLs have unencoded spaces, paginated responses

## State Files

All under Decky's data/settings dirs (`~/homebrew/data/decky-romm-sync/` and `~/homebrew/settings/decky-romm-sync/`):
- `settings.json`: Server URL, credentials, enabled platforms, steam input mode
- `state.json`: Shortcut registry (rom_id→app_id), installed ROMs, sync stats
- `download_requests.json`: Written by launcher when ROM isn't installed, polled by backend

## Build & Test

```bash
pnpm install && pnpm build    # Frontend (Rollup -> dist/index.js)
python -m pytest tests/ -q    # Backend (110+ tests)
```

## Git Workflow

- `main` branch is protected — all changes need PRs
- release-please manages versions and changelogs via conventional commits
- CI builds plugin zip with Decky CLI and attaches to GitHub releases

## Environment

- Development runs in a Distrobox container (Fedora). Project root may be owned by `nobody` due to UID mapping — fix with `sudo chown`.
- SSH agent socket path changes between reboots. If git push fails, find it with `find /tmp /run -name "agent.*" -o -name "ssh-agent*" -type s`.
