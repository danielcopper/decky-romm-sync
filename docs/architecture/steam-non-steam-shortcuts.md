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

### appId reuse across a server switch / re-import

Because the appId is `CRC32(exe + name)` and both are **stable for a given ROM across syncs** (the exe is the constant
`…/bin/rom-launcher`, the name is the RomM `name`), the same game always hashes to the **same appId** — even after its
server-issued `rom_id` changes. Switching the RomM server URL (or re-importing on the same server) reissues `rom_id`s;
the `roms` rows survive (ADR-0007 retention) and the new `rom_id` for an unchanged game resolves to the appId the old
`rom_id` already holds.

Two guards keep this from wiping a freshly-synced shortcut (`#1036`):

- **One appId, one bound row.** `SqliteRomRepository.save()` unbinds any sibling row holding the appId before the
  per-`rom_id` UPSERT, and migration `003`'s partial unique index on `shortcut_app_id` enforces it (see
  [Database Design](database-design.md)). A re-import never leaves two bound rows sharing one appId.
- **Stale-removal excludes appIds bound this run.** The finalize stale pass flags bound rows whose `rom_id` wasn't
  synced this run — which includes the old colliding `rom_id`. `domain/sync_diff.py:select_stale_removals` removes any
  candidate whose appId is in the run's `committed_app_ids` (every appId bound this run, across both the happy-path and
  the heartbeat-timeout late-ack commit paths), so the appId the run just bound to the new `rom_id` is never emitted for
  removal. The `get_by_app_id` reverse lookup orders `rom_id DESC LIMIT 1` so it resolves the live (newest) binding for
  any pre-migration edge state.

## Sync-start reconcile of Steam-UI-deleted shortcuts

A user can delete a RomM shortcut through **Steam's own UI** (remove from library), which the plugin never observes. The
`roms` row keeps its now-dead `shortcut_app_id`, so `get_app_id_rom_id_map` keeps serving it (playtime writes and
launch-options bakes aim at a Steam app that no longer exists) and the **incremental skip never recreates it**: the skip
counts bound `roms` rows, not live Steam shortcuts, so the platform reports "unchanged" forever. The game stays gone
until a server-side change or a Force Full Sync (`#1046`).

The fix is a **frontend-assisted reconcile at sync start**, because only the frontend can read Steam's shortcut store.
It runs **before** the sync builds its work queue — so the unbind lands before the incremental-skip decision — on both
the skip-preview (`start_sync`) and preview (`sync_preview`) paths:

1. `getLiveRomMShortcutAppIds()` (`src/utils/steamShortcuts.ts`) scans Steam's live shortcuts and returns the raw appIds
   of every RomM-owned shortcut (exe ends with `/bin/rom-launcher`), regardless of any backend binding. It returns
   `null` when the store was **unreadable** (`collectionStore` absent) versus `[]` when the scan **ran and found none**
   — a load-bearing distinction.
2. `reconcileStaleShortcuts()` (`src/utils/syncManager.ts`) skips the reconcile on a `null` scan (reconciling against
   "couldn't look" would unbind every binding), and otherwise calls the `reconcile_shortcuts` callable with the live
   set. It is best-effort: a scan or backend failure is logged and swallowed, never blocking the sync.
3. `ShortcutRemovalService.reconcile_live_shortcuts` unbinds every bound `roms` row whose `shortcut_app_id` is **not**
   in the live set — clearing only the binding (`Rom.unbind_shortcut`, ADR-0007), never deleting the row or its per-ROM
   children. An empty live set is the correct "they're all gone" signal and unbinds every binding.

Once a row is unbound, the fetcher's incremental baseline (`_read_incremental_baseline`, which reconstructs only rows
with a non-NULL `shortcut_app_id`) no longer counts it, so `unit.rom_count == registry_count` fails and the platform
falls through to a full fetch that recreates the shortcut. The unbind is reversible by design — the next sync re-binds.

This is **eager (sync-start) reconciliation of the Steam-shortcut binding**, distinct from `#951`'s lazy on-access
reconciliation of the `rom_installs` (on-disk install) view: a different aggregate, a different cost driver, and —
unlike installs — one the backend physically cannot reconcile lazily, since no per-game backend seam observes Steam's
shortcut store.

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

## Overview metadata mutations (readiness-gated)

