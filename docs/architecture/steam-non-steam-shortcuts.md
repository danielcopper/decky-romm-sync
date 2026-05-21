# Steam Non-Steam Shortcuts

Technical reference for how decky-romm-sync creates, manages, and launches non-Steam shortcuts. This covers the `SteamClient.Apps.AddShortcut` API, VDF format details, and app ID handling.

## AddShortcut API Behavior

### Signature

```typescript
SteamClient.Apps.AddShortcut(name: string, exe: string, startDir: string, launchOptions: string): Promise<number>
```

Returns the new shortcut's `appId` (a number), or `0`/`null` on failure.

### What it actually does

Despite accepting four parameters, `AddShortcut` **ignores `startDir` and `launchOptions`**. This was confirmed by the [MoonDeck plugin](https://github.com/FrogTheFrog/moondeck) developers. Only `name` and `exe` are used during creation.

To set all shortcut properties reliably, call the `Set*` methods **after a 500ms delay**:

```typescript
const appId = await SteamClient.Apps.AddShortcut(name, exe, "", "");
await delay(500);

SteamClient.Apps.SetShortcutName(appId, name);
SteamClient.Apps.SetShortcutExe(appId, exe);
SteamClient.Apps.SetShortcutStartDir(appId, startDir);
SteamClient.Apps.SetAppLaunchOptions(appId, launchOptions);
```

The 500ms delay is critical. Without it, the `Set*` calls may silently fail because Steam has not finished registering the new app internally.

### Exe quoting

**Do NOT pass quoted exe paths to `AddShortcut` or `SetShortcutExe`.** The API handles quoting internally. Passing `"\"path/to/exe\""` (pre-quoted) results in double-quoting, which causes launches to fail with "file not found."

Pass the raw path:

```typescript
SteamClient.Apps.SetShortcutExe(appId, "/home/deck/homebrew/plugins/decky-romm-sync/bin/romm-launcher");
```

### Updating existing shortcuts

Calling `Set*` methods on an existing shortcut to change its `exe`, `startDir`, or `launchOptions` **may not take effect reliably**. If launch configuration needs to change, the safest approach is to delete and recreate the shortcut (re-sync).

Simple display property changes (name, artwork) work fine via `Set*`.

See: `src/utils/steamShortcuts.ts`

## BIsModOrShortcut

Non-Steam shortcuts return `BIsModOrShortcut() = true` by default. This is their natural state — Steam uses this flag to determine how to render and launch an app.

An earlier version of the plugin used a "bypass counter" pattern (inspired by MetaDeck) to temporarily return `false` from `BIsModOrShortcut()` so that Steam would render metadata sections (description, developer, etc.) on the game detail page. This approach was **dropped in Phase 5.6** because it caused launch failures — Steam skips the shortcut launch path when `BIsModOrShortcut()` returns `false`.

The current approach owns the entire game detail UI via custom React components (`RomMPlaySection`, `RomMGameInfoPanel`, `CustomPlayButton`) injected through route patching. This avoids fighting Steam's internal rendering logic.

See: `src/patches/gameDetailPatch.tsx`, `src/components/RomMPlaySection.tsx`

## VDF Format Notes

Shortcut creation goes through the frontend `SteamClient.Apps.AddShortcut()` API — `AddShortcut` returns the real `appId` directly, so the plugin never computes app IDs itself for shortcut creation. VDF read/write support remains in the backend `SteamConfigAdapter` (`adapters/steam_config.py`) for reading the existing shortcut set and writing shortcut icons into the grid directory.

### shortcuts.vdf structure

Steam stores non-Steam shortcuts in a binary VDF file at:

```text
~/.local/share/Steam/userdata/<user_id>/config/shortcuts.vdf
```

Each entry has these key fields:

| VDF Field | Format | Notes |
| --- | --- | --- |
| `AppName` | string | Display name |
| `Exe` | string | **Quoted** path: `"/path/to/exe"` |
| `StartDir` | string | **Quoted** path: `"/path/to/dir"` |
| `LaunchOptions` | string | Unquoted: `romm:42` — the ROM marker `bin/romm-launcher` parses |
| `appid` | signed int32 | Assigned by Steam when `AddShortcut` runs; stored as the signed int32 form (`to_signed_app_id`) |
| `icon` | string | Icon path or hash |
| `tags` | object | Steam collection tags. The plugin manages collections via `collectionStore` (machine-scoped names like `RomM: N64 (steamdeck)`), not by writing this VDF field. |

### AddShortcut vs VDF quoting

When the backend `SteamConfigAdapter` writes directly to `shortcuts.vdf`, the `Exe` and `StartDir` fields **must** be wrapped in double quotes:

```python
entry = {
    "Exe": f'"{exe}"',        # VDF requires quotes
    "StartDir": f'"{start_dir}"',
}
```

When using `SteamClient.Apps.AddShortcut()` (the path shortcut creation goes through), **do NOT quote** — the API adds quotes internally.

See: `py_modules/adapters/steam_config.py`

## App IDs and Artwork

`SteamClient.Apps.AddShortcut()` returns the real `appId`, so the plugin does **not** compute shortcut app IDs from `exe + name`. The frontend stores the returned `appId` and the backend registry keys ROM entries by it. There is no CRC32 app-ID generator in the codebase.

The only app-ID math the backend does is converting an unsigned Steam app ID to its signed int32 form for `shortcuts.vdf` records — `to_signed_app_id(app_id)` in `py_modules/domain/sgdb_artwork.py`. SGDB endpoint/asset-type maps live in the same module.

### Artwork file naming

Grid artwork is stored at `userdata/<user_id>/config/grid/`, keyed by the shortcut's real `appId`:

| Suffix | Artwork Type |
| --- | --- |
| `<appId>p.png` | Portrait grid (cover) |
| `<appId>_hero.png` | Hero banner |
| `<appId>_logo.png` | Logo overlay |
| `<appId>.png` | Wide grid / horizontal |
| `<appId>_icon.png` | Icon |

`ArtworkService` (cover staging/finalisation, renaming the staged cover to `{app_id}p.png`) and `SteamGridService` (SGDB hero/logo/grid/icon, writing the icon into the grid dir) own the artwork flow. Icon writes go through `SteamConfigAdapter.write_shortcut_icon`.

## Key Files

| File | Purpose |
| --- | --- |
| `src/utils/steamShortcuts.ts` | `addShortcut()`, `removeShortcut()`, `getExistingRomMShortcuts()` — frontend shortcut CRUD |
| `src/utils/syncManager.ts` | Listens for sync events, orchestrates shortcut creation/removal, artwork application, collection management |
| `src/utils/collections.ts` | Machine-scoped Steam collection management |
| `src/patches/gameDetailPatch.tsx` | Route patch for `/library/app/:appid` — injects RomMPlaySection for custom game detail UI |
| `src/patches/metadataPatches.ts` | Store patches for description, associations, categories, release date display |
| `py_modules/adapters/steam_config.py` | `SteamConfigAdapter` — VDF read/write, grid dir, shortcut icon write, Steam Input config |
| `py_modules/services/library/` | LibraryService — builds shortcut data, drives per-unit sync apply |
| `py_modules/domain/sgdb_artwork.py` | `to_signed_app_id`, SGDB asset-type/endpoint maps |
| `bin/romm-launcher` | Bash script invoked by Steam — parses `romm:<id>`, looks up ROM path, launches RetroDECK |

## Common Pitfalls

### Quoting exe breaks launches

Pre-quoting the exe path in `AddShortcut` or `SetShortcutExe` causes double-quoting. Steam tries to execute `""/path/to/exe""` and fails with "file not found." Always pass raw paths through the SteamClient API.

### Empty Set* params after AddShortcut

Calling `Set*` methods too quickly after `AddShortcut` (before the 500ms delay) results in the properties not being saved. The shortcut appears in the library but with wrong or missing exe/startDir/launchOptions. Launches fail or open the wrong thing.

### Shortcut property updates are unreliable

Changing `exe`, `startDir`, or `launchOptions` on existing shortcuts via `Set*` calls sometimes does not persist. The workaround is to delete and recreate the shortcut. The sync engine handles this by processing removals before additions.

### AddShortcut timing between shortcuts

When creating multiple shortcuts in a loop, a 50ms delay between each `addShortcut()` call prevents corrupting Steam's internal shortcut state. Without this delay, some shortcuts may silently fail to register.
