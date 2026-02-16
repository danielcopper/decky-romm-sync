# romm-library — Decky Loader Plugin Implementation Plan

## Context

Self-hosted RomM server manages a ROM collection on Unraid. Goal: browse and play the entire RomM library directly from Steam Gaming Mode on a Bazzite HTPC, with on-demand downloads and correct RetroDECK emulator launches. No existing tool does this — DeckRomMSync is alpha/full-sync-only, Playnite RomM plugin is Windows-only.

**Core concept**: ALL RomM games appear as Non-Steam shortcuts in the Steam Library. The Decky QAM panel is only for settings, sync, and download management. This follows the Unifideck pattern (GPL-3.0, compatible with our GPL-3.0 license).

## Key Decisions

- **Play on undownloaded game**: Launcher triggers background download via plugin backend, exits cleanly. User gets toast notification. Play again when done.
- **Emulator**: RetroDECK (`flatpak run net.retrodeck.retrodeck [-e emulator] [-s system] <rom>`). Supports per-system and per-game emulator override.
- **Shortcut creation**: Use `SteamClient.Apps.AddShortcut()` from the frontend (NOT direct VDF writes). Steam does not watch shortcuts.vdf — direct VDF edits require a restart. The SteamClient API creates shortcuts instantly in Steam's live state.
- **Frontend**: `@decky/ui` + `@decky/api` (NOT deprecated `decky-frontend-lib`)
- **Backend comms**: `callable()` from `@decky/api` (NOT `ServerAPI.callPluginMethod()`)
- **RomM Auth**: HTTP Basic Auth (OAuth2 Bearer as future option)
- **Download endpoint**: `GET /api/roms/{id}/content/{file_name}` — returns single file or ZIP for multi-file ROMs (includes M3U if applicable). Always download all files (no partial downloads).
- **RomM API quirks**: ROM filtering uses `platform_ids` (plural). Cover image URLs contain unencoded spaces (must URL-encode). Paginated responses use `{"items": [...], "total": N}` envelope.

## Environment & Git Setup

We are inside a **Distrobox container `dev`** (Fedora 43) on a Bazzite HTPC. The home directory is shared between host and container.

**SSH for git push**: The SSH agent runs on the host. Claude Code must set `SSH_AUTH_SOCK` for every git push/pull:
```bash
SSH_AUTH_SOCK=/tmp/ssh-XXXXXX7kJjym/agent.61196 git push
```
The socket path may change between sessions. If push fails with "Permission denied (publickey)", **ask Daniel to re-run `ssh-add`** in his terminal and provide the new socket path.

**When to ask Daniel (human) for help:**
- SSH agent expired or socket path changed → ask to `ssh-add` again
- `sudo dnf install` needed for new system packages → ask to run in terminal
- Testing in Gaming Mode → ask to switch to Gaming Mode and check
- RomM server credentials or URL → ask for connection details
- Any destructive action on Steam shortcuts or ROM files → confirm first

## Architecture

```
RomM Server ←HTTP→ Python Backend (main.py)
                        ↓ callable()
              Frontend (TypeScript) ←→ SteamClient.Apps API
                        ↓
                   Steam Library (shortcuts appear instantly)
                        ↓
                   bin/romm-launcher (bash)
                        ↓
                   RetroDECK (flatpak)
```

**Shortcut creation flow**: Python backend fetches ROM data from RomM → passes to frontend via callable() → frontend calls `SteamClient.Apps.AddShortcut()` → shortcut appears in Steam instantly. Python backend still handles: RomM API calls, ROM downloads, artwork downloads, state persistence, file I/O.

Every shortcut: `exe` → `bin/romm-launcher`, `LaunchOptions` → `romm:{rom_id}`. Launcher checks install status, either launches RetroDECK or requests download.

## RomM API Reference (key endpoints)

| Endpoint | Method | Notes |
|----------|--------|-------|
| `/api/heartbeat` | GET | Health check (no auth needed) |
| `/api/platforms` | GET | All platforms (plain list) |
| `/api/roms?platform_ids={id}&limit=50&offset=N` | GET | ROMs by platform (paginated envelope) |
| `/api/roms/{id}` | GET | Full ROM detail (includes `files`, `user_saves`, `user_states`, metadata) |
| `/api/roms/{id}/content/{file_name}` | GET | Download ROM (single file or ZIP for multi-file, includes M3U) |
| `/api/romsfiles/{file_id}/content/{file_name}` | GET | Download individual file from multi-file ROM |
| `/api/saves` | GET/POST | List/upload saves. Filter by `rom_id`, `platform_id`. Upload via multipart form (`saveFile`) |
| `/api/saves/{id}` | GET/PUT | Get/update specific save |
| `/api/saves/{id}/content` | GET | Download save file binary |
| `/api/saves/delete` | POST | Batch delete saves `{"saves": [id, ...]}` |
| `/api/states` | GET/POST | Same as saves but for emulator save states |
| `/api/states/{id}/content` | GET | Download state file binary |