Beyond the custom UI, the plugin writes three fields directly onto each RomM shortcut's `SteamAppOverview` so the
shortcut presents like a native Steam game: `controller_support = 2` (the "Full Controller Support" badge — important so
Game Mode doesn't flag the controller-driven RetroDECK launch), `metacritic_score` (from RomM's `average_rating`), and
`m_setStoreCategories` (RomM's `steam_categories`).

Steam rebuilds `appStore` from scratch on every `SharedJSContext` mount, so these in-memory mutations are lost on each
reload and must re-apply per mount. `registerMetadataPatches` builds the appId→romId map; `applyAllMetadata` then
applies the mutations with a **readiness retry** (the same `[0, 1s, 3s, 5s]` ladder as `applyAllPlaytime`). Without the
retry the pass runs before `appStore` is populated and silently no-ops on a cold boot, so the badge/rating/categories
never appear until a later mount (#1203). The mutations are idempotent, so retries are safe.

See: `src/patches/metadataPatches.ts`

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

| File                                      | Purpose                                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/utils/steamShortcuts.ts`             | `addShortcut()`, `removeShortcut()`, `getExistingRomMShortcuts()`, `getLiveRomMShortcutAppIds()` (raw live appId scan for the sync-start reconcile) — frontend shortcut CRUD. The existing-shortcut scan emits a sync heartbeat every 10s between batches so a large library can't stall the run past the backend's per-unit heartbeat timeout |
| `src/utils/syncManager.ts`                | Listens for sync events, orchestrates shortcut creation/removal, artwork application, collection management. `reconcileStaleShortcuts()` runs the sync-start reconcile of Steam-UI-deleted shortcuts. Caches the existing-shortcut scan per run (keyed by the `sync_apply_unit` `run_id`) so it scans Steam once per run, not once per unit    |
| `py_modules/services/shortcut_removal.py` | `ShortcutRemovalService` — resolves shortcut-removal sets, unbinds removed ROMs, and runs `reconcile_live_shortcuts` (the sync-start reconcile of Steam-UI-deleted bindings)                                                                                                                                                                   |
| `src/utils/collections.ts`                | Machine-scoped Steam collection management                                                                                                                                                                                                                                                                                                     |
| `src/patches/gameDetailPatch.tsx`         | Route patch for `/library/app/:appid` — injects RomMPlaySection for custom game detail UI                                                                                                                                                                                                                                                      |
| `src/patches/metadataPatches.ts`          | Store patches for description, associations, categories, release date display                                                                                                                                                                                                                                                                  |
| `py_modules/adapters/steam_config.py`     | `SteamConfigAdapter` — VDF read/write, grid dir, shortcut icon write, Steam Input config                                                                                                                                                                                                                                                       |
| `py_modules/services/library/`            | LibraryService — builds shortcut data, drives per-unit sync apply                                                                                                                                                                                                                                                                              |
| `py_modules/domain/sgdb_artwork.py`       | `to_signed_app_id`, SGDB asset-type/endpoint maps                                                                                                                                                                                                                                                                                              |
| `bin/rom-launcher`                        | Pure `exec "$@"` wrapper invoked by Steam — runs the full launch command baked into the shortcut's launch options; owns no state, no path resolution, no emulator knowledge                                                                                                                                                                    |

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

### A per-unit heartbeat timeout must not discard the unit's delivered bindings

The per-unit apply pipeline emits `sync_apply_unit`, then waits for the frontend's `report_unit_results` ack. If the
frontend stops heartbeating for longer than the per-unit timeout (`_UNIT_HEARTBEAT_TIMEOUT_SEC`, 60s — e.g. a unit
large/slow enough that real heartbeats lag), the wait gives up. But by then the frontend has **already created the Steam
shortcuts** and will still fire its late `report_unit_results`. Dropping that ack is data loss: the bindings are never
written to `roms`, so `get_app_id_rom_id_map` doesn't know about the shortcuts, and the next sync re-creates them as
**duplicates** (an unmapped exe-detected shortcut takes the `addShortcut` branch).

So a heartbeat **timeout** is handled differently from a **user cancel** (#1052):

- **User cancel** — in-flight work is intentionally discarded. The orchestrator clears `pending_sync` and nulls
  `unit_complete_event`, so a stray late ack can't commit a cancelled unit.
- **Heartbeat timeout** — the orchestrator keeps `pending_sync`, flags `unit_abandoned`, and stashes the unit's ROMs in
  `pending_unit_roms`. The late `report_unit_results` observes the flag and drives `commit_unit_results` itself,
  persisting the delivered bindings (and metadata from the stash). Do **not** re-clear `pending_sync` on timeout — that
  re-opens the orphan/duplicate loop.

The committed binding self-heals the duplicate hazard: a bound `roms` row is mapped by `getExistingRomMShortcuts` next
sync, so `resolveShortcutAppId` takes the update branch. The orchestrator does **not** add active orphan deletion — a
Steam shortcut is the sole record of its tile (the "never delete data that exists nowhere else" invariant).
