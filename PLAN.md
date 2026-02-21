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
| `/api/saves/{id}/content` | GET | Download save file binary (**4.7+ only** — does not exist in 4.6.1; use `download_path` from save metadata instead) |
| `/api/saves/delete` | POST | Batch delete saves `{"saves": [id, ...]}` |
| `/api/states` | GET/POST | Same as saves but for emulator save states |
| `/api/states/{id}/content` | GET | Download state file binary |

### Known RomM API Limitations (v4.6.1)

Verified live against RomM v4.6.1. See `.romm-api-verified.md` for full test results.

| Issue | Workaround |
|-------|------------|
| `GET /api/roms/{id}/notes` returns 500 when any note exists | Read `all_user_notes` array from `GET /api/roms/{id}` (ROM detail endpoint) instead |
| No `content_hash` field in SaveSchema | Hybrid detection: fast path compares `updated_at` + `file_size_bytes`; slow path downloads server save to tmp and computes MD5 |
| No dedicated playtime API | Store playtime in per-ROM user notes with `title: "romm-sync:playtime"` and JSON content. Do NOT send `tags` field (contributes to GET bug). |
| `GET /api/saves/{id}/content` does not exist | Use `download_path` URL from save metadata (must URL-encode spaces and parentheses) |
| `device_id` param on POST /api/saves is accepted but ignored | Client-side device tracking via `save_sync_state.json`; keep device_id for future 4.7.x compatibility |
| No server-side conflict detection (no 409 responses) | Three-way conflict detection on the client using last-sync snapshot hash |
| `POST /api/saves` does upsert by filename | Same file_name + rom_id + user_id → updates in place (same ID preserved). No need to delete first. |

### RomM 4.7.0-alpha.1 Future Migration

RomM 4.7.0-alpha.1 was released 2026-02-12 but is not yet deployed on the target server. It adds:

- **Device registration**: `POST /api/devices` — proper server-side device tracking
- **`content_hash`** in SaveSchema — eliminates need for download-and-hash slow path
- **`GET /api/saves/{id}/content`** — dedicated binary download endpoint (replaces `download_path` workaround)
- **Server-side 409 conflict detection** — server tracks per-device sync state, returns HTTP 409 on stale uploads
- **Save slots** — multiple save versions per ROM

**When the user upgrades to 4.7+**, we can simplify significantly:
- Switch to `content_hash` for change detection (no more download-and-hash)
- Use server-side 409 conflicts instead of client-side three-way detection
- Use `GET /api/saves/{id}/content` instead of `download_path`
- Register as a device via `POST /api/devices` for proper server tracking
- Notes GET bug may be fixed (untested)

**Feature request #1225** (dedicated playtime API) is still open. Until it ships, we continue using notes-based playtime storage.

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
- The canonical saves path comes from `retrodeck.json` at `~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json` → `paths.saves_path`. This varies by install location (internal SSD vs SD card). Do NOT hardcode `~/retrodeck/saves/` — on SD card installs it's `/run/media/deck/Emulation/retrodeck/saves/`.
- RetroArch saves: `<saves_path>/{system}/{rom_name}.srm` (when `sort_savefiles_by_content_enable = true`, which is the RetroDECK default — subdirs match ROM folder names, NOT core names)
- RetroArch states: `<states_path>/{system}/`
- Standalone emulator saves: `<saves_path>/{platform}/{emulator_name}/` — each emulator has its own subdirectory and save format (see Phase 7)

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

### 4C: Screenshots — DEFERRED to Phase 8

Steam's screenshot viewer is for user-captured screenshots. Injecting IGDB promotional screenshots there would be misleading. The only option is a custom gallery in GameDetailPanel — cosmetic, low priority.

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

## Phase 4.5: Pre-Phase-5 Bug Fixes

**Goal**: Fix bugs found during alpha testing before moving to save sync.

### Bug 1: ROM download button does nothing on Steam Deck — FIXED
**Symptom**: Clicking the Download button on the game detail page does nothing on Steam Deck. No error, no progress, just nothing happens. BIOS downloads work fine on the same device. Works correctly on Bazzite HTPC.

**Root cause**: Investigated and resolved — download handler was not properly triggering on Steam Deck due to event propagation differences.

### Bug 2: DangerZone counts not refreshing after removal — FIXED
**Symptom**: After removing shortcuts via DangerZone (per-platform or remove-all), the game counts displayed next to each platform did not update. User had to close and reopen the QAM to see correct counts.

**Root cause**: The `showModal()` confirmation dialog caused the QAM panel to remount when dismissed, creating a race condition where the component re-rendered with stale data before the removal operation completed and state refreshed.

**Fix**: Replaced `showModal()` confirmation dialogs with inline confirmation UI (confirm/cancel buttons rendered directly in the DangerZone component). This avoids the QAM remount race entirely. Also added cross-refresh calls so that after any removal operation (per-platform, remove-all-romm, remove-all-non-steam), the component re-fetches counts for all affected sections.

### Bug 3: Non-Steam shortcuts appear on other devices via Steam Remote Play — DOCUMENTED
**Symptom**: Non-Steam shortcuts created by the plugin on one machine (e.g. Steam Deck) appear on other machines logged into the same Steam account (e.g. Bazzite HTPC). Shortcuts show without artwork, and disappear when the source machine goes offline.

**Root cause — Steam Remote Play discovery protocol (NOT Steam Cloud)**:
This is NOT `shortcuts.vdf` syncing via Steam Cloud. `shortcuts.vdf` is local-only and never leaves the machine. What users see is Steam's **In-Home Streaming / Remote Play discovery protocol**:

1. **Discovery**: Steam clients on the same LAN broadcast UDP packets on port 27036. When two clients with the same Steam account discover each other, they establish a TCP control connection.
2. **App advertisement**: The source client sends `CMsgRemoteClientAppStatus` protobuf messages containing `ShortcutInfo` for each non-Steam shortcut. This includes: `name`, `icon`, `categories` (collections), `exepath`, and `launch_options`.
3. **What's NOT transmitted**: Artwork files (grid, hero, logo) are local filesystem references — they don't transfer over the protocol. This is why remote shortcuts appear without artwork.
4. **Ephemeral**: These phantom shortcuts exist only while the TCP connection is live. When the source machine goes offline (sleep, shutdown, network disconnect), the shortcuts disappear from the remote client.
5. **No per-shortcut opt-out**: Steam advertises ALL non-Steam shortcuts to remote clients. There is no API or setting to exclude specific shortcuts from streaming advertisement.

**Detection APIs available**:
- `collectionStore.localGamesCollection` — contains only locally-created shortcuts (excludes remote phantoms)
- `SteamAppOverview.per_client_data` — array of `{ clientid, client_name, ... }` entries; remote shortcuts have entries from a different client
- `SteamAppOverview.local_per_client_data` — only the local machine's entry

**Implementation — DangerZone protection + device labels**:
- **DangerZone**: Filter removal operations to only include locally-owned shortcuts (using `localGamesCollection` or `per_client_data` checks) to prevent accidentally removing remote phantom entries
- **Game detail panel**: Show device availability labels (e.g. "Available on: Steam Deck") when `per_client_data` indicates the shortcut originates from another device

**Known Steam bugs**:
- Steam issue #8791: Name collisions when identical shortcut names exist on source and remote client
- Steam issue #12315: "Stream" button regression — sometimes remote shortcuts only show "Stream" instead of allowing local launch

**References**: See wiki page "Steam Remote Play and Cross-Device Shortcuts" for full protocol documentation.

### Verification:
- [x] ROM download works on Steam Deck from game detail page
- [x] Download progress visible after clicking download
- [x] Error shown if download fails
- [x] DangerZone counts refresh immediately after removal operations
- [x] DangerZone confirmation uses inline UI (no modal remount race)
- [x] Non-Steam shortcut cross-device behavior documented (Steam Remote Play, not Cloud)
- [x] DangerZone protected from removing remote streaming phantom shortcuts
- [ ] Device availability labels shown in game detail panel

---

## Phase 4.5: Codebase Restructuring

**Goal**: Split oversized files into focused modules before adding more features. Currently `main.py` is 1800+ lines and `tests/test_main.py` is 2400+ lines — both will keep growing with save sync (Phase 5) and metadata (Phase 4B).