## SteamClient.Apps API Reference (for shortcut management)

Available from the Decky frontend JS context. Shortcuts appear instantly without restart.

| Method | Signature | Notes |
|--------|-----------|-------|
| `AddShortcut` | `(appName, exe, startDir, launchOptions) → Promise<number>` | Creates shortcut, returns app ID |
| `RemoveShortcut` | `(appId) → void` | Removes shortcut instantly |
| `SetShortcutName` | `(appId, name) → void` | Sets display name |
| `SetShortcutExe` | `(appId, path) → void` | Sets executable |
| `SetShortcutStartDir` | `(appId, dir) → void` | Sets working directory |
| `SetShortcutLaunchOptions` | `(appId, options) → void` | Sets launch options |
| `SetCustomArtworkForApp` | `(appId, artworkPath, artworkType) → void` | Sets grid/hero/logo artwork |

## RetroDECK Integration Reference

**CLI**: `flatpak run net.retrodeck.retrodeck [-e emulator] [-s system] [-m] <game_path>`

**Emulator resolution order** (when no `-e` flag):
1. Per-game `<altemulator>` in `gamelist.xml`
2. Per-system `<alternativeEmulator>` in `gamelist.xml`
3. First `<command>` in `es_systems.xml` (the default)

**Systems with multiple emulators** (examples):
- PSX: SwanStation (default), Beetle PSX, Beetle PSX HW, PCSX ReARMed, DuckStation
- PS2: PCSX2 Standalone (default), LRPS2, PCSX2 RetroArch core
- Dreamcast: Flycast RetroArch (default)
- All disc-based systems support `.m3u` extension

**Save file locations** (RetroDECK):
- RetroArch saves: `~/retrodeck/saves/{system}/`
- RetroArch states: `~/retrodeck/states/{system}/`
- Standalone emulator saves vary by emulator

---

## Phase 1: Plugin Skeleton + Settings ✅ DONE

**Goal**: Plugin loads in Decky, QAM shows settings, can connect to RomM.

- Plugin manifest, build config, TypeScript frontend, Python backend
- Settings persistence, connection test, platform mapping (149 entries)
- Merged to main as v0.1.0

## Phase 2: Sync + Steam Shortcuts ✅ DONE

**Goal**: All RomM games appear in Steam Library with artwork and collections.

### Completed:
- SteamClient.Apps API migration (instant shortcuts, no Steam restart)
- sync_apply event flow: backend emits shortcut data → frontend creates via SteamClient → reports app_ids back
- Artwork download with URL-encoding fix for RomM's unencoded spaces
- Artwork staging pattern (romm_{rom_id}_cover.png → {app_id}p.png) survives remove+re-add
- Per-item artwork via `get_artwork_base64(rom_id)` callable (avoids bulk base64 WebSocket crash)
- Post-AddShortcut delay increased to 500ms for reliable artwork application
- Paginated ROM fetching with `platform_ids` parameter (fixed from `platform_id`)
- Per-platform sync toggle with enable/disable all
- Drill-down QAM navigation (Main → Connection / Platforms / Danger Zone)
- Per-platform removal in Danger Zone
- Library stats computed live from registry
- Sync progress spinner: stays visible during frontend processing, auto-clears after 8s
- State persistence (shortcut_registry, sync_stats)
- Collections via correct `collectionStore` API: `NewUnsavedCollection()` + `AsDragDropCollection().AddApps()` + `Save()`
- Collections use `appStore.GetAppOverviewByAppID()` for proper app overview objects
- 45 unit tests

### Remaining — Danger Zone: Remove All Non-Steam Games:

**Problem**: Leftover non-steam shortcuts from previous imports (not managed by our plugin) clutter the library. Current Danger Zone only removes RomM-managed shortcuts.

**Feature: "Remove All Non-Steam Games" button** in Danger Zone:
- Enumerates ALL non-steam shortcuts via `collectionStore.deckDesktopApps.apps`
- Confirmation dialog (must confirm twice for safety)
- **Whitelist/Exclude list**: Button that opens a dropdown/checklist of all current non-steam games. User checks items that should NOT be removed (e.g. RetroDECK, other manually added shortcuts). Whitelist persisted in plugin settings.
- Removal calls `SteamClient.Apps.RemoveShortcut()` for each non-whitelisted shortcut

