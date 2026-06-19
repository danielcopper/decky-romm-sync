# Steam Non-Steam Shortcuts

Technical reference for how decky-romm-sync creates, manages, and launches non-Steam shortcuts. This covers the
`SteamClient.Apps.AddShortcut` API, VDF format details, and app ID handling.

## AddShortcut API Behavior

### Signature

```typescript
SteamClient.Apps.AddShortcut(name: string, exe: string, startDir: string, launchOptions: string): Promise<number>
```

Returns the new shortcut's `appId` (a number), or `0`/`null` on failure.

### What it actually does

Despite accepting four parameters, `AddShortcut` **ignores `startDir` and `launchOptions`**. This was confirmed by the
[MoonDeck plugin](https://github.com/FrogTheFrog/moondeck) developers. Only `name` and `exe` are used during creation.

To set all shortcut properties reliably, call the `Set*` methods **after a 500ms delay**:

```typescript
const appId = await SteamClient.Apps.AddShortcut(name, exe, "", "");
await delay(500);

SteamClient.Apps.SetShortcutName(appId, name);
SteamClient.Apps.SetShortcutExe(appId, exe);
SteamClient.Apps.SetShortcutStartDir(appId, startDir);
SteamClient.Apps.SetAppLaunchOptions(appId, launchOptions);
```

The 500ms delay is critical. Without it, the `Set*` calls may silently fail because Steam has not finished registering
the new app internally.

### Exe quoting

**Do NOT pass quoted exe paths to `AddShortcut` or `SetShortcutExe`.** The API handles quoting internally. Passing
`"\"path/to/exe\""` (pre-quoted) results in double-quoting, which causes launches to fail with "file not found."

Pass the raw path:

```typescript
SteamClient.Apps.SetShortcutExe(appId, "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher");
```

### Updating existing shortcuts

A shortcut's `appId` is derived from `exe + appName` (CRC32). Two consequences follow:

- **`launchOptions` and `startDir` are appId-safe.** Changing either on an existing shortcut keeps the same `appId`, so
  the shortcut's identity, artwork, collection membership, and `roms.shortcut_app_id` binding all survive.
  `SetAppLaunchOptions` on an existing shortcut is **reliable** — confirmed on hardware in
  [#827](https://github.com/danielcopper/decky-romm-sync/issues/827) across in-session writes, a Steam restart, and
  removal-churn re-syncs. The plugin uses it directly to bake the launch command in at download-complete and to
  re-resolve paths after a RetroDECK-home migration.
- **`exe` and `appName` are destructive.** Changing either yields a _different_ `appId` — effectively a new shortcut. A
  launch-config change that touches `exe` or the name therefore requires delete + recreate (re-sync); a
  `launchOptions`-only change does not.

Because `SetAppLaunchOptions` returns `void` with no success signal, the plugin **fires the set then polls**
`RegisterForAppDetails` until the read-back `strLaunchOptions` matches (`setLaunchOptionsConfirmed`). Setting `""` — the
placeholder an uninstalled ROM carries until it is downloaded — is valid and confirms against an empty read-back.

The real hazard is not the set: heavy removal-churn can corrupt Steam's in-memory shortcut state. A Steam restart clears
it. The sync engine processes removals before additions to minimise churn.

See: `src/utils/steamShortcuts.ts`

## BIsModOrShortcut

Non-Steam shortcuts return `BIsModOrShortcut() = true` by default. This is their natural state — Steam uses this flag to
determine how to render and launch an app.

An earlier version of the plugin used a "bypass counter" pattern (inspired by MetaDeck) to temporarily return `false`
from `BIsModOrShortcut()` so that Steam would render metadata sections (description, developer, etc.) on the game detail
page. This approach was **dropped in Phase 5.6** because it caused launch failures — Steam skips the shortcut launch
path when `BIsModOrShortcut()` returns `false`.

The current approach owns the entire game detail UI via custom React components (`RomMPlaySection`, `RomMGameInfoPanel`,
`CustomPlayButton`) injected through route patching. This avoids fighting Steam's internal rendering logic.

See: `src/patches/gameDetailPatch.tsx`, `src/components/RomMPlaySection.tsx`

## VDF Format Notes

Shortcut creation goes through the frontend `SteamClient.Apps.AddShortcut()` API — `AddShortcut` returns the real
`appId` directly, so the plugin never computes app IDs itself for shortcut creation. VDF read/write support remains in
the backend `SteamConfigAdapter` (`adapters/steam_config.py`) for reading the existing shortcut set and writing shortcut
icons into the grid directory.

### shortcuts.vdf structure

Steam stores non-Steam shortcuts in a binary VDF file at:

```text
~/.local/share/Steam/userdata/<user_id>/config/shortcuts.vdf
```

Each entry has these key fields:

| VDF Field       | Format       | Notes                                                                                                                                                                                                                                                 |
| --------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AppName`       | string       | Display name                                                                                                                                                                                                                                          |
| `Exe`           | string       | **Quoted** path: `"/path/to/exe"`                                                                                                                                                                                                                     |
| `StartDir`      | string       | **Quoted** path: `"/path/to/dir"`                                                                                                                                                                                                                     |
| `LaunchOptions` | string       | The full launch command the `bin/rom-launcher` exec wrapper runs, e.g. `flatpak run net.retrodeck.retrodeck "/path/to/game.iso"` — or `""` (placeholder) for an uninstalled ROM. No `romm:<id>` marker; ownership is detected by the exe path instead |
| `appid`         | signed int32 | Assigned by Steam when `AddShortcut` runs; stored as the signed int32 form (`to_signed_app_id`)                                                                                                                                                       |
| `icon`          | string       | Icon path or hash                                                                                                                                                                                                                                     |
| `tags`          | object       | Steam collection tags. The plugin manages collections via `collectionStore` (machine-scoped names like `RomM: N64 (steamdeck)`), not by writing this VDF field.                                                                                       |

### AddShortcut vs VDF quoting

When the backend `SteamConfigAdapter` writes directly to `shortcuts.vdf`, the `Exe` and `StartDir` fields **must** be
wrapped in double quotes:

```python
entry = {
    "Exe": f'"{exe}"',        # VDF requires quotes
    "StartDir": f'"{start_dir}"',
}
```

When using `SteamClient.Apps.AddShortcut()` (the path shortcut creation goes through), **do NOT quote** — the API adds
quotes internally.

See: `py_modules/adapters/steam_config.py`

## Collection management

Steam collections are managed entirely on the frontend via `collectionStore`, not by writing the shortcut's `tags` VDF
field. The plugin owns machine-scoped collections named `RomM: <platform> (<hostname>)` for platforms and
`RomM: [<name>] (<hostname>)` for synced RomM collections. The `sync_complete` event carries `platform_app_ids` and
`romm_collection_app_ids` maps; `onSyncComplete` (`src/index.tsx`) creates/updates the collections for the maps it
receives and then runs a **stale-collection cleanup** that deletes any `RomM: …` collection for this machine whose
platform/collection name is absent from those maps.

The cleanup is **gated on a completed (non-cancelled) sync** (`!data.cancelled`). On a cancelled run the maps are
**partial** — they list only the platforms the run reached before the cancel (empty if the cancel fired before the first
unit), because the backend builds `platform_app_ids` from the cross-unit accumulator of reached platforms. Treating a
partial map as the authoritative active-set would delete the collections for unreached platforms — an early cancel would
wipe the entire library organization. The additive create/update path stays ungated, so the platforms that did complete
still get their collections; only the destructive deletion is skipped on cancel. Steam collections are not backed up, so
the safe behavior on a partial/cancelled run is to delete nothing.

## App IDs and Artwork

`SteamClient.Apps.AddShortcut()` returns the real `appId`, so the plugin does **not** compute shortcut app IDs itself —
there is no CRC32 app-ID generator in the codebase. Steam derives the `appId` from `exe + appName` (CRC32), which is why
mutating `launchOptions` or `startDir` keeps the same `appId` (see
[Updating existing shortcuts](#updating-existing-shortcuts)) while changing `exe`/name produces a different one. The
frontend stores the returned `appId` and the backend persists it as `shortcut_app_id` on the ROM's `roms` row (the
synced-ROM registry; reverse-lookupable by `shortcut_app_id`). The frontend resolves rom_id ↔ appId through the
backend's `get_app_id_rom_id_map()` callable, which reads that binding.

The only app-ID math the backend does is converting an unsigned Steam app ID to its signed int32 form for
`shortcuts.vdf` records — `to_signed_app_id(app_id)` in `py_modules/domain/sgdb_artwork.py`. SGDB endpoint/asset-type
maps live in the same module.

### Artwork file naming

Grid artwork is stored at `userdata/<user_id>/config/grid/`, keyed by the shortcut's real `appId`:

| Suffix             | Artwork Type           |
| ------------------ | ---------------------- |
| `<appId>p.png`     | Portrait grid (cover)  |
| `<appId>_hero.png` | Hero banner            |
| `<appId>_logo.png` | Logo overlay           |
| `<appId>.png`      | Wide grid / horizontal |
| `<appId>_icon.png` | Icon                   |

`ArtworkService` (cover staging/finalisation, renaming the staged cover to `{app_id}p.png`) and `SteamGridService` (SGDB
hero/logo/grid/icon, writing the icon into the grid dir) own the artwork flow. Icon writes go through
`SteamConfigAdapter.write_shortcut_icon`.

## Key Files

| File                                  | Purpose                                                                                                                                                                                                                                                      |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `src/utils/steamShortcuts.ts`         | `addShortcut()`, `removeShortcut()`, `getExistingRomMShortcuts()` — frontend shortcut CRUD. The existing-shortcut scan emits a sync heartbeat every 10s between batches so a large library can't stall the run past the backend's per-unit heartbeat timeout |
| `src/utils/syncManager.ts`            | Listens for sync events, orchestrates shortcut creation/removal, artwork application, collection management. Caches the existing-shortcut scan per run (keyed by the `sync_apply_unit` `run_id`) so it scans Steam once per run, not once per unit           |
| `src/utils/collections.ts`            | Machine-scoped Steam collection management                                                                                                                                                                                                                   |
| `src/patches/gameDetailPatch.tsx`     | Route patch for `/library/app/:appid` — injects RomMPlaySection for custom game detail UI                                                                                                                                                                    |
| `src/patches/metadataPatches.ts`      | Store patches for description, associations, categories, release date display                                                                                                                                                                                |
| `py_modules/adapters/steam_config.py` | `SteamConfigAdapter` — VDF read/write, grid dir, shortcut icon write, Steam Input config                                                                                                                                                                     |
| `py_modules/services/library/`        | LibraryService — builds shortcut data, drives per-unit sync apply                                                                                                                                                                                            |
| `py_modules/domain/sgdb_artwork.py`   | `to_signed_app_id`, SGDB asset-type/endpoint maps                                                                                                                                                                                                            |
| `bin/rom-launcher`                    | Pure `exec "$@"` wrapper invoked by Steam — runs the full launch command baked into the shortcut's launch options; owns no state, no path resolution, no emulator knowledge                                                                                  |

## Common Pitfalls

### Quoting exe breaks launches

Pre-quoting the exe path in `AddShortcut` or `SetShortcutExe` causes double-quoting. Steam tries to execute
`""/path/to/exe""` and fails with "file not found." Always pass raw paths through the SteamClient API.

### Empty Set* params after AddShortcut

Calling `Set*` methods too quickly after `AddShortcut` (before the 500ms delay) results in the properties not being
saved. The shortcut appears in the library but with wrong or missing exe/startDir/launchOptions. Launches fail or open
the wrong thing.

### Removal-churn can corrupt shortcut state

`SetAppLaunchOptions` on an existing shortcut is reliable (validated in
[#827](https://github.com/danielcopper/decky-romm-sync/issues/827); see
[Updating existing shortcuts](#updating-existing-shortcuts)) — the historical "property updates may not persist" warning
has been narrowed. The remaining hazard is **removal-churn**: adding and removing many shortcuts in one pass can corrupt
Steam's in-memory shortcut state. A Steam restart clears it. The sync engine processes removals before additions to keep
churn down, and every launch-options write uses the fire-then-poll `setLaunchOptionsConfirmed` so a silently dropped
write is observable rather than assumed. Only `exe`/name changes still need delete + recreate, because those change the
`appId`.

### AddShortcut timing between shortcuts

When creating multiple shortcuts in a loop, a 50ms delay between each `addShortcut()` call prevents corrupting Steam's
internal shortcut state. Without this delay, some shortcuts may silently fail to register.