**Backend (`main.py` → `backend/` package)**:
- `backend/__init__.py` — Plugin class, lifecycle (_main, _unload), settings
- `backend/romm_api.py` — RomM HTTP client (connection, auth, requests)
- `backend/sgdb.py` — SteamGridDB integration (artwork fetch, verify key, cache)
- `backend/sync.py` — Sync engine (_do_sync, report_sync_results, registry)
- `backend/downloads.py` — ROM download manager (start, cancel, progress, multi-file)
- `backend/firmware.py` — BIOS/firmware management
- `backend/state.py` — State persistence (state.json, settings.json, save/load)

**Tests (`tests/test_main.py` → split by module)**:
- `tests/test_sgdb.py` — SGDB artwork and verify tests
- `tests/test_sync.py` — Sync engine tests
- `tests/test_downloads.py` — Download manager tests
- `tests/test_firmware.py` — BIOS tests
- `tests/test_settings.py` — Settings/state tests
- `tests/conftest.py` — Shared fixtures (already exists)

**Considerations**:
- Decky Loader expects a single `main.py` entry point with a `Plugin` class — the Plugin class stays in `main.py` but delegates to modules
- Need to verify Decky's Python environment supports relative imports from subdirectories
- All 140+ tests must pass after restructuring (no behavior changes)

---

## Phase 5: Save File Sync (RetroArch .srm saves) — IN PROGRESS

**Goal**: Bidirectional save file synchronization between RetroDECK and RomM for RetroArch-based systems. Covers per-game `.srm` saves only — standalone emulator saves (PCSX2, DuckStation, Dolphin, etc.) deferred to Phase 7.

**IMPORTANT: RomM account requirement**: Save games in RomM are tied to the authenticated user account. Users MUST use their own RomM account (not a shared/generic one) so saves are correctly attributed. Document this in README and show a warning in settings if the account appears to be shared (e.g. username is "admin" or "romm").

### Scope — RetroArch per-game .srm saves only:

Systems covered (all use RetroArch cores via RetroDECK):
- NES, SNES, GB, GBC, GBA, Genesis/Mega Drive, Master System
- N64, PSX (via SwanStation/Beetle PSX cores), Saturn, Dreamcast
- PC Engine/TurboGrafx-16, Neo Geo Pocket, WonderSwan, Atari Lynx
- Any other system using a RetroArch core with `.srm` save files

Save path pattern: `<saves_path>/{system}/{rom_name}.srm` where `<saves_path>` is read at runtime from `retrodeck.json` → `paths.saves_path` (e.g. `/run/media/deck/Emulation/retrodeck/saves/` on SD card installs). The `sort_savefiles_by_content_enable = true` default means subdirectories match ROM folder names (e.g. `gba/`), NOT RetroArch core names (e.g. NOT `mGBA/`).

**IMPORTANT**: The hardcoded `~/retrodeck/saves/` fallback in the current code is wrong for SD card installs. The path MUST be read from `retrodeck.json` at runtime. See `_get_retroarch_saves_dir()` in `lib/save_sync.py` — it currently reads from RetroArch config (correct) but falls back to `~/retrodeck/saves/` (wrong on SD card). The fallback should also read `retrodeck.json`.

**Explicitly deferred** (see Phase 7):
- PS2 via PCSX2 (shared memory cards)
- PSX via DuckStation standalone (`.mcd` files)
- GameCube/Wii via Dolphin (`.gci` files, region subfolders)
- PSP via PPSSPP (title ID directories)
- NDS via melonDS (`.sav` files)
- 3DS via Azahar, Wii U via Cemu (NAND/title ID structures)

### Device registration:

The plugin registers with RomM's `/api/saves` as a device so saves are attributed to this machine. A `device_id` (UUID) is generated on first use and persisted in `save_sync_state.json`. This allows multi-device save management — each device's saves are tracked independently.

### Session detection:

Uses `SteamClient.GameSessions.RegisterForAppLifetimeNotifications` to detect game start/stop events. `Router.MainRunningApp` provides reliable app ID resolution after a short delay (500ms). Suspend/resume hooks (`RegisterForOnSuspendRequest` / `RegisterForOnResumeFromSuspend`) pause playtime tracking during device sleep.

The frontend `sessionManager.ts` maintains a cached `appId -> romId` map (from backend registry) to quickly determine if a launched app is a RomM shortcut.

### Three-way conflict detection:

Conflict detection uses three data points:
1. **Local file**: current `.srm` on disk (MD5 hash + mtime)
2. **Last-sync snapshot**: MD5 hash of the file at last successful sync (stored in `save_sync_state.json`)
3. **Server save**: RomM's version via hybrid detection — fast path compares `updated_at` + `file_size_bytes` against stored values (both unchanged → skip); slow path downloads server save to tmp and computes MD5 when timestamps differ. RomM 4.6.1 has no `content_hash` field.

This is more reliable than simple two-way timestamp comparison because it can distinguish:
- Local-only changes (local differs from snapshot, server matches snapshot)
- Server-only changes (server differs from snapshot, local matches snapshot)
- True conflicts (both local and server differ from snapshot)
- No changes (all three match)

### Conflict resolution modes (user setting):
- **"Newest wins"** (default): Compare local mtime vs server `updated_at`, use whichever is more recent
- **"Always upload"**: Local saves always win, upload even if server is newer
- **"Always download"**: Server saves always win, overwrite local
- **"Ask me"**: Queue the conflict and show it in the Save Sync settings page for manual resolution (keep local / keep server)

Clock skew tolerance is configurable (`clock_skew_tolerance_sec`, default 5s) to handle minor time differences between devices.

### Sync flows:

**Pre-launch sync** (if `sync_before_launch` enabled, default: on):
1. Game start detected via lifetime notification
2. Backend fetches server saves for this ROM via `GET /api/saves?rom_id={id}`
3. Three-way comparison against local file and last-sync snapshot
4. If server has newer save and no conflict → download and replace local
5. If conflict → resolve per conflict mode (or queue for "ask me")
6. Non-blocking: game launch is not delayed by sync failures

**Post-exit sync** (if `sync_after_exit` enabled, default: on):
1. Game stop detected via lifetime notification
2. Backend scans local save file, computes hash
3. If local hash differs from last-sync snapshot → save has changed
4. Three-way comparison with server version
5. If no conflict → upload via `POST /api/saves` with `rom_id` and `emulator`
6. If conflict → resolve per conflict mode or queue
7. Toast notification on success or conflict

**Manual sync all**:
- "Sync All Saves Now" button in Save Sync settings
- Iterates all installed ROMs, performs three-way check for each
- Reports total synced count and any conflicts found

**Offline queue drain**:
- If server is unreachable during post-exit sync, changes are queued locally
- Queued changes are retried on next successful server contact

### Playtime tracking:

**Local tracking**: Session start/end times recorded via `recordSessionStart` / `recordSessionEnd` backend callables. Suspend/resume pauses are subtracted for accurate delta. Playtime delta stored per-ROM in `save_sync_state.json`.

**Steam display**: Steam tracks playtime natively for non-Steam shortcuts — no additional work needed for the Steam UI.

**RomM `last_played`**: After each session, the backend updates the ROM's `last_played` timestamp on the server. Ready for future RomM playtime API when it becomes available.

### State schema — save_sync_state.json:

Separate file from `state.json` to avoid bloating the main state. Structure:

```json
{
  "device_id": "uuid-v4",
  "saves": {
    "<rom_id>": {
      "last_synced_at": "ISO-8601",
      "last_snapshot_hash": "md5-hex",
      "server_save_id": 123,
      "playtime_seconds": 3600
    }
  },
  "pending_conflicts": [
    {
      "rom_id": 42,
      "file_path": "saves/gba/game.srm",
      "local_hash": "abc...",
      "local_mtime": "ISO-8601",
      "server_hash": "def...",
      "server_updated_at": "ISO-8601",
      "server_save_id": 456,
      "detected_at": "ISO-8601"
    }
  ],
  "settings": {
    "conflict_mode": "newest_wins",
    "sync_before_launch": true,
    "sync_after_exit": true,
    "clock_skew_tolerance_sec": 5
  }
}
```

### QAM UI:

**Save Sync settings sub-page** (`SaveSyncSettings.tsx`):
- Auto-sync toggles: sync before launch, sync after exit
- Conflict resolution mode dropdown (newest_wins, always_upload, always_download, ask_me)
- "Sync All Saves Now" button with status feedback
- Pending conflicts section: shows unresolved conflicts with "Keep Local" / "Keep Server" buttons

**Game detail page**: Save status (last synced, local/remote status) visible per-game. Manual sync button per-game.

### Remaining work (Phase 5 completion):

#### 1. Backend retry logic with exponential backoff
`lib/save_sync.py` currently has no retry on failed uploads/downloads — a single failure goes straight to the offline queue. Add retry with exponential backoff (3 attempts: 1s, 3s, 9s) before falling back to the offline queue. This applies to:
- `_romm_upload_save()` — retry the multipart POST
- `_romm_download_save()` — retry the GET
- `_romm_list_saves()` — retry the list call
- Any RomM API call in `pre_launch_sync` and `post_exit_sync`

The offline queue drain (`sync_all_saves`, next `pre_launch_sync`) should also use retry logic when processing queued entries.

#### 2. Custom Play button, launch blocking, save sync UX, and conflict resolution

This is the largest remaining item — it replaces the native Play button with a state-aware custom button, adds a global launch interceptor as safety net, and integrates proactive save sync checks with conflict resolution. Also includes pre-launch toast notifications.

**Research finding**: There is NO Steam API to change the Play button text from "Play" to "Install"/"Download" for non-Steam shortcuts. `EDisplayStatus`, `display_status`, `installed` etc. are Steam-internal and cannot be reliably overridden. The proven approach (used by Unifideck, GPL-3.0 compatible) is: hide the native Play button via React tree style mutation, render a custom replacement, and intercept launches globally as a safety net.

**Part A — Custom Play button replacement** (`gameDetailPatch.tsx`):

When the game detail page renders for a RomM shortcut, hide the native PlaySection via React tree style mutation (`playSection.props.style = { display: 'none' }`) and render a custom button in its place. The button has 4 states:

1. **Checking**: Loading spinner shown while proactive save sync check runs. Retry count displayed in the center of the spinner (e.g. "1", "2", "3" as retries happen). This state is brief — typically <1 second on LAN.
2. **Download**: ROM not installed. Button text: "Download" (or "Install"). Click triggers the existing ROM download flow (`startDownload(romId)`). While downloading, show inline progress bar with percentage and a cancel option. On completion, transition to state 4 (Play).
3. **Resolve Conflict**: Save conflict detected in "ask_me" mode. Button text: "Resolve Conflict" (orange/warning style). Click opens the conflict resolution popup (see Part D). Other conflict modes (newest_wins, always_upload, always_download) auto-resolve during the check phase and go straight to state 4.
4. **Play**: All clear — ROM downloaded, no conflicts. Green button, same as native. Click calls `SteamClient.Apps.RunGame()`.

State transitions:
- Page open → **Checking** → (no ROM) → **Download**
- Page open → **Checking** → (conflict) → **Resolve Conflict** → (resolved) → **Play**
- Page open → **Checking** → (all clear) → **Play**
- **Download** → (complete) → **Checking** → **Play**

The custom button should visually match Steam's native Play button as closely as possible (use `appActionButtonClasses.PlayButton`, `playSectionClasses`, green styling for Play/Download, orange for Resolve Conflict).

**Part B — Global launch interceptor** (`index.tsx`):

Safety net for launches from context menus, Steam search, etc. — anywhere outside the game detail page where our custom button doesn't exist.

```typescript
SteamClient.Apps.RegisterForGameActionStart((gameActionId, appIdStr, action) => {
  if (action !== "LaunchApp") return;
  const appId = parseInt(appIdStr, 10);
  if (!isOurApp(appId)) return;

  if (!isRomDownloaded(appId)) {
    SteamClient.Apps.CancelGameAction(gameActionId);
    // Toast: "ROM not downloaded — open game page to download"
  }
  if (hasSaveConflict(appId)) {
    SteamClient.Apps.CancelGameAction(gameActionId);
    // Toast: "Save conflict — resolve on game page before playing"
  }
});
```

Register on plugin load, unregister on unload. This is the authoritative block — the custom button is UX polish, this interceptor is the functional gate.

**Part C — Proactive save sync check on detail page**:

When `GameDetailPanel` mounts for a RomM game:
1. Call `preLaunchSync(romId)` in the background to detect/resolve conflicts before the user hits Play
2. Result determines the custom button state (Play vs Resolve Conflict)
3. This replaces the old approach of syncing inside `RegisterForAppLifetimeNotifications` lifecycle hooks

Pre-launch sync still happens in the session manager for the case where users launch from context menu (interceptor cancels → user opens detail page → sync runs → user can play).

**Pre-launch toast notifications** (also part of this task):
- Success: "Saves downloaded from RomM" (only when files were actually downloaded, not on no-op)
- Failure: "Failed to sync saves — playing with local saves"
- Conflict queued: "Save conflict detected — resolve on game page"

**Part D — Conflict resolution popup**:

Triggered from the "Resolve Conflict" custom button or from SaveSyncSettings page. Shows:
- File name and system (e.g. "Pokemon FireRed.srm — GBA")
- Local file: last modified timestamp, which device
- Server file: `updated_at` timestamp, which device last uploaded
- Options: "Keep Local" (upload ours), "Keep Server" (download theirs), "Skip" (defer)
- ~~"Keep Both"~~ — requires RomM 4.7+ save slots. Stub in code as commented-out option with `// TODO: Enable when RomM 4.7+ save slots are available`
- After resolution, custom button transitions to **Play** state

File sizes are omitted — `.srm` saves are fixed-size per cartridge/core, providing no useful decision-making information. Revisit in Phase 7 when standalone emulator saves (variable-size memory cards) are added.

**SaveSyncSettings consistency**: The existing "Keep Local" / "Keep Server" buttons on the SaveSyncSettings page should also use this improved popup for consistency.

#### ~~4. Retry UI for failed sync operations~~ — ALREADY IMPLEMENTED
SaveSyncSettings already shows failed operations with "Retry Now" per item and "Clear All Failed". Game detail panel shows failed ops with retry button. Toast on failure covered by item #2 Part C toasts.