### QAM UI structure (current):
- **Main Page**: Connection status, last sync stats, Sync button + progress, nav buttons
- **Connection Settings**: URL, username, password, save, test
- **Platforms**: Enable/disable toggles per platform with ROM counts
- **Danger Zone**: Per-platform removal + Remove All RomM + Remove All Non-Steam (with whitelist)

## Phase 3: Download Manager + Multi-Disc

**Goal**: Download ROMs on-demand with full multi-disc support. Storage space validation.

### Pre-download storage check:
- Check available disk space on target filesystem before starting download
- Block download with clear error if insufficient space (ROM size + buffer)
- Show file size in download confirmation

### Single-file ROM downloads:
- `start_download(rom_id)` → async download task
- Stream from `/api/roms/{id}/content/{file_name}` to `~/retrodeck/roms/{system}/{file_name}`
- Progress tracking, cancellation, partial file cleanup
- Register in `installed_roms` state on completion
- `emit("download_complete")` for toast notification

### Multi-file / multi-disc ROM handling:

**Detection**: Check `has_multiple_files` flag from ROM API. Inspect `files` array to distinguish:
- **Multi-disc** (PS1, PS2, Saturn): Files named `(Disc 1)`, `(Disc 2)`, etc. + M3U file
- **Multi-track single disc** (audio CDs): Files named `(Track 01)`, etc. + single CUE. Not multi-disc — launch via CUE.
- **Game + DLC/updates** (Switch): Base game + update NSP files. Always download ALL files.
- **Extracted directories** (Wii U, PS3): Hundreds of files. Download ZIP, extract to ROM dir.

**Download flow for multi-file ROMs**:
1. `GET /api/roms/{id}/content/{name}` returns a ZIP containing all files + M3U (if applicable)
2. Extract ZIP to `~/retrodeck/roms/{system}/{rom_name}/`
3. Detect launch file:
   - If `.m3u` exists → store as launch path (for multi-disc)
   - Else if `.cue` exists → store as launch path (for single disc with tracks)
   - Else → first/largest file
4. Store launch file path in `installed_roms` state

**M3U auto-generation setting**: If ROM has multiple disc files but no M3U included from RomM, optionally auto-generate one (toggle in settings, default: on).

### Download request from launcher:
- `_poll_download_requests()` — async loop checking `download_requests.json` every 2s
- Launcher writes request, plugin picks it up and starts download
- Toast notification when download completes

### Frontend — QAM additions:

**Download Queue sub-page** (new nav button on Main Page):
- Active downloads with progress bars, cancel buttons
- Completed/failed downloads list

**Downloads Settings sub-page** (new nav button on Main Page):
- Download location override (default: `~/retrodeck/roms/{system}/`)
- M3U auto-generation toggle

**Game detail page injection** (route patch):
- `routerHook.addPatch("/library/app/:appid", ...)` using findInReactTree
- Show: "Not installed" + Download / Progress + Cancel / "Installed" + Uninstall
- `getRomBySteamAppId(appId)` to identify RomM games

### M3U testing checklist:
- [ ] Download a multi-disc PS1 ROM (e.g. FF7) from RomM via the API
- [ ] Verify ZIP contains M3U file
- [ ] Verify M3U content lists all disc .cue files correctly
- [ ] Extract and launch via RetroDECK with the M3U
- [ ] Verify disc swapping works in-game
- [ ] Test a multi-track single-disc ROM (e.g. Tomb Raider) — should NOT use M3U
- [ ] Test auto-generation of M3U when RomM doesn't include one

### Danger Zone — Uninstall All Installed ROMs:

**Feature: "Uninstall All Installed ROMs" button** in Danger Zone:
- Removes all downloaded ROM files from `~/retrodeck/roms/{system}/` for installed ROMs tracked in `installed_roms` state
- Confirmation dialog warns: "This will delete all downloaded ROM files. If save sync is available, sync saves to RomM first."
- If Phase 5 (save sync) is implemented, prompt to sync saves before uninstalling
- If Phase 5 is not yet implemented, show warning: "Save files may be lost — back up manually if needed"
- Clears `installed_roms` state after deletion
- Does NOT remove shortcuts — only the ROM files on disk

### Verification:
- [ ] Storage space check prevents download when disk full
- [ ] Single-file ROM download works end-to-end
- [ ] Multi-disc ROM (e.g. FF7) downloads as ZIP, extracts, has working M3U
- [ ] Launcher detects M3U and passes it to RetroDECK
- [ ] Download progress visible in QAM and game detail page
- [ ] Cancel cleans up partial files
- [ ] Game detail page shows correct install status
- [ ] Uninstall all removes ROM files and clears installed state

## Phase 3.5: Pre-Alpha Bug Fixes

**Goal**: Fix blockers preventing a usable alpha release.

### Bug 1: Controller/gamepad can't navigate game detail page — FIXED
- Download/Uninstall buttons on game detail page were plain HTML, not focusable by Steam's gamepad navigation
- **Fix**: Replaced with `Focusable` + `DialogButton` from `@decky/ui`

### Bug 2: Game launch fails silently — FIXED
Two root causes:
- **Wrong state path**: Launcher looked in `homebrew/settings/` but `state.json` lives in `homebrew/data/`. Fixed search candidates.
- **Invalid `-s` flag**: RetroDECK doesn't have a `-s system` flag — auto-detects from `roms/{system}/` directory path. Removed the flag.

### Bug 3: Steam Input conflicts with RetroArch controller — FIXED
**Symptom**: Gamepad works in-game (GBA tested) but RetroArch menu/hotkeys don't function correctly.

**Research findings (deep dive)**:
- `UseSteamControllerConfig` has three values: `"0"` (Force Off), `"1"` (Default), `"2"` (Force On)
- Our initial fix used `"0"` (Force Off) which **caused** the problem — disabling Steam Input entirely breaks controller passthrough on SteamOS/Bazzite
- EmuDeck uses `"1"` (Default) with custom controller templates, NOT Force Off
- RetroDECK **requires** Steam Input to be enabled — its hotkeys (L3+R3 for menu, save states, etc.) depend on it
- Force Off passes raw HID device; RetroArch's menu/hotkey layer doesn't see it properly
- Force On (`"2"`) normalizes the controller as standard XInput gamepad, which is what RetroArch autoconfig expects

**Fix**: Replaced boolean "Disable Steam Input" toggle with three-option dropdown:
- **Default** (`"1"` / remove key) — recommended, uses global Steam settings
- **Force On** (`"2"`) — explicitly enables Steam Input wrapping
- **Force Off** (`"0"`) — raw passthrough, only for advanced users
- "Apply to All Shortcuts" button for immediate application
- Old `disable_steam_input` boolean auto-migrates to `steam_input_mode`

### Bug 4: BIOS files required for disc-based systems — IMPLEMENTED
**Symptom**: PSX Tomb Raider fails to launch — SwanStation core requires BIOS files (e.g. `scph5501.bin`) which are missing from `~/retrodeck/bios/`.

**Research findings**:
- RomM has a full `/api/firmware` endpoint (list, download, filter by platform_id)
- BIOS files downloaded to `~/retrodeck/bios/` with platform-specific subfolders (dc → `dc/`, ps2 → `pcsx2/bios/`, everything else flat)
- MD5 verification against RomM's stored hashes
- No renaming needed — trust filenames as stored in RomM
- BIOS file management (correct naming, checksums) is RomM's or igir's responsibility

**Implementation**:
- Backend: `get_firmware_status()`, `download_firmware(id)`, `download_all_firmware(platform_slug)` callables
- Frontend: BiosManager QAM page showing per-platform firmware status with "Download All" buttons
- Path mapping via `BIOS_DEST_MAP` dict (currently hardcoded to RetroDECK paths — will become configurable in Phase 4)

### Bug 5: BIOS not shown on game detail page — FIXED
- `checkPlatformBios("psx")` failed because RomM stores firmware under `bios/ps` (slug `ps`) but platform slug is `psx`
- **Fix**: Added `_platform_to_firmware_slugs()` mapping so `psx` matches both `["psx", "ps"]`
- Game detail page now always shows BIOS status (green when ready, orange when missing)
- Clickable BIOS status opens modal overlay listing individual BIOS files per platform

### Bug 6: BIOS file lists not collapsible — FIXED
- BiosManager showed all files expanded by default, making long lists hard to navigate
- **Fix**: Added per-platform expand/collapse toggle, collapsed by default

### Bug 7: RetroArch menu not navigable with controller — FIXED
- D-pad/buttons didn't work in RetroArch Quick Menu (L3+R3), only mouse worked
- All three `UseSteamControllerConfig` values (0/1/2) tested — none fixed it
- **Root cause**: `input_driver = "x"` in RetroArch config uses X11 input on a Wayland system; menu navigation relies on `input_driver` while gameplay uses `input_joypad_driver`
- **Fix**: Changed `input_driver` to `"sdl2"` in RetroArch config
- Added diagnostic check in plugin settings: detects `input_driver` value from RetroArch config (checks RetroDECK, RetroArch Flatpak, and native paths) and shows status/warning in Controller section

### Verification:
- [x] Game detail page buttons navigable with controller
- [x] GBA game launches successfully via RetroDECK
- [x] Steam Input dropdown shows three options (Default, Force On, Force Off)
- [x] "Apply to All Shortcuts" writes correct values to VDF
- [x] Controller works in RetroArch menu with input_driver = "sdl2"
- [x] PSX game launches after BIOS download
- [x] BIOS download from RomM works
- [x] BIOS status shown on game detail page with file list overlay
- [x] BIOS file lists collapsed by default in BiosManager
- [x] RetroArch input_driver diagnostic shown in settings