#### 5. Playtime storage via RomM user notes — IMPLEMENTED
RomM has no dedicated playtime field (feature request #1225 open). Implemented using the per-ROM Notes API to store playtime as a separate note per ROM.

**Note format** (verified against RomM 4.6.1):
```json
{
  "title": "romm-sync:playtime",
  "content": "{\"seconds\": 18450, \"updated\": \"2026-02-17T10:30:00Z\", \"device\": \"steamdeck\"}",
  "is_public": false
}
```

**Important**: Do NOT send `tags` field when creating/updating notes — it contributes to the `GET /api/roms/{id}/notes` 500 bug. Filter notes by `title == "romm-sync:playtime"` instead.

**API flow**:
1. **Read**: `GET /api/roms/{id}` → filter `all_user_notes` for `title == "romm-sync:playtime"` (bypasses broken GET notes endpoint)
2. **Create**: `POST /api/roms/{id}/notes` with `title`, `content` (JSON string), `is_public: false`
3. **Update**: `PUT /api/roms/{id}/notes/{note_id}` with updated JSON content
4. **Delete**: `DELETE /api/roms/{id}/notes/{note_id}` if needed

**Edge cases**:
- **Multiple devices update simultaneously**: Last write wins. Acceptable for playtime — small deltas mean the worst case is losing one session's worth of time, which is recovered on the next session from that device.
- **Note deleted by user**: Treat as "no server playtime". Re-create on next session end.
- **Migration**: When RomM adds a real playtime API (feature request #1225), stop writing to notes and read from the new field instead.

#### 6. RomM shared account warning
PLAN.md specifies: "Users MUST use their own RomM account (not a shared/generic one) so saves are correctly attributed." Add a warning in ConnectionSettings when the username looks like a shared account:
- Check if username is "admin", "romm", "user", "guest", or similar generic names
- Show an orange warning below the username field: "Save sync requires a personal account. Shared accounts (like 'admin') will mix save files between users."
- Non-blocking — the user can still proceed, it's just informational

#### 7. Save sync feature flag (off by default)

Save sync must be an opt-in feature. Default state: **disabled**. Nothing related to save sync should run, register, or be interactive until the user explicitly enables it.

**New setting**: `save_sync_enabled: boolean` (default: `false`) in `save_sync_state.json` → `settings`.

**Backend guards** (`lib/save_sync.py`):
- `pre_launch_sync()` and `post_exit_sync()` — early return if `save_sync_enabled` is false
- `sync_all_saves()` — early return if disabled
- Device registration (`ensure_device_registered`) — skip if disabled (no device_id generated until first enable)
- New callable `enable_save_sync()` — sets `save_sync_enabled = true`, performs device registration on first enable
- New callable `disable_save_sync()` — sets `save_sync_enabled = false`, does NOT clear existing sync state (preserving snapshot hashes, playtime, etc. so re-enabling is seamless)

**Frontend — session manager** (`src/utils/sessionManager.ts`):
- `initSessionManager()` — check `save_sync_enabled` before registering lifetime/suspend hooks. If disabled, skip all hook registration.
- When the flag is toggled on at runtime, re-initialize the session manager (register hooks). When toggled off, destroy hooks.

**Frontend — QAM navigation** (`src/components/MainPage.tsx`):
- The "Save Sync" navigation button is **hidden** while `save_sync_enabled` is false. The settings page is inaccessible.
- Alternative: show the button but greyed out with a label like "Save Sync (Disabled)" — tapping it goes to the settings page where the user sees the enable toggle prominently at the top. This is better for discoverability.

**Frontend — SaveSyncSettings.tsx**:
- Add a master toggle at the very top of the page: "Enable Save Sync"
- While disabled, all other controls (auto-sync toggles, conflict mode dropdown, sync all button, conflicts list) are visually disabled / non-interactive
- Enabling the toggle triggers a **two-step confirmation**:
  1. First confirmation: warning dialog explaining the feature and risks. Text:
     > "Save Sync will automatically synchronize your RetroArch save files (.srm) with your RomM server. Before enabling, please back up your save files. While unlikely, save data loss is possible due to sync conflicts, network issues, or bugs. Your saves are located at: [saves_path from retrodeck.json]."
  2. Second confirmation: explicit consent. Text:
     > "I have backed up my save files and understand that save data loss is possible. Enable Save Sync?"
     With "Enable" and "Cancel" buttons.
- Only after both confirmations does the toggle flip to enabled and `enable_save_sync()` is called
- Disabling does NOT require confirmation (safe direction — just stops syncing)

**Frontend — GameDetailPanel.tsx**:
- The "Saves & Playtime" section is **greyed out and non-interactive** while `save_sync_enabled` is false
- The manual "Sync" button is disabled
- Show a subtle label: "Save Sync disabled" or "Enable in Save Sync settings"
- Playtime display can remain visible (read-only, harmless) but sync actions must be blocked

**Plugin startup** (`src/index.tsx`):
- Check `save_sync_enabled` before calling `ensureDeviceRegistered()` and `initSessionManager()`
- If disabled, skip both — no device registration, no session hooks, no background sync activity whatsoever

**State persistence**:
- The `save_sync_enabled` flag persists across plugin restarts via `save_sync_state.json`
- Existing sync state (snapshot hashes, playtime, conflicts) is preserved when disabling — re-enabling picks up where it left off
- The flag is independent of `sync_before_launch` / `sync_after_exit` — those sub-toggles only matter when the master flag is on

#### 8. Native Steam playtime display — INVESTIGATED

**Root cause (confirmed by investigation)**:

1. **Primary: Flatpak process sandboxing**. Our launcher does `exec flatpak run net.retrodeck.retrodeck "$ROM_PATH"` — the `exec` is correct (replaces shell PID), but `flatpak run` creates a bubblewrap sandbox with a separate PID namespace. Steam tracks the `flatpak run` PID, which may exit before the sandboxed emulator finishes. Steam sees 0 seconds of playtime.
2. **Secondary: Re-sync app ID instability**. Full re-syncs (DangerZone remove + re-sync) create new app IDs, resetting Steam's playtime counter. Normal syncs correctly preserve app IDs via `existing.get(rom_id)`.
3. **Ruled out: BIsModOrShortcut bypass**. The bypass counter restores `true` during launch (via `GetGameID`/`GetPrimaryAppID` hooks), so Steam's launch pipeline sees shortcuts correctly. Only UI rendering uses `false`.

**Solution: Write our tracked playtime to `SteamAppOverview` via `stateTransaction()`**

`SteamAppOverview` has writable MobX-observed properties:
- `minutes_playtime_forever: number`
- `minutes_playtime_last_two_weeks: number`
- `rt_last_time_played: number` (Unix timestamp)
- `rt_last_time_played_or_installed: number`

We already use `stateTransaction()` to write `controller_support` and `metacritic_score` in `applyDirectMutations()`. Same pattern:

```typescript
function updatePlaytimeDisplay(appId: number, totalMinutes: number) {
  const overview = appStore.GetAppOverviewByAppID(appId);
  if (!overview) return;
  stateTransaction(() => {
    overview.minutes_playtime_forever = totalMinutes;
    overview.rt_last_time_played = Math.floor(Date.now() / 1000);
  });
}
```

**When to call this**:
- On plugin load: iterate all ROM shortcuts in registry, read playtime from `save_sync_state.json`, apply via `stateTransaction()` during `applyDirectMutations()`
- After `recordSessionEnd()` in `sessionManager.ts`: update the just-played game's playtime display immediately
- After sync completes (if app IDs are preserved): re-apply playtime for all synced shortcuts

**Limitations**:
- Values don't survive Steam restarts — they're MobX state, not persisted to Steam's `localconfig.vdf`. We re-apply on every plugin load.
- Steam's own process tracking may overwrite with 0 (due to the flatpak issue). We re-apply after session end to correct this.
- No other Decky plugin (SDH-PlayTime, SteamlessTimes) has solved persistent playtime writes — all use the same workaround approach.

**Future improvement**: Investigate whether `flatpak run` blocks until RetroDECK exits on SteamOS. If it does, Steam's native tracking would work and we'd only need the `stateTransaction` as a supplement. If not, consider a wrapper that polls for the emulator process and keeps the parent alive.

### Verification:
- [ ] Save file uploaded to RomM after play session ends
- [ ] Save file downloaded from RomM before game launch
- [ ] Three-way conflict correctly identified (both sides changed)
- [ ] "Newest wins" resolves based on timestamp comparison
- [ ] "Ask me" queues conflict and shows in detailed popup UI
- [ ] Manual "Sync All Saves Now" processes all installed ROMs
- [ ] Playtime tracked accurately (suspend/resume excluded)
- [ ] Playtime stored in RomM user notes, readable across devices
- [ ] Device registration persists across plugin restarts
- [ ] save_sync_state.json separate from state.json
- [ ] Pre-launch sync does not block game launch on failure
- [ ] Pre-launch and post-exit sync show toast notifications
- [ ] Failed uploads retry 3 times before falling to offline queue
- [ ] User can manually retry failed sync operations
- [ ] Shared account warning shown for generic usernames
- [ ] Save sync disabled by default — no hooks registered, no device registration on fresh install
- [ ] Enabling save sync requires two-step confirmation with backup warning
- [ ] Disabling save sync stops all sync activity without confirmation
- [ ] Save Sync settings page shows master toggle at top, all controls disabled when off
- [ ] Game detail page "Saves & Playtime" section greyed out and non-interactive when disabled
- [ ] QAM "Save Sync" button hidden or visually disabled when feature is off
- [ ] Re-enabling preserves existing sync state (hashes, playtime, conflicts)
- [ ] Plugin startup skips all save sync initialization when disabled
- [ ] Playtime displayed in Steam's native UI or prominently in game detail panel

### Phase 5.5: Custom PlaySection — Native-Looking Game Detail Page

**Goal**: Replace the native Steam PlaySection entirely with a pixel-perfect custom version that provides RomM-specific functionality (download, save sync, BIOS status) while looking indistinguishable from native Steam UI.

**Current state**: We hide the native PlaySection via CSS (`.PlaySection:not([data-romm]) { display: none !important }`) and inject a CustomPlayButton wrapped in native CSS class hierarchy. The button renders correctly with a dropdown menu (Uninstall, BIOS Status, Sync Saves). GameDetailPanel ("ROMM SYNC" section) has been **removed** from game detail page injection — all its functionality is now in the CustomPlayButton dropdown. The old GameDetailPanel component file remains in the codebase but is no longer injected.

**Known issue — shadow/grey line**: Our injected PlaySection wrapper shows a grey shadow line at the bottom that does NOT appear on native Steam PlaySection elements. This indicates the shadow comes from a CSS rule that targets our wrapper specifically (or that native elements have an override we're missing). Current mitigation: `[data-romm] { box-shadow: none !important; border-bottom: none !important; border-image: none !important; background-image: none !important; }` in `styleInjector.ts`. Needs further investigation — inspect native PlaySection's computed styles vs our wrapper's.

#### Research findings

**Native Play button approach — NOT viable**:
- Steam's native Play button text CANNOT be changed for non-Steam shortcuts (no API exists)
- No dropdown/context menu capability exists on the native Play button for shortcuts
- MoonDeck confirmed: adds a SEPARATE MenuButton, does NOT modify the native Play button
- Hybrid approach (native for Play, custom for Download/Conflict) gives inconsistent UX
- **Decision**: Keep custom button replacement approach. Launch interceptor stays as safety net.

**PlaySectionClasses from `@decky/ui`** — 116 properties available via `playSectionClasses`:

Key classes for the info bar (right of Play button):
- `GameStat` — Individual stat item container
- `GameStatIcon` — Icon within a stat
- `GameStatRight` — Right-aligned value portion
- `GameStatsSection` — Container for all stats (horizontal flex)
- `PlayBarDetailLabel` — Small uppercase label text (e.g. "PLAYTIME")
- `DetailsSection` — Container for details area
- `DetailsSectionStatus` — Status indicators
- `ClickablePlayBarItem` — Makes stats interactive
- `CloudStatusIcon`, `CloudStatusLabel`, `CloudStatusRow` — Cloud sync status display
- `CloudStatusSyncFail`, `CloudSynching` — Sync state classes
- `LastPlayedInfo`, `Playtime`, `PlaytimeIcon` — Native playtime display classes

Key classes for button area:
- `AppButtonsContainer` — Layout for buttons
- `MenuButtonContainer` — Dropdown menu button
- `RightControls` — Right-side controls
- `PlayBar` — Main play bar container
- `PlayBarIconAndGame` — Left side with icon + game name

Styling classes:
- `Glassy` — Glass morphism effect
- `BackgroundAnimation` — Animated background
- `BreakNarrow`, `BreakShort`, `BreakWide`, `BreakTall` — Responsive breakpoints

**basicAppDetailsSectionStylerClasses** — wrapper classes:
- `PlaySection` — Outer container
- `AppActionButton` — Button container
- `ActionButtonAndStatusPanel` — Action + status layout

**DOM structure of one info item** (confirmed from HLTB plugin analysis):
```html
<div class="GameStat">
  <div class="PlayBarDetailLabel">PLAYTIME</div>
  <div class="GameStatRight">19 Minutes</div>
</div>
```
All items sit within `GameStatsSection` which provides horizontal flex layout.

**HLTB plugin** — confirmed to use `GameStat` class (8 occurrences in compiled code) for injecting "How Long To Beat" stats into the info row.

**Injection approach for info items**: Can splice into `GameStatsSection.props.children` via React tree patching (same pattern we use for InnerContainer). Alternatively, recreate the full bar using the same CSS classes.

#### Implementation plan: RomMPlaySection component

A unified component that replaces CustomPlayButton and merges relevant parts of GameDetailPanel. Structure:

```
PlaySection wrapper (basicAppDetailsSectionStylerClasses.PlaySection, data-romm="true")
├── AppButtonsContainer (playSectionClasses.AppButtonsContainer)
│   ├── AppActionButton (basicAppDetailsSectionStylerClasses.AppActionButton)
│   │   ├── Primary button (Play / Download / Checking / Resolve Conflict)
│   │   └── Dropdown arrow button (⌄) → Context menu
│   │       ├── Uninstall (when installed)
│   │       ├── BIOS Status → opens BIOS modal
│   │       └── Sync Saves (when installed + save sync enabled)
│   └── Info items row (GameStatsSection)
│       ├── BIOS Status: green ● / orange ● / red ✕ circle + "X/Y files"
│       ├── Last Sync: "5m ago" or "Never" + success/failure indicator
│       └── Playtime: "2h 14m" formatted duration
```

**Button states** (unchanged from CustomPlayButton):
1. **Loading**: Initial mount, returns null
2. **Not RomM**: Not a RomM shortcut, returns null (native UI shows)
3. **Checking**: Spinner + "Checking saves..." + retry count
4. **Download**: Blue "Download" button
5. **Downloading**: Progress bar with percentage
6. **Conflict**: Orange "Resolve Save Conflict" button
7. **Play**: Green "Play" button

**Dropdown menu** (via `showContextMenu` + `Menu`/`MenuItem` from `@decky/ui`):
- **Uninstall**: Only when state === "play" (ROM installed). Calls `removeRom(romId)`, dispatches `romm_rom_uninstalled` event
- **BIOS Status**: Always visible. Opens modal with per-file status list (reuses GameDetailPanel BIOS modal pattern). Label shows counts when BIOS needed
- **Sync Saves**: Only when installed AND `save_sync_enabled`. Calls `syncRomSaves(romId)`, shows toast
- **Refresh Metadata**: Always visible. Calls `fetchSgdbArtwork()` + `getRomMetadata()`

**Info items** (right of buttons, same row):
- **BIOS**: Color-coded circle indicator
  - Green `●` = all BIOS files present
  - Orange `●` = some BIOS files missing
  - Red `✕` = no BIOS files (or platform needs BIOS but none downloaded)
  - Hidden if platform doesn't need BIOS
  - Label: "BIOS" / Value: "Ready" / "2/5" / "Missing"
- **Last Sync**: When save sync enabled and ROM installed
  - Label: "LAST SYNC" / Value: "5m ago" / "Never" / "Failed"
  - Red text for failed, normal for success
- **Playtime**: When any tracked playtime exists
  - Label: "PLAYTIME" / Value: "2h 14m" / "< 1 min"
  - Uses `PlaytimeIcon` class for clock icon if available

**Data sources** (all fetched in single `useEffect` on mount):
- ROM info: `getRomBySteamAppId(appId)` → rom_id, platform_slug, platform_name
- Install status: `getInstalledRom(romId)` → installed path
- BIOS status: `checkPlatformBios(platformSlug)` → needs_bios, file counts
- Save sync settings: `getSaveSyncSettings()` → save_sync_enabled, conflict_mode
- Save status: `getSaveStatus(romId)` → last sync time, playtime
- Pre-launch sync: `preLaunchSync(romId)` → conflict detection

**File changes**:
- **New**: `src/components/RomMPlaySection.tsx` — unified component (~400-500 lines)
- **Modify**: `src/patches/gameDetailPatch.tsx` — inject RomMPlaySection instead of CustomPlayButton
- **Remove from injection**: `src/components/GameDetailPanel.tsx` — no longer injected into game detail page (done). Component file remains for potential future use. BIOS modal, uninstall, save sync moved to CustomPlayButton dropdown.
- **Migrate to**: `src/components/CustomPlayButton.tsx` → `RomMPlaySection.tsx` — add info items row, consolidate logic
- **Modify**: `src/utils/styleInjector.ts` — may need to target more specifically (PlayBar vs full PlaySection) depending on what native elements we want to preserve

**CSS class usage mapping**:

| Element | CSS Class Source | Property |
|---------|-----------------|----------|
| Outer wrapper | `basicAppDetailsSectionStylerClasses` | `PlaySection` |
| Button container | `playSectionClasses` | `AppButtonsContainer` |
| Button wrapper | `basicAppDetailsSectionStylerClasses` | `AppActionButton` |
| Play button | `appActionButtonClasses` | `PlayButtonContainer`, `PlayButton`, `Green` |
| Dropdown menu | `playSectionClasses` | `MenuButtonContainer` |
| Stats container | `playSectionClasses` | `GameStatsSection` |
| Individual stat | `playSectionClasses` | `GameStat` |
| Stat label | `playSectionClasses` | `PlayBarDetailLabel` |
| Stat value | `playSectionClasses` | `GameStatRight` |
| Stat icon | `playSectionClasses` | `GameStatIcon` |
| Playtime icon | `playSectionClasses` | `PlaytimeIcon` |

**Estimated effort**: 500-700 lines of TypeScript, 4-6 hours implementation.

#### Verification:
- [ ] Custom PlaySection visually matches native Steam PlaySection
- [ ] Play/Download/Checking/Conflict button states all work
- [ ] Dropdown menu appears with correct options per state
- [ ] Uninstall from dropdown removes ROM and updates button to Download
- [ ] BIOS modal opens from dropdown with correct file list
- [ ] Sync Saves from dropdown triggers sync and shows toast
- [ ] BIOS indicator: green when ready, orange when partial, red when missing
- [ ] Last sync time displays correctly with success/failure coloring
- [ ] Playtime displays formatted duration
- [ ] Info items hidden when data not available (no BIOS needed, no playtime, sync disabled)
- [ ] Non-RomM games show native PlaySection (no custom replacement)
- [ ] Grey line eliminated from custom PlaySection
- [ ] No visual artifacts (extra borders, shadows, backgrounds) from CSS class usage

### Phase 5.6: Unifideck-Style Game Detail Page Replacement

**Goal**: Replace the entire game detail page content area for RomM games with a custom layout, similar to how Unifideck replaces both PlaySection and the metadata panel. Most native Steam game detail sections (DLC, achievements, community hub, store categories) are useless for ROM games.

**Reference**: Unifideck injects two components into `InnerContainer`:
1. `PlaySectionWrapper` — custom Play/Install button (replaces native PlaySection, hidden via CDP)
2. `GameInfoPanel` — custom metadata panel with: compatibility badge, developer/publisher/release, Metacritic, genres, navigation buttons, synopsis, uninstall

**Our equivalent for RomM games**:
1. **RomMPlaySection** — custom Play/Download button with dropdown (already being built in Phase 5.5)
2. **RomMGameInfoPanel** — custom metadata panel replacing native sections:
   - Platform name and system (e.g. "PlayStation — psx")
   - ROM file info (name, size, multi-disc indicator)
   - BIOS status (color-coded: green/orange/red with file counts, clickable for detail modal)
   - Save sync status (last sync time, success/failure, manual sync button)
   - Playtime (tracked by us, formatted duration)
   - Description/summary (from RomM metadata, already cached)
   - Developer/publisher (from RomM metadata)
   - Genre tags (from RomM metadata)
   - "Refresh Metadata" and "Refresh Artwork" actions

**Hiding native content**: For RomM games, hide or collapse the native metadata sections that don't apply (DLC, achievements, community hub, etc.) while preserving the header/hero area. Approach TBD — either CSS hiding of specific sections or replace AppDetailsRoot content entirely.

**Metadata patches interaction**: Once we have a custom GameInfoPanel, we may no longer need the store object patches (GetDescriptions, GetAssociations, BHasStoreCategory) for the game detail page — the custom panel renders our data directly. However, keep the patches for contexts where native UI still renders (library grid tooltips, etc.).

**Estimated effort**: Medium-large. Build after Phase 5.5 PlaySection is stable.

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

**Related: safety timeout fragility in `_do_sync()`**: The 60-second safety timeout for frontend shortcut processing can fire prematurely for very large libraries (1000+ ROMs with artwork fetching), causing the sync to be reported as failed even though the frontend is still processing. Fix options: make the timeout configurable, scale it based on ROM count (e.g. 60s base + 1s per ROM), or switch to a heartbeat-based approach where the frontend periodically signals it's still working.

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

### Bug 5: SSL Certificate Verification — CRITICAL for Plugin Store

**Symptom**: SGDB API calls fail with certificate errors on Steam Deck. The `ssl.create_default_context()` can't find CA certs in the embedded Python environment.

**Current state**: SSL verification is completely disabled **everywhere** — `lib/romm_client.py` (`_romm_request`, `_romm_download`), `lib/sgdb.py`, and `lib/save_sync.py` (`_romm_json_request`, `_romm_upload_multipart`). Every HTTP helper creates its own `ssl.create_default_context()` with `check_hostname = False` and `verify_mode = ssl.CERT_NONE`. This is 4+ independent locations that all need fixing.

**Proper fix — two tiers**:
- **RomM API calls** (local LAN, self-signed certs common): Add a user-facing setting "Accept self-signed certificates" (default: off). When off, use proper SSL verification. When on, disable verification. This is acceptable for self-hosted services on trusted networks.
- **Public internet APIs** (SteamGridDB): Always use proper SSL verification. Options:
  - Bundle `certifi` package and set `ctx.load_verify_locations(cafile=certifi.where())`
  - Point to the system CA bundle if available (`/etc/ssl/certs/ca-certificates.crt`)
  - Use `requests` library instead of `urllib` (handles certs automatically)
- **Consolidation**: SSL context creation should be extracted into a shared helper (see External Review Findings #6) so this logic lives in one place, not 4+ copies.

This is a security concern — `CERT_NONE` on public APIs allows MITM attacks. Low risk for this use case (API keys, not credentials) but must be fixed before Plugin Store submission.

### Bug 6: Secrets Stored in Plain Text

**Symptom**: `settings.json` stores `romm_pass` and `steamgriddb_api_key` in plain text. Any process or user with read access to `~/homebrew/settings/decky-romm-sync/settings.json` can read credentials.

**Current state**: Decky Loader doesn't provide a secrets/keyring API. All Decky plugins store settings as plain JSON.

**Investigation needed**:
- Check if other Decky plugins (MoonDeck, etc.) encrypt credentials
- Check if Python's `keyring` module is available in Decky's embedded Python
- Consider using OS keyring (libsecret/GNOME Keyring) if available on SteamOS/Bazzite
- Minimum fix: restrict file permissions to owner-only (`chmod 600`)
- Ensure debug logging never exposes these values (mask in logs)

### Bug 7: BIOS Status Reporting Needs Rethinking

**Current behavior**: We compare how many BIOS files exist on RomM for a platform vs how many are downloaded locally. If the counts don't match, we show "missing". The game detail page shows an orange badge with "BIOS required — X/Y downloaded".

**Problems**:
- Not all BIOS files listed in RomM may actually be required for a given game/emulator — some are regional variants, optional files, or for specific emulator backends
- Showing "missing" when optional BIOS files aren't downloaded creates false urgency
- No distinction between "required and missing" vs "optional and missing"
- Need to investigate: does RomM provide any metadata about which BIOS files are required vs optional? Does RetroDECK/RetroArch have a way to check which BIOS a specific core needs?

**Investigation needed**:
- Check what BIOS metadata RomM's firmware API returns (required flag? per-core association?)
- Check if RetroArch/RetroDECK has a BIOS requirements file per core
- Consider showing "X required / Y optional" instead of just "X/Y downloaded"
- Consider only warning when actually required BIOS files are missing

### Bug 8: VDF-Created Shortcut Icons Not Displaying

**Symptom**: Shortcut icons set via VDF/shortcut creation don't display properly in Steam. The icon path is set but Steam doesn't render it.

**Investigation needed**:
- Check how the icon path is set during shortcut creation
- Verify the icon file exists and is in a format Steam accepts (ICO, PNG, TGA)
- Check if `SteamClient.Apps.SetShortcutIcon()` or equivalent is needed after shortcut creation
- Compare with how other plugins (e.g. BoilR, MoonDeck) handle shortcut icons

### Bug 9: Play Button Intermittently Fails on First Click

**Symptom**: Clicking "Play" on a RomM shortcut sometimes does nothing on the first click but works on retry. This is an intermittent timing/race condition in the BIsModOrShortcut bypass counter mechanism (adapted from MetaDeck).

**How the bypass counter works** (`src/patches/metadataPatches.ts`):
- Steam's `BIsModOrShortcut()` controls two things: metadata display (returns false → shows full game details) and launch path (returns true → uses shortcut exe/launch options)
- We need false for rendering (game detail page) but true for launching — these conflict
- Two module-level counters manage the toggle:
  - `bypassCounter`: Set to `-1` (indefinite true) by `GetGameID`/`GetPrimaryAppID` hooks during launch, set to `4` by `BHasRecentlyLaunched`/`GetPerClientData` hooks
  - `bypassBypass`: Set to `11` by `gameDetailPatch.tsx` when navigating to game detail page, forces false for rendering
- `bypassBypass` is checked FIRST (highest priority) — if > 0, always returns false regardless of bypassCounter

**Likely root causes** (in order of probability):
1. **bypassBypass collision with launch**: User clicks Play while game detail page is still rendering. `bypassBypass` is still > 0, takes priority over the launch counter, returns false → Steam skips the shortcut launch path. On retry, bypassBypass has exhausted to 0, launch counter works normally.
2. **Counter exhaustion**: `bypassCounter = 4` but Steam makes > 4 calls to `BIsModOrShortcut` during launch → counter exhausts, returns false on the final check, breaking the launch.
3. **Shared global counter state**: All shortcuts share the same `bypassCounter`. If Steam evaluates multiple shortcuts concurrently, one app's counter reset can interfere with another's launch check.

**Why retry works**: First click fails because counters are in a bad state. Failure resets everything to idle (both counters = 0). Second click starts fresh — `GetGameID` fires, sets `bypassCounter = -1` (indefinite true), launch succeeds.

**Fix options to investigate**:
- Make `bypassBypass` aware of launch state (don't override if launch is in progress)
- Use per-app counters instead of shared global state
- Increase `bypassBypass` exhaustion or clear it when Play is clicked
- Add a small delay between game detail page render and Play button becoming active
- Study how MetaDeck handles this (our implementation was adapted from theirs)

### Verification:
- [ ] Progress bar shows real-time progress during sync
- [ ] Cancel button stops the sync mid-progress
- [ ] Single-disc multi-track game launches via CUE, not bad M3U
- [ ] Startup pruning removes orphaned state entries
- [ ] Partial downloads cleaned up on startup
- [ ] Shortcut icons display correctly in Steam
- [ ] Play button works reliably on first click

---

## Phase 7: Multi-Emulator Support (Deferred)

**Goal**: Support EmuDeck, standalone RetroArch, and manual emulator installs beyond RetroDECK. Also extends save sync to standalone emulators.

### Emulator platform presets:
- **RetroDECK** (current): Paths read from `retrodeck.json` at `~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json` → `paths.roms_path`, `paths.bios_path`, `paths.saves_path`. These vary by install location (internal: `~/retrodeck/`, SD card: `/run/media/deck/Emulation/retrodeck/`).
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

### Standalone emulator save sync:

Phase 5 covers RetroArch `.srm` saves only. This phase adds support for standalone emulator save formats, which use fundamentally different save file structures.

**RetroDECK standalone emulator save directory convention**: All standalone emulators store saves under `<saves_path>/<platform>/<emulator_name>/`. This is distinct from RetroArch saves which go directly into `<saves_path>/<platform>/` as `.srm` files. The `<saves_path>` is read from `retrodeck.json` → `paths.saves_path`.

**Observed save directory structure** (confirmed on local RetroDECK 0.10.3b install):

| Platform | Emulator | Save Path (relative to saves_path) | Format |
|----------|----------|-------------------------------------|--------|
| psx | DuckStation | `psx/duckstation/memcards/` | `.mcd` memory card files (`shared_card_1.mcd`, `shared_card_2.mcd`) |
| ps2 | PCSX2 | `ps2/pcsx2/memcards/` | `.ps2` memory card files |
| gc | Dolphin | `gc/dolphin/{US,EU,JP}/` | Per-region memory card files |
| gc | PrimeHack | `gc/primehack/{US,EU,JP}/` | Per-region memory card files (Dolphin fork) |
| wii | Dolphin | `wii/dolphin/` | Wii save data + `sd.raw` virtual SD card |
| wii | PrimeHack | `wii/primehack/` | Same as Dolphin |
| nds | melonDS | `nds/melonds/` | Per-game `.sav` files |
| n3ds | Azahar | `n3ds/azahar/` | NAND/SDMC title ID structure |
| PSP | PPSSPP | `PSP/PPSSPP-SA/` | Title ID directories under `SAVEDATA/` |
| ps3 | RPCS3 | `ps3/rpcs3/` | Title ID-based save structure |
| psvita | Vita3K | `psvita/vita3k/` | Title ID-based save structure |
| switch | Ryubing | `switch/ryubing/{saveMeta,user,system}/` | User profile-based save data |
| wiiu | Cemu | `wiiu/cemu/` | mlc01 title ID structure |
| xbox | Xemu | `xbox/xemu/` | Xbox HDD image saves |

**Note**: DuckStation save paths are also confirmed in its config (`settings.ini`):
```
Card1Path = <saves_path>/psx/duckstation/memcards/shared_card_1.mcd
Card2Path = <saves_path>/psx/duckstation/memcards/shared_card_2.mcd
```

**PS2 via PCSX2**:
- Shared memory card files in `ps2/pcsx2/memcards/`
- Challenge: entire memory card must be synced (contains saves for multiple games)
- Tracking: system-level rather than per-game; upload/download the whole card

**PSX via DuckStation**:
- Memory card format: `.mcd` files in `psx/duckstation/memcards/`
- Per-game or shared cards depending on DuckStation config
- Same shared-card challenge as PCSX2

**GameCube via Dolphin**:
- Per-game `.gci` save files in region-specific subfolders
- Path: `gc/dolphin/{US,EU,JP}/`
- Region detection needed to find the right subfolder

**PSP via PPSSPP**:
- Save directories named by title ID (e.g. `ULUS10041/`)
- Path: `PSP/PPSSPP-SA/SAVEDATA/{title_id}/`
- Title ID mapping required: ROM filename -> title ID for save discovery

**NDS via melonDS**:
- Per-game `.sav` files (same name as ROM)
- Path: `nds/melonds/`
- Straightforward per-game sync, similar to RetroArch `.srm`

**3DS via Azahar**:
- NAND/SDMC structure: saves stored by title ID
- Complex directory hierarchy under `n3ds/azahar/`
- Title ID mapping required

**Wii U via Cemu**:
- mlc01 directory with title ID structure
- Path: `wiiu/cemu/`
- Title ID mapping required

**Switch via Ryubing (née Ryujinx)**:
- User profile-based save data under `switch/ryubing/user/`
- System-level data under `switch/ryubing/system/`
- Title ID mapping required

**Shared challenges**:
- Title ID mapping: need a database or API to map ROM filenames to emulator-specific title IDs
- Shared memory cards: must sync at system level, not per-game; conflicts affect all games on the card
- Multiple save slots: some emulators support multiple save slots per game
- All paths are relative to `saves_path` from `retrodeck.json` — must never be hardcoded
- **Streaming multipart upload**: Current `_romm_upload_multipart()` reads the entire file into memory (`f.read()`). This is fine for `.srm` files (<64KB) but PS2 memory cards (PCSX2) are 8MB and Wii NAND saves can be larger. When implementing standalone emulator save sync, switch to streaming multipart upload for files over a threshold (e.g. 1MB).

---

## Phase 8: Polish & Advanced Features (Deferred)

**Goal**: Production-ready with good UX and reliability.

- **Multi-version/language ROM selector**: Dropdown when multiple versions exist in RomM
- **Auto sync interval**: Configurable background re-sync
- **Library management**: Detect removed/updated ROMs on RomM, stale state cleanup
- **Offline mode**: Cache lists locally, queue operations
- **Stacked sync progress UI**: Replace single progress bar with phased checklist
- **Error handling**: Retry with backoff, toast notifications, detailed logging
- **Connection settings: remove save button, save on popup confirm**: Each connection field (URL, username, password) already has an edit button that opens a popup. Change behavior so that confirming the popup immediately persists the new value to settings. Cancelling the popup must discard any changes and restore the original value. Remove the global "Save" button entirely — it is no longer needed since each field saves independently on popup confirmation.
- **RomM playtime API integration**: When RomM adds a playtime field (feature request #1225), plug in our existing delta-based accumulation to sync playtime bidirectionally. Architecture is already in place — just needs the API endpoint.
- **Emulator save state sync**: RomM supports "States" (emulator save states / quick saves) separately from "Saves" (SRAM `.srm` files). RetroArch save states live at `<states_path>/{system}/` (path from `retrodeck.json` → `paths.states_path`). These are `.state`, `.state1`, `.state.auto` etc. files. Currently we only sync `.srm` saves — save states are not synced. Challenges: save states are larger (100KB-10MB+), emulator-version-specific (not portable between different RetroArch core versions), and there can be multiple per game (numbered slots + auto). Consider syncing at least the auto-save state for convenience, with a user toggle and size warnings.

---

## External Review Findings

Items from a full code review of the `main` and `feat/phase-5-save-sync` branches. Findings that enriched existing entries are noted inline (Phase 5 #2, Phase 6 Bugs 1/5, Phase 7 shared challenges). The items below are genuinely new — not tracked elsewhere in this plan.

### EXT-1: Platform Map Caching

`_load_platform_map()` reads `config.json` from disk on every `_resolve_system()` call. During batch operations (sync, downloads), this file is read repeatedly for each ROM.

**Recommendation**: Cache the platform map once during plugin initialization or on first access, with a manual invalidation when settings change. Low priority given current usage patterns, but becomes relevant for large libraries (1000+ ROMs).

### EXT-2: Atomic Writes for Settings

`_save_state()` correctly uses atomic writes (write to `.tmp`, then `os.replace()` to final path). `_save_save_sync_state()` also does this correctly. However, `_save_settings_to_disk()` writes directly to the target file without atomic operation.

**Risk**: Corrupted `settings.json` if the plugin crashes or power is lost during a write.

**Fix**: Apply the same `.tmp` + `os.replace()` pattern to `_save_settings_to_disk()`. Small fix, one consistent pattern everywhere.

### EXT-3: Download Queue Memory Growth

`_download_queue` dict grows indefinitely — completed and failed download entries are never pruned. For users who install and uninstall many ROMs over time, this is a slow memory leak.

**Recommendation**: Prune completed/failed entries after N days (e.g. 7), or implement a max queue size (e.g. 100 completed entries).

### EXT-4: HTTP Client Code Duplication

There are now four places that independently build SSL contexts + Basic Auth headers:
- `_romm_request()` in `lib/romm_client.py`
- `_romm_download()` in `lib/romm_client.py`
- `_romm_json_request()` in `lib/save_sync.py`
- `_romm_upload_multipart()` in `lib/save_sync.py`

Each one creates its own `ssl.create_default_context()`, builds credentials with `base64.b64encode()`, and adds auth headers independently.

**Recommendation**: Extract shared helpers (e.g. `_get_ssl_context()` + `_get_auth_header()`) in `romm_client.py` that all HTTP methods use. Critical for:
- The SSL verification fix (Phase 6 Bug 5) — currently needs changes in 4+ places
- Future auth changes (e.g. OAuth2 bearer support mentioned in Key Decisions)
- Consistency in timeout handling

Consider as part of a Phase 4.5-style restructuring pass.

### EXT-5: Blocking I/O Assumption in Async Callables

`pre_launch_sync()`, `post_exit_sync()`, and `sync_all_saves()` are async callables that internally call synchronous `_sync_rom_saves()`, which uses `time.sleep()` in `_with_retry()`. This blocks the event loop during retries.

**Current impact**: None — Decky's `callable()` mechanism runs these in a thread pool, so blocking is fine.

**Risk**: If Decky's threading model changes in a future version, this becomes a problem.

**Documentation note**: Assumes Decky `callable()` executes in thread pool. If this changes, wrap `_sync_rom_saves` calls in `run_in_executor`.

### EXT-6: Shell Interpolation in `bin/romm-launcher`

`bin/romm-launcher` interpolates `$STATE_FILE` and `$ROM_ID` into Python strings. `ROM_ID` is regex-validated (digits only), so this is safe in practice, but the pattern is fragile.

**Recommendation**: Pass `ROM_ID` and `STATE_FILE` as environment variables or command-line arguments to the Python snippet instead of string interpolation. Low priority given the validation already in place.

### EXT-7: No Rate Limiting on RomM API Calls During Sync

During a full sync, the backend fires rapid sequential requests to the RomM API (list platforms, paginate ROMs, fetch artwork per ROM). For large libraries this could be hundreds of requests in quick succession. If RomM has rate limiting or if the server is resource-constrained, this could cause failures.

**Recommendation**: Add a configurable delay between API calls during batch operations (e.g. `sync_api_delay_ms`, default 0 for LAN, adjustable for remote/slow servers). Low priority for typical homelab use.

---

## Future Improvements (nice-to-have)

- **Concurrent download queue**: Support multiple queued downloads instead of one at a time.
- **RomM native device sync**: When RomM v4.7+ ships device sync features, migrate from our own conflict resolution.
- **Download queue priority/reordering**: Let users reorder queued downloads.
- **Developer vs Publisher distinction**: RomM's `companies` is a flat list with no role info. Research IGDB's `involved_companies` relationship (has `developer` and `publisher` boolean flags) to properly split companies. May need an extra API call to IGDB or a RomM enhancement.
- **BIsModOrShortcut bypass counter**: MetaDeck uses a counter system to let `BIsModOrShortcut` return `true` for specific internal Steam calls (GetGameID, GetPrimaryAppID, GetPerClientData, BHasRecentlyLaunched) while returning `false` for UI rendering. Our simple "always false for our apps" approach works for now since ROM shortcuts are cleanly in the non-Steam ID range. Implement if users report: broken play time tracking, missing from Recently Played, or console errors about app ID lookups. ~80 lines, small-medium effort.
- **RetroAchievements integration**: Show and track RetroAchievements for games. RomM supports adding RA data. Research needed: fetch from RomM's RA data vs. query RetroAchievements API directly vs. leverage an existing Decky plugin (e.g. there may be a dedicated RA Decky plugin). Display options: badge on game detail page, achievement list overlay, progress tracking.
- **Sync completion notification accuracy**: The post-sync toast always reports "Added X games" using the total count of shortcuts processed. This is misleading — re-syncing an unchanged library shows the same count as a fresh sync. Fix: track which shortcuts are truly new (didn't exist before) vs. updated (already existed, metadata/artwork refreshed) vs. unchanged (skipped entirely). Display an accurate breakdown like "Added 3 new, updated 12, 485 unchanged" or just "Library up to date" when nothing changed. Requires comparing incoming ROM data against `shortcut_registry` before processing and counting each outcome category in `syncManager.ts`.
- **Playtime sync between RomM and Steam**: Sync playtime data bidirectionally between Steam's per-shortcut playtime and RomM's per-ROM playtime tracking. Challenges: merging playtime across multiple devices (additive — sum deltas, not overwrite), deciding sync direction (Steam → RomM after play sessions, RomM → Steam on sync for display), and displaying it in Steam's native playtime field (research if `SteamClient.Apps` or store patching can set the played-time value). Could also surface RomM's cross-device total playtime in the game detail panel.
- **UI settings page**: Add a settings sub-page for UI display preferences. Toggleable options with sensible defaults:
  - **Machine-scoped collections** (default: on): Append hostname to collection names, e.g. "RomM: N64 (steamdeck)". When off, use plain "RomM: N64".
  - **Device availability labels** (default: on): Show "Also on [device]" / "Streamable from [device]" in game detail panel. When off, hide device info.
  - **Custom device display name**: Override the hostname shown in collections and labels (e.g. "Deck" instead of "steamdeck-12345").
  - Future UI toggles (metadata display, artwork preferences, etc.) would live here too.
- **Per-game sync selection**: Currently sync is all-or-nothing per platform. Add ability to select/deselect individual games within a platform for syncing. UI: game list with checkboxes on the Platforms settings page (expand a platform to see its games). Backend: per-ROM `sync_enabled` flag in shortcut_registry, checked during sync to skip deselected games. Useful for large platforms where only a subset of games are wanted.
- **Translations / i18n**: All user-facing strings (button labels, toasts, settings, modals) are currently hardcoded in English. Add i18n support so the plugin adapts to the user's Steam language. Reference: Unifideck uses `src/i18n/` with per-language JSON files. Decky plugins can read Steam's language setting to pick the right locale.