---

## Phase 4: Artwork, Metadata & Native Steam Integration

**Goal**: Make ROM shortcuts look like first-class Steam games. Full artwork (hero, logo, wide grid), native metadata (description, genres, developer, release date) via store patching, and on-demand metadata caching.

### 4A: Full Artwork (all 4 types)

Currently we only set **portrait grid (assetType 0)** — the cover image in collection/grid views. Three artwork slots are empty, leaving the detail page bare.

**Steam artwork types** (all set via `SteamClient.Apps.SetCustomArtworkForApp(appId, base64, "png", assetType)`):

| Type | assetType | Dimensions | Purpose | Source |
|------|-----------|------------|---------|--------|
| Portrait Grid | 0 | 600x900 | Library grid tiles, collections | RomM cover (already done) |
| Hero Banner | 1 | 1920x620 | Detail page background | SteamGridDB |
| Logo | 2 | varies (transparent PNG) | Title overlay on hero | SteamGridDB |
| Wide Grid | 3 | 460x215 / 920x430 | Recent games shelf, list view | SteamGridDB |

**Icon (assetType 4)** cannot be set via `SetCustomArtworkForApp()` — requires `shortcuts.vdf` write + Steam restart. Deferred.

**SteamGridDB integration**:
- RomM already stores `igdb_id` and `sgdb_id` per ROM — exact match via `GET /games/igdb/{igdb_id}`, no fuzzy name search needed
- API is free, no paid tiers. Needs API key (user provides their own or we use a project key)
- Endpoints: `/heroes/game/{sgdb_id}`, `/logos/game/{sgdb_id}`, `/grids/game/{sgdb_id}?dimensions=460x215,920x430`
- API key stored in `settings.json`

**Implementation**:
- Backend: New callable `get_steamgriddb_artwork(rom_id, asset_type)` — fetches from SteamGridDB, caches to disk
- Backend: During sync, for each ROM: look up SGDB game ID via `igdb_id`, fetch hero + logo + wide grid
- Frontend: After `AddShortcut()`, call `SetCustomArtworkForApp()` for all 4 types (0=RomM cover, 1/2/3=SteamGridDB)
- After setting logo (type 2), save default logo position to prevent blank logos:
  ```typescript
  await window.appDetailsStore.SaveCustomLogoPosition(appOverview, {
    pinnedPosition: 'BottomLeft', nWidthPct: 50, nHeightPct: 50
  });
  ```
- Artwork cached on disk to avoid re-fetching on re-sync. Cache dir: `~/homebrew/data/decky-romm-sync/artwork/`
- Graceful fallback: if SteamGridDB has no match, skip that artwork type (Steam shows defaults)

**Artwork fetch timing**: During sync alongside the existing cover art download. SteamGridDB lookups add latency but artwork is cached after first sync. For 20k+ games, this may be slow — consider a "skip SteamGridDB" toggle or fetch lazily on first detail page visit.

### 4B: Native Metadata via Store Patching

No `SteamClient.Apps` APIs exist for setting description, genres, etc. on non-Steam shortcuts. However, **MetaDeck** (EmuDeck) proves that patching Steam's internal store objects works reliably and makes shortcuts look native.

**Store patches** (applied on plugin load for all registered shortcut app IDs):

| Field | Patch Target | Data Shape | RomM Source |
|-------|-------------|------------|-------------|
| Description | `appDetailsStore.GetDescriptions()` | `{ strFullDescription, strSnippet }` | `summary` |
| Developer | `appDetailsStore.GetAssociations()` | `{ rgDevelopers: [{strName, strURL}] }` | `companies` |
| Publisher | `appDetailsStore.GetAssociations()` | `{ rgPublishers: [{strName, strURL}] }` | `companies` |
| Genres | `appStore.BHasStoreCategory()` + `m_setStoreCategories` | Steam StoreCategory enum values | `genres` |
| Release Date | `appStore.GetCanonicalReleaseDate()` | Unix timestamp | `first_release_date` |
| Controller Support | `appOverview.controller_support` | `2` (full) | Hardcode |
| Hide "non-Steam" label | `BIsModOrShortcut()` | Return `false` for our app IDs | — |

**Reference implementation**: [MetaDeck](https://github.com/EmuDeck/MetaDeck) — uses `afterPatch` from `@decky/ui`.

**Metadata fetching strategy — on-demand + cache**:
- Do NOT fetch metadata for all ROMs during sync (too slow for large libraries)
- Fetch full metadata from RomM API when game detail page is opened: `get_rom_metadata(rom_id)` callable
- Cache response in `metadata_cache.json` (separate from `state.json` to avoid bloat)
- Cache TTL: 7 days, re-fetch if expired and network available
- If offline and cached: show cached data. If offline and not cached: show "metadata unavailable"
- Store patches read from cache — if metadata not yet cached for an app ID, patches return empty/default (Steam shows nothing, same as now)

**Backend changes**:
- New callable `get_rom_metadata(rom_id)` — fetches from RomM API `GET /api/roms/{id}`, extracts and returns structured metadata
- Metadata cache: `metadata_cache.json` keyed by `rom_id`, stores `{ summary, genres, companies, first_release_date, screenshots, cached_at }`
- Handle ROMs without IGDB match gracefully (return empty metadata)

**Frontend changes**:
- On plugin load: apply store patches for all registered app IDs
- Patches check `metadata_cache.json` (loaded into memory) for the requested app ID
- When `GameDetailPanel` mounts: call `get_rom_metadata(rom_id)`, update cache, re-apply patches for that app ID
- Loading state while metadata fetches (detail page briefly shows defaults, then fills in)

### 4C: Screenshots

RomM provides `url_screenshots` (IGDB screenshots, 1280x720) and `merged_screenshots` (locally cached paths).

**Options to investigate**:
- Can we inject screenshots into Steam's native screenshot viewer for non-Steam shortcuts?
- Can store patching set `nScreenshots` and provide screenshot data?
- Fallback: display screenshots in our existing `GameDetailPanel` route patch as a scrollable gallery
- IGDB screenshots could also serve as hero banner fallback (1280x720, not ideal aspect ratio for hero's 1920x620 but better than nothing)

### Verification:
- [ ] Detail page shows hero banner background image
- [ ] Detail page shows logo overlay on hero
- [ ] Wide grid image appears in recent games shelf and list view
- [ ] Portrait grid still works (regression check)
- [ ] Games without SteamGridDB match degrade gracefully (no hero/logo, just defaults)
- [ ] Description shows in native Steam detail page area
- [ ] Genres displayed natively
- [ ] Developer/publisher displayed natively
- [ ] Release date displayed natively
- [ ] Controller support shows "Full Controller Support"
- [ ] Metadata loads on first detail page visit, cached for subsequent visits
- [ ] Cached metadata shown when offline
- [ ] ROMs without IGDB metadata show graceful fallback
- [ ] SteamGridDB API key configurable in settings
- [ ] Store patches survive QAM close/reopen and Steam sleep/wake

---

## Phase 5: Save File Sync (RetroDECK)

**Goal**: Bidirectional save file synchronization between RetroDECK and RomM. Hardcoded to RetroDECK paths for now — multi-emulator path abstraction deferred.

**IMPORTANT: RomM account requirement**: Save games in RomM are tied to the authenticated user account. Users MUST use their own RomM account (not a shared/generic one) so saves are correctly attributed. Document this in README and show a warning in settings if the account appears to be shared (e.g. username is "admin" or "romm").

### Play session detection:
Need to reliably detect when a game session starts and ends. Two main options:
- **Poll RetroDECK process**: Check if `net.retrodeck.retrodeck` flatpak process is running, detect when it exits
- **Steam play tracking**: Use `SteamClient` APIs to detect game launch/exit events for our shortcut app IDs

Investigation needed to determine which is more reliable. Polling the process seems more direct; Steam tracking has better integration but may not fire reliably for non-Steam shortcuts.

### Save file locations (RetroDECK only):
- RetroArch saves: `~/retrodeck/saves/{system}/{rom_name}.srm`
- RetroArch states: `~/retrodeck/states/{system}/`
- Shared memory cards (PS1/PS2): `~/retrodeck/saves/{system}/` — these systems use shared memory cards rather than per-game saves. Need to determine how to associate uploads with specific ROMs, or handle at a system level.

### Upload flow (after play session ends):
1. Detect game session ended (via chosen detection method)
2. Scan save directory for files newer than last sync timestamp
3. For each changed save file:
   - Compare `updated_at` from RomM save vs local file `mtime`
   - If local is newer → upload via `POST /api/saves` with `rom_id` and `emulator`
   - If remote is newer → conflict (see below)
   - If equal → skip

### Download flow (before play or on-demand):
1. Fetch saves: `GET /api/saves?rom_id={id}`
2. Compare each save's `updated_at` with local file `mtime`
3. Download newer saves via `GET /api/saves/{id}/content`
4. Place in correct RetroDECK save directory

### Conflict resolution options (user setting):
- **"Always upload local"**: Local saves win. Upload even if remote is newer.
- **"Always download remote"**: Remote wins. Overwrite local.
- **"Ask me"** (default): Show conflict dialog in QAM with timestamps, let user choose.
- **"Newest wins"**: Compare timestamps, use whichever is more recent.

### Auto-sync behavior options:
- **Sync saves before launch** (toggle, default: on): Download latest saves before starting game
- **Sync saves after play** (toggle, default: on): Upload changed saves after game exits
- **Background periodic sync** (toggle, default: off): Sync all saves every N minutes

### Save sync tracking:
- Track per-rom last sync timestamp in state.json
- Track which emulator created each save (RomM's `emulator` field)
- Handle multiple save files per game (e.g. multiple memory card slots)

### Manual "Sync All Saves to RomM" button:
- In Danger Zone (or Save Sync settings page): one-click button to upload all local saves to RomM
- Scans all installed ROMs, finds their save files, uploads any that are newer than RomM's copy
- Use case: before uninstalling all ROMs, user can ensure saves are backed up to RomM
- Also useful as a manual "backup my saves" action independent of play sessions

### QAM UI additions:
- **Save Sync settings sub-page**: Conflict resolution mode, auto-sync toggles, sync interval
- **Per-game save status on game detail page**: Last synced, local/remote timestamps, manual sync button
- **Conflict dialog**: Show when "Ask me" mode encounters a conflict
- **"Sync All Saves to RomM" button**: In Danger Zone, uploads all local saves before destructive actions

### Verification:
- [ ] Save file uploaded to RomM after play session
- [ ] Save file downloaded from RomM before launch
- [ ] Conflict detected and presented to user
- [ ] "Newest wins" correctly compares timestamps
- [ ] Multiple save files for same game handled
- [ ] Save sync status visible on game detail page

---

## Phase 6: Bug Fixes & Stability

**Goal**: Fix known bugs and improve reliability before adding more features.

### Bug 1: Sync Progress Bar Not Updating

**Symptom**: Progress bar appears briefly (indeterminate), then either freezes or disappears. Does not show real-time progress during sync. The sync itself completes successfully — only the UI feedback is broken.

**What was tried (all failed)**:
1. Setting initial `syncProgress` immediately in `handleSync()` before `await startSync()`
2. Changing backend `finally` block to not emit `running: false` prematurely (added 60s safety timeout)
3. Adding `updateApplyProgress` callable for frontend to report per-item progress back to backend
4. **Event-based approach**: Backend emits `sync_progress` events via `decky.emit()`, persistent listener in `index.tsx` writes to module-level store, MainPage polls store at 250ms — no WebSocket round-trips

**Current architecture** (as implemented but not working):
- `syncProgress.ts`: Module-level variable store (`_progress`), updated by event listener + syncManager
- `index.tsx`: Persistent `sync_progress` event listener calls `setSyncProgress()` on the module store
- `syncManager.ts`: Calls `updateSyncProgress()` per shortcut during the applying phase
- `MainPage.tsx`: `setInterval(250ms)` reads `getSyncProgress()` from module store, updates React state

**Research findings**:
- QAM panel **unmounts** `MainPage` when closed, destroying all React state and intervals. On reopen, MainPage remounts fresh — this is handled by checking the store on mount.
- Decky's WebSocket bridge has contention: concurrent `callable()` calls (e.g. `getArtworkBase64()` during sync) may block or delay other calls.
- The module-level store approach should bypass WebSocket contention since it's a plain JS read, but progress still doesn't update.

**Investigation needed**:
- Verify `sync_progress` events are actually being received by the frontend (add `console.log` in event listener)
- Verify the `setInterval` in MainPage is actually running and reading updated values
- Check if `setSyncProgress` in the event listener is being called with correct data
- Check if `updateSyncProgress` in syncManager is being called (the applying phase runs entirely in frontend)
- Consider whether the 250ms polling interval is too slow or if React batching is preventing re-renders
- Test with a minimal reproduction: hardcode progress updates on a timer to isolate whether the issue is data flow or rendering

### Bug 2: Cancel Sync Not Working

**Symptom**: Clicking "Cancel Sync" does not stop the frontend shortcut processing loop. Backend cancellation may work (stops ROM fetching), but the frontend `sync_apply` handler continues processing all shortcuts.

**Current implementation**:
- `syncManager.ts`: Module-level `_cancelRequested` flag, checked after each shortcut in the loop
- `MainPage.tsx`: `handleCancel()` calls both `requestSyncCancel()` (sets frontend flag) and `cancelSync()` (backend callable)
- Backend `cancelSync()`: Sets `_sync_cancel = True` in Python, which stops the backend sync loop

**Why it might not work**:
- The `sync_apply` event is fired ONCE with all shortcut data. By the time the user clicks cancel, the backend has already emitted the full payload. Backend cancellation only helps if the backend is still fetching ROMs.
- The frontend cancel flag (`_cancelRequested`) should work for the frontend loop, but may not be checked if the loop is blocked on an `await` (e.g. `addShortcut()` or `getArtworkBase64()`)
- Need to verify `requestSyncCancel()` is actually being called and the flag is being read

**Investigation needed**:
- Add `console.log` to verify `requestSyncCancel()` is called
- Add `console.log` inside the sync loop to verify the flag check runs
- Check if the `await getArtworkBase64()` call is blocking for a long time, preventing the cancel check
- Consider adding the cancel check BEFORE the artwork fetch (currently only checked at end of loop iteration)
- Consider whether a different cancellation mechanism is needed (e.g. AbortController for fetch calls)

### Bug 3: RomM M3U Lists BIN Files Instead of CUE

**Symptom**: RomM's download endpoint auto-generates M3U files for multi-file ROMs. For single-disc multi-track games (e.g. Tomb Raider), the M3U lists all `.bin` tracks + the `.cue` file. This is wrong — the M3U should only list `.cue` files (for multi-disc switching). The CUE file already references its BIN tracks internally.

**Impact**: Our `_detect_launch_file` prefers M3U over CUE. So RetroArch gets an M3U pointing to raw BIN files instead of the proper CUE sheet. Audio tracks won't play correctly.

**Fix**: After extraction, validate downloaded M3U files. If an M3U contains only `.bin` entries (plus optionally one `.cue`), it's a bad single-disc M3U — delete it. A proper multi-disc M3U lists `.cue`/`.chd`/`.iso` entries. Alternatively, fix `_detect_launch_file` to prefer CUE over M3U when the M3U only contains `.bin` files.

### Bug 4: State Consistency / Startup Pruning

**Problem**: `state.json` can drift from reality after crashes or force-closes:
- `installed_roms` entry exists but the ROM file is gone from disk
- `shortcut_registry` entry exists but the Steam shortcut no longer exists
- ROM file exists on disk but no `installed_roms` entry tracks it (orphaned partial download)
- Single-file downloads lack `.tmp` atomicity

**Solution — startup state healing in `_main()`**:
1. **Prune `installed_roms`**: Iterate entries, remove any where `file_path` no longer exists on disk.
2. **Prune `shortcut_registry`**: Frontend checks which Steam shortcuts still exist via `SteamClient` API, reports stale app IDs back to backend. Backend removes orphaned registry entries.
3. **Download atomicity**: Single-file ROM downloads should write to `{file_path}.tmp` and `os.replace()` to final path on completion.
4. **Save state after pruning**: Write the cleaned state back to `state.json` before normal operation begins.

### Verification:
- [ ] Progress bar shows real-time progress during sync
- [ ] Cancel button stops the sync mid-progress
- [ ] Single-disc multi-track game launches via CUE, not bad M3U
- [ ] Startup pruning removes orphaned state entries
- [ ] Partial downloads cleaned up on startup

---

## Phase 7: Multi-Emulator Support (Deferred)

**Goal**: Support EmuDeck, standalone RetroArch, and manual emulator installs beyond RetroDECK.

### Emulator platform presets:
- **RetroDECK** (current): `~/retrodeck/roms/`, `~/retrodeck/bios/`, `~/retrodeck/saves/`
- **EmuDeck**: `~/Emulation/roms/`, `~/Emulation/bios/`, `~/Emulation/saves/`
- **Manual**: All paths user-configurable

### Configurable paths:
- ROM directory, BIOS directory, save directory, emulator launch command
- All stored in `settings.json`
- Update `BIOS_DEST_MAP`, `start_download()`, and launcher to read from settings

### Enhanced launcher:
- Per-preset launch commands
- EmuDeck per-system emulator mapping
- Verify emulator installed before launch

### Per-system emulator/core selection:
- New "Emulators" QAM sub-page
- Per-game override on detail page
- Resolution order: per-game → per-system → preset default

---

## Phase 8: Polish & Advanced Features (Deferred)

**Goal**: Production-ready with good UX and reliability.

- **Multi-version/language ROM selector**: Dropdown when multiple versions exist in RomM
- **Auto sync interval**: Configurable background re-sync
- **Library management**: Detect removed/updated ROMs on RomM, stale state cleanup
- **Offline mode**: Cache lists locally, queue operations
- **Stacked sync progress UI**: Replace single progress bar with phased checklist
- **Error handling**: Retry with backoff, toast notifications, detailed logging

---

## Future Improvements (nice-to-have)

- **Concurrent download queue**: Support multiple queued downloads instead of one at a time.
- **RomM native device sync**: When RomM v4.7+ ships device sync features, migrate from our own conflict resolution.
- **Download queue priority/reordering**: Let users reorder queued downloads.
