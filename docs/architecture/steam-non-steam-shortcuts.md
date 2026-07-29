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

To set all shortcut properties reliably, wait for Steam to register the new app's **overview** before the `Set*` calls:

```typescript
const appId = await SteamClient.Apps.AddShortcut(name, exe, "", "");
// Poll appStore.GetAppOverviewByAppID(appId) (~100ms cadence) until the overview
// exists, with a 1000ms fallback; on timeout, proceed anyway.
await waitForAppOverview(appId, 1000);

SteamClient.Apps.SetShortcutName(appId, name);
SteamClient.Apps.SetShortcutExe(appId, exe);
SteamClient.Apps.SetShortcutStartDir(appId, startDir);
// An empty launch_options (uninstalled placeholder) needs no write or confirm —
// a fresh shortcut's launch options are already empty, so skip both. A non-empty
// command takes the confirmed write (setLaunchOptionsConfirmed).
if (launchOptions !== "") await setLaunchOptionsConfirmed(appId, launchOptions);
```

Steam must finish registering the new app internally before the `Set*` calls land, or they silently fail. Rather than a
fixed worst-case wait, the plugin polls `appStore` for the new overview (readiness) — the common case proceeds in ~100ms
instead of a blind 500ms, and the 1000ms ceiling keeps the old wait's safety net when the overview is slow. Skipping the
launch-options write for the (majority) uninstalled case also avoids `setLaunchOptionsConfirmed`'s
`RegisterForAppDetails` poll, which forces Steam to load and cache a fat `AppDetails` object per call.

### Exe quoting

**Do NOT pass quoted exe paths to `AddShortcut` or `SetShortcutExe`.** The API handles quoting internally. Passing
`"\"path/to/exe\""` (pre-quoted) results in double-quoting, which causes launches to fail with "file not found."

Pass the raw path:

```typescript
SteamClient.Apps.SetShortcutExe(appId, "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher");
```

### Updating existing shortcuts

Steam **assigns** a shortcut's `appId` when `AddShortcut` creates it, and that `appId` is **stable for the shortcut's
lifetime**. The plugin never computes it: it records Steam's assigned id in `roms.shortcut_app_id` and detects ownership
by the exe path (`…/bin/rom-launcher`), not by re-deriving the id. (The historical "`appId` is `CRC32(exe + appName)`"
formula does not hold on current Steam — see [App IDs and Artwork](#app-ids-and-artwork).) Two consequences follow:

- **`launchOptions` and `startDir` are appId-safe.** Changing either on an existing shortcut keeps the same `appId`, so
  the shortcut's identity, artwork, collection membership, and `roms.shortcut_app_id` binding all survive.
  `SetAppLaunchOptions` on an existing shortcut is **reliable** — confirmed on hardware in
  [#827](https://github.com/danielcopper/decky-romm-sync/issues/827) across in-session writes, a Steam restart, and
  removal-churn re-syncs. The plugin uses it directly to bake the launch command in at download-complete and to
  re-resolve paths after a RetroDECK-home migration.
- **`exe` and the display name are applied by delete + recreate.** A launch-config change that touches `exe` or the name
  is handled by removing the shortcut and re-syncing it, which yields a **new** `appId` (a fresh shortcut); a
  `launchOptions`-only change is not. This delete + recreate behavior is unchanged; the original rationale — that
  changing `exe`/name re-hashes to a different `appId` — rests on the CRC derivation above and is no longer verified.

Because `SetAppLaunchOptions` returns `void` with no success signal, the plugin **fires the set then polls**
`RegisterForAppDetails` until the read-back `strLaunchOptions` matches (`setLaunchOptionsConfirmed`). Setting `""` — the
placeholder an uninstalled ROM carries until it is downloaded — is valid and confirms against an empty read-back.

The real hazard is not the set: heavy removal-churn can corrupt Steam's in-memory shortcut state. A Steam restart clears
it. The sync engine processes removals before additions to minimise churn.

See: `src/utils/steamShortcuts.ts`

### Recovery after a server switch / re-import

Switching the RomM server URL — or re-importing on the same server — reissues `rom_id`s while the Steam shortcuts are
**not** deleted (their assigned appIds persist) and the `roms` rows survive (ADR-0007 retention) with the binding in
`roms.shortcut_app_id`, reverse-lookupable via `get_app_id_rom_id_map()`. Because Steam **assigns** the appId at
creation (the `CRC32(exe + name)` derivation is disproven — see [App IDs and Artwork](#app-ids-and-artwork)), the plugin
never re-derives an id to "find" the old shortcut. It keeps a game's shortcut alive across the `rom_id` churn through
three lanes:

- **Stable sibling-group keys → the rebind lane.** An unchanged game whose `sibling_group_key` is the same after the
  re-import (same IGDB/… identity) rides `collapse_sibling_groups`'s **rebind lane**: the group's only fetched member is
  the fresh `rom_id`, the old bound `rom_id` has vanished, so collapse emits one entry keyed to the vanished sibling —
  the frontend reuses its existing shortcut by `rom_id` — carrying `bind_rom_id` → the fresh representative. The
  shortcut, its artwork, its collection membership, and its playtime all survive; only the DB binding moves onto the new
  `rom_id`, and no duplicate is minted. _(The domain behavior is unit-pinned in `tests/domain/test_sync_diff.py`;
  on-device confirmation of the end-to-end path is still pending.)_
- **Changed / absent group keys → delete + create.** If the re-import changes or drops the `sibling_group_key` (the game
  rematches to a different metadata entry, or its identity is lost), the group no longer resolves to the old bound
  sibling, so the fresh `rom_id` takes the **new** lane: a fresh `AddShortcut` with a **new** appId, and the old
  shortcut is torn down by the stale path. Artwork and collection membership are re-established for the new shortcut.
  This is honest degradation — the identity link the rebind lane needs is gone, so the shortcut cannot be preserved.
- **Untracked orphans → adoption at create time.** A live RomM-owned shortcut (exe ends `/bin/rom-launcher`) that
  carries **no** DB binding — a crashed run's uncommitted in-flight shortcut, or a zombie left after a DB reset — is
  invisible to both the rebind lane (no `roms` row) and the stale path (nothing to unbind), so a naive create would
  leave a duplicate (`#1366`). When such a ROM reaches the create path, the frontend **adopts** the orphan instead: it
  matches the built entry's display name against a once-per-run pool of live-but-unbound RomM appIds
  (`getLiveRomMShortcutAppIds()` minus the run's already-bound appIds, resolved to names via
  `appStore.GetAppOverviewByAppID`), reuses the matched appId, and rewrites its identity + launch bake (the same `Set*`
  writes as an update). The pool is built **lazily** on the first create candidate (a pure-update run pays no scan
  cost); each orphan is adopted at most once per run; a name collision adopts the lowest appId deterministically; a
  `null` live scan (store unreadable) disables adoption for the run rather than guess. An adoption counts as "added" in
  the post-sync toast (a game came under management) but skips the `AddShortcut` a duplicate would cost. See
  `resolveShortcutAppId` in `src/utils/syncManager.ts`.

Two guards keep a re-import from wiping a freshly-bound shortcut (`#1036`):

- **One appId, one bound row.** `SqliteRomRepository.save()` unbinds any sibling row holding the appId before the
  per-`rom_id` UPSERT, and migration `003`'s partial unique index on `shortcut_app_id` enforces it (see
  [Database Design](database-design.md)). A re-import never leaves two bound rows sharing one appId.
- **Stale-removal excludes appIds bound this run.** The finalize stale pass flags bound rows whose `rom_id` wasn't
  synced this run — which includes the old `rom_id` the rebind lane just superseded.
  `domain/sync_diff.py:select_stale_removals` removes any candidate whose appId is in the run's `committed_app_ids`
  (every appId bound this run, across both the happy-path and the heartbeat-timeout late-ack commit paths), so the appId
  the run just re-bound onto the new `rom_id` is never emitted for removal. The `get_by_app_id` reverse lookup orders
  `rom_id DESC LIMIT 1` so it resolves the live (newest) binding for any pre-migration edge state.

### Retained-row availability in the version picker

ADR-0007 keeps a `roms` row after RomM stops returning that id because the row can anchor local-only saves, playtime,
and an installed ROM. Retention does not imply that RomM still offers the row as a playable version. The Game Page's
lazy `get_version_list` load therefore recomputes availability every time; there is no liveness column, migration,
cache, or persisted verdict.

The existing detail request for the bound id supplies its own answer and RomM's current direct `sibling_roms` view. The
bound id and every local id positively present in that view are live. A local group member absent from the direct view
is only a suspect because RomM sibling membership can be transitive, so each suspect is checked by an exact-id
`get_rom_once` request. These checks fan out concurrently on the worker executor and use the short timeout with no
retry, keeping them off the event loop and out of the initial Game Page render.

Only a typed `RommNotFoundError` from an exact id marks that entry `vanished`. A successful response is live; timeout,
transport, authentication, server errors, and malformed or empty data all fail open and leave it available. If the bound
detail itself 404s, `bound_vanished` is true, `server_query_failed` stays false, and every other local member is checked
individually. That entity-specific 404 is not fed into the global connection store; a genuine explicit
server-unreachable result still is. Cover and save endpoints are not liveness authorities.

Vanished rows stay visible with their active and downloaded markers, but are disabled and excluded before the existing
default-resolution kernel runs. `vanished` does not change `switchable` or
`domain.sibling_group.target_in_sibling_group`: availability and sibling membership remain separate verdicts. This lets
a shortcut still bound to a vanished id show the retained context while the user selects a live alternative. The Saves
tab likewise skips positively vanished inactive installs before checking local drift, then continues through later live
candidates.

The list verdict is advisory UI state, not authority for a later write. Immediately before `switch_version` moves the
binding onto an already-local target, it checks that exact target id again through the same three-second, single-attempt
`get_rom_once` path. The request runs on the worker executor outside the write UoW and after the save-stranding guard
permits the attempt. Consequently an initial unsynced-save warning makes no target request, while both `Sync now` and
`Switch anyway` retries are protected; `allow_stranded` never bypasses liveness. A typed target 404 returns
`version_vanished` without changing the binding or any recorded launch state. Every other optional-probe outcome fails
open, so a local switch remains fast when RomM is uncertain or offline. The active-target no-op does not probe because
it moves no binding.

A server-only target already requires its full RomM detail for membership validation and row construction. That fetch
keeps its normal retry policy, doubles as the liveness verdict, and receives no second probe. Its typed 404 produces the
same `version_vanished` refusal; other failures retain their ordinary classified reason. Network I/O remains outside the
short write UoW, whose fresh membership and bound-elsewhere checks still decide SQLite races. This leaves an unavoidable
cross-system interval after a successful response: the liveness check reduces stale-list risk but is not a transaction
with RomM.

## Explicit cleanup of vanished versions

Automatic sync remains unbind/retain-only. Deleting retained local state is a separate confirmed workflow under **Danger
Zone → Clean Up Removed RomM Games**, also reachable by activating a synced vanished version's own picker row (which
carries a trash affordance, the menu row being the focusable unit) or as a focused button for a synced singleton
vanished binding. Candidate discovery is not deletion authority: the backend freshly probes each exact RomM id, and only
typed 404s can proceed.

For a vanished bound version with a live sibling, the default-on repoint action reuses `switch_version` independently of
the row-removal option, then the frontend confirm-writes the returned exact launch options. Cover/cache publication and
the `version_switched` event are deferred until terminal prune completion. Before emitting a terminal result that needs
repoint publication, the backend acquires a continuation lease while the old run is still active; the frontend registers
that token immediately and holds it across both release acknowledgement and the final artwork write. Another prune
therefore cannot enter between the old claim and publication. Publication uses the same path as VersionPicker. Repoint
changes neither shortcut name nor exe and never calls `AddShortcut`, so the assigned appId, collections, and Steam
playtime remain attached. Unsynced-save stranding can be overridden only after enabled recovery has sealed.

For a fully vanished bound game, whole-game cleanup is its own confirmation option, default-on and paired with the
default-on recovery bundle that keeps the shortcut rebuildable. With recovery enabled, the root frontend handler
captures complete shortcut details, available Steam playtime fields, and every collection id/name or fails closed if
that JSON cannot fit the wire bound. The backend resolves the active account from Steam's login identity once, stores
that identity in the recovery handle, and adds only that user's grid artwork, both per-app Steam Input roots, and
relevant controller setting. Cleanup must use those exact captured roots even if the active account changes later.

Every action event is deduplicated and serialized. Before any Steam mutation, the frontend claims its token from the
backend, re-reads the live shortcut, and requires the appId to exist with an exe ending in `/bin/rom-launcher`. Only
then does shortcut removal capture a fresh complete snapshot, compare it with the sealed snapshot, call
`RemoveShortcut(appId)` once, and poll the live store until absence. Identical claim retries are idempotent. Completion
reporting uses bounded retries of the same payload without repeating the Steam operation. If every completion report is
lost, the claimed lease expires as an ambiguous partial and retains source data. A `RemoveShortcut` call followed by an
unreadable store or settle timeout is also explicitly attempted-but-unconfirmed, never reported as an unchanged failure;
a later run can confirm the appId is already absent and reconcile the binding without calling removal again. An
unreadable/foreign store before mutation, stale claim, or unclaimed timeout is failure, and no row/source finalization
follows an uncommitted action.

The same reciprocal exclusion covers ordinary frontend Steam continuations outside an action event. SGDB fetch results
carry leases through hero/logo/grid/icon writes; `sync_complete` keeps one shared lease until launch-option, collection,
playtime, and overview-metadata branches all settle; and bulk shortcut removal clears its collections before
acknowledging and releasing the removal lease. Leases live in one frontend registry, renew only for a bounded active
continuation, and receive a cooperative cancellation signal before their component owner or plugin dismount releases
them. Owner/plugin mount generations are captured before backend waits, so a token arriving after teardown is released
without admitting old continuation work even if a new owner has since mounted. Each non-empty `sync_stale` frame owns a
lease through its paced tail; successful `sync_complete` processing overlaps that lease while joining the same tail. A
backend emit failure rolls back a token the frontend never received.

Recovery records the Steam-assigned appId and playtime, but there is no automatic restore and Steam cannot currently
reattach those values to a newly created shortcut.

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

The pass also re-runs on **`sync_complete`**. A sync adds or re-keys ROMs whose metadata the init-time pass never saw,
so `onSyncComplete` re-fetches the full paged metadata cache + appId map (`fetchMetadataCachePages`, shared with init),
re-registers via `registerMetadataPatches` with the fresh data, and re-applies — mirroring the playtime re-apply beside
it. It runs on every `sync_complete`, cancelled runs included (a partial run's committed units still carry fresh
metadata, and the pass is idempotent), in its own detached block with its own error handling so a re-fetch failure never
touches the toast, collections, or playtime paths (#1207). The backend commits metadata per unit during the sync (before
the terminal emit), so this re-fetch always sees the new ROMs.

See: `src/patches/metadataPatches.ts`, `src/utils/metadataCache.ts` (paged fetch), `onSyncComplete` in `src/index.tsx`

## VDF Format Notes

Shortcut creation and every field update go through the frontend `SteamClient.Apps.AddShortcut()` / `Set*` API —
`AddShortcut` returns the real `appId` directly, so the plugin never computes app IDs itself and never edits
`shortcuts.vdf` while Steam is running (Steam holds the file in memory and rewrites it from memory, silently clobbering
external writes — see [shortcuts.vdf is memory-authoritative](#shortcutsvdf-is-memory-authoritative)). The backend
`SteamConfigAdapter` (`adapters/steam_config.py`) still lays down artwork **files** in the grid directory, including the
icon PNG; its `shortcuts.vdf` read/write helpers remain in the adapter but are no longer on any live path after the icon
write moved to `SteamClient.Apps.SetShortcutIcon`.

### shortcuts.vdf structure

Steam stores non-Steam shortcuts in a binary VDF file at:

```text
~/.local/share/Steam/userdata/<user_id>/config/shortcuts.vdf
```

Each entry has these key fields:

| VDF Field       | Format       | Notes                                                                                                                                                                                                                                                 |
| --------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AppName`       | string       | Display name                                                                                                                                                                                                                                          |
| `Exe`           | string       | Executable path. `AddShortcut`-created entries store it **unquoted** (on-device inspection) — the API handles any quoting internally                                                                                                                  |
| `StartDir`      | string       | Start directory. Stored **unquoted** for `AddShortcut`-created entries                                                                                                                                                                                |
| `LaunchOptions` | string       | The full launch command the `bin/rom-launcher` exec wrapper runs, e.g. `flatpak run net.retrodeck.retrodeck "/path/to/game.iso"` — or `""` (placeholder) for an uninstalled ROM. No `romm:<id>` marker; ownership is detected by the exe path instead |
| `appid`         | signed int32 | Assigned by Steam when `AddShortcut` runs; stored as the signed int32 form (`to_signed_app_id`)                                                                                                                                                       |
| `icon`          | string       | Icon path or hash                                                                                                                                                                                                                                     |
| `tags`          | object       | Steam collection tags. The plugin manages collections via `collectionStore` (machine-scoped names like `RomM: N64 (steamdeck)`), not by writing this VDF field.                                                                                       |

### shortcuts.vdf is memory-authoritative

While Steam is running, `shortcuts.vdf` is authoritative **in Steam's memory**: Steam rewrites the file from memory
mid-session and on exit, so any external write to it while Steam runs is silently clobbered. The plugin therefore
creates and mutates shortcuts only through the `SteamClient` API (`AddShortcut` / `Set*` / `SetShortcutIcon`), never by
editing `shortcuts.vdf` directly. Pass raw, **unquoted** paths through those APIs — the API adds any quoting internally,
and on-device inspection confirms `AddShortcut`-created entries are stored unquoted; pre-quoting double-quotes the path
and breaks launches (see [Exe quoting](#exe-quoting)).

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

### Collection naming mode — `merge` vs `by_label` (#1539)

The `romm_collection_app_ids` wire payload is `name → appIds` **only** — kind/virtual_type are collapsed away before the
frontend sees them, and the frontend simply wraps each key as `RomM: [<key>] (<hostname>)`. So the Steam-collection name
is decided **entirely by the reporter's dict key**, computed backend-side in
`SyncReporter._resolve_collection_memberships` (`services/library/reporter.py`) from the `collection_naming_mode`
setting:

- **`merge`** (default) — the key is the bare collection display name. Same-named RomM collections of any kind union
  into one `RomM: [<name>]` Steam collection (RomM permits same-named collections across kinds/users, #1503).
- **`by_label`** (opt-in) — the key is `"<name> (<FineLabel>)"`, where the fine label comes from the pure
  `domain/collection_label.py` kernel: `Standard` / `Smart` for those kinds, and the virtual sub-type label (`Franchise`
  / `IGDB Collection`) for `kind == "virtual"`. So a franchise and an IGDB collection that share a name become
  `RomM: [<name> (Franchise)]` and `RomM: [<name> (IGDB Collection)]` — separate Steam collections. Two collections that
  share **both** name and label still union.

The label strings **must** match the frontend collection-type vocabulary (`SUB_TAB_LABELS` / `VIRTUAL_TYPE_LABELS` in
`src/components/LibraryPage.tsx`) so the type a user sees on the Collections page is the type baked into the Steam name.
The reporter needs the kind/virtual_type at its union key, so `WorkUnit.virtual_type` and `CollectionMembership.kind` +
`CollectionMembership.virtual_type` thread that identity through the fetcher → orchestrator → reporter.

**Label-format constraint:** the reconcile parses the collection name with `/^RomM: \[([^\]]+)\]/` (`src/index.tsx`), so
a label must contain **no** `]` character — it sits inside the single existing bracket pair. Parens (`(Franchise)`) are
safe; a bracket would truncate the parsed name and orphan the collection. Every produced label is bracket-free (asserted
in `tests/domain/test_collection_label.py`).

**No Force Full Sync on a mode flip.** Because the create-name and the reconcile's `activeNames` both derive from the
same `romm_collection_app_ids` keys, flipping the mode is applied by the ordinary **complete-set reconcile** on the next
normal sync: the reporter re-emits the complete set of enabled collections under the new keys, `onSyncComplete` creates
the new-named collections and deletes any old-named collection absent from the new complete set. No skip-state
invalidation is involved (same mechanism owner-scope reshaping uses).

**Name identity is case-insensitive (#1569).** Steam collapses collection names by a **case-insensitive** identity — two
collections whose display names differ only in case (`RomM: [7 up]` vs `RomM: [7 Up]`) are the same Steam collection, so
creating the second silently overwrites the first and loses its games. To match, collection **and** platform name
identity is treated case-insensitively **everywhere** the plugin compares names: the reporter groups both
`romm_collection_app_ids` and `platform_app_ids` by a case-folded key (`str.casefold()`), keeping the first-seen
original casing for display (which exact casing wins is irrelevant — Steam uppercases collection names anyway); the
frontend create/find (`createOrUpdateCollections` / `createOrUpdateRomMCollections`), the cleanup matchers
(`clearPlatformCollection` / `clearAllRomMCollections`), and the `onSyncComplete` stale-delete comparisons all match by
`toLowerCase()`. This is always safe precisely because Steam's identity is case-insensitive: two collections differing
only by case can never coexist, so there is never an ambiguous match to disambiguate. The DB is unaffected —
`collection_sync_state` is keyed by `(collection_id, collection_kind)`, never by name — so there is no migration.

## App IDs and Artwork

`SteamClient.Apps.AddShortcut()` returns the real `appId`, so the plugin does **not** compute shortcut app IDs itself —
there is no app-ID generator in the codebase. Steam **assigns** the `appId` at creation and it is stable for the
shortcut's lifetime, which is why mutating `launchOptions` or `startDir` keeps the same `appId` (see
[Updating existing shortcuts](#updating-existing-shortcuts)) while delete + recreate yields a new one.

> **Errata (2026-07): the appId is not `CRC32(exe + appName)`.** Earlier docs described the `appId` as
> `CRC32(exe + appName)`. On-device inspection of 68 live plugin-created shortcuts matched **none** against any CRC32
> candidate (exe/name variants, quoted/unquoted, with/without a trailing NUL, top bit set), and the live appids are
> uniformly spread across `[0x80000000, 0xFFFFFFFF]` — consistent with random assignment at creation (and with the
> community observation that delete + re-add yields a different appid). The load-bearing facts are unchanged: the appId
> is stable for the shortcut's lifetime (so `launchOptions` / `startDir` edits are appId-safe), delete + recreate yields
> a new appId, and the plugin's identity model never computes appIds — it records Steam's assigned id in
> `roms.shortcut_app_id` and detects ownership by the exe path. Only the derivation _mechanism_ was wrong.

The frontend stores the returned `appId` and the backend persists it as `shortcut_app_id` on the ROM's `roms` row (the
synced-ROM registry; reverse-lookupable by `shortcut_app_id`). The frontend resolves rom_id ↔ appId through the
backend's `get_app_id_rom_id_map()` callable, which reads that binding.

The signed-int32 helper `to_signed_app_id(app_id)` remains in `py_modules/domain/sgdb_artwork.py` (alongside the SGDB
endpoint/asset-type maps) for the `shortcuts.vdf` record format, but no longer has a production caller now that the icon
write goes through `SteamClient` rather than editing the VDF.

### Artwork file naming

Grid artwork is stored at `userdata/<user_id>/config/grid/`, keyed by the shortcut's real `appId`:

| Suffix             | Artwork Type           |
| ------------------ | ---------------------- |
| `<appId>p.png`     | Portrait grid (cover)  |
| `<appId>_hero.png` | Hero banner            |
| `<appId>_logo.png` | Logo overlay           |
| `<appId>.png`      | Wide grid / horizontal |
| `<appId>_icon.png` | Icon                   |

Each form also occurs with a `.jpg` / `.jpeg` extension. On shortcut removal the plugin deletes the **full** suffix ×
extension set for the removed appId (`ArtworkService.remove_artwork_files`), so companion art (hero/logo/icon/wide)
never outlives its shortcut. Files a removal missed historically are reclaimed by the Danger Zone's **Remove Orphaned
Grid Images** cleanup (`cleanup_orphaned_grid_images`): candidates are only grid-image-named files whose appId sits in
the non-Steam-shortcut range (`[0x80000000, 0xFFFFFFFF]` — see the errata above; store-game custom art is out of range
and never touched) and whose appId belongs to no live shortcut in the frontend's full scan; if any bound
`roms.shortcut_app_id` is missing from that scan the cleanup refuses and deletes nothing.

`ArtworkService` (cover staging/finalisation, renaming the staged cover to `{app_id}p.png`) and `SteamGridService` (SGDB
hero/logo/grid/icon) own the artwork flow. The icon is a two-step write: `SteamGridService.save_shortcut_icon` writes
the icon PNG into the grid dir via `SteamConfigAdapter.write_shortcut_icon` and returns its `icon_path`; the frontend
then points the live shortcut at it with `SteamClient.Apps.SetShortcutIcon(appId, icon_path)`. The backend no longer
edits the `shortcuts.vdf` `icon` field — Steam is memory-authoritative and clobbered that write, so pointing the
shortcut must go through `SteamClient` (see
[shortcuts.vdf is memory-authoritative](#shortcutsvdf-is-memory-authoritative)).

Covers are applied per created shortcut through Steam's own artwork API during the apply, so tiles show their real cover
in-session with no client restart. Right after a newly created shortcut resolves its `appId` in the per-item apply loop
(`applyCoverArtwork` in `syncManager.ts`), the frontend fetches the cover bytes for that ROM
(`get_artwork_base64(rom_id)`) and hands them to `SteamClient.Apps.SetCustomArtworkForApp(appId, base64, "png", 0)`.
Steam decodes the image, owns the tile, and writes the file itself as `{app_id}p.png` in the grid dir — the same path
the backend also writes — so the cover appears as the shortcut is created and Steam refreshes the tile in-session.

The key properties:

- **Creates only.** A cover is applied only when the item is a fresh create. An updated or rebound shortcut keeps its
  existing grid file (in-session cover refresh on a version/metadata change is tracked separately, `#1386`).
- **One cover per item, under the session-budget gate.** The cover is fetched and applied _inside_ the existing 50
  ms-paced per-item loop — never prefetched or batched. Decoding many covers resident at once is exactly the CEF heap
  overflow (`#797`) that crashed `SharedJSContext` on large libraries; the session-budget gate (see
  [ADR-0024](../adr/0024-session-budget-rss-gate.md)) prices each create at its permanent cost plus the cover's
  transient peak and pauses the run before the renderer nears its heap cliff.
- **Fail-soft.** A cover that can't be fetched (`base64: null`) or applied (a throwing `SetCustomArtworkForApp`) is
  logged and never fails the shortcut — the shortcut is already created, and the backend's commit-time grid write is the
  durability net.

The backend also writes each `{app_id}p.png` grid file at commit (`SyncReporter._finalize_cover_path` →
`ArtworkService.finalize_cover_path`). That copy costs no renderer heap and is the durability net: it lands the grid
file even if a per-item API call failed, so a residual gray tile resolves the next time the game's page is opened or on
the next client restart.

## Pre-launch launch-options confirmation

Both launch funnels (the game-detail Play button and Steam's direct-launch watcher) re-fetch the selected ROM's resolved
command and confirm-write it immediately before `RunGame`. Ordinary fetch or Steam-write failures remain best-effort and
the launch proceeds. A three-second callable timeout is different: the already-cancelled launch remains blocked, while
the unresolved callable stays observed so a lease token returned later is released without a Steam write. Each launch
captures its plugin/component generation before gate and modal waits; teardown makes that admission stale, and even an
immediate remount cannot let the old chain write launch options or invoke `RunGame` under the new generation.

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

Calling `Set*` methods too quickly after `AddShortcut` (before the new app's overview is registered) results in the
properties not being saved. The shortcut appears in the library but with wrong or missing exe/startDir/launchOptions.
Launches fail or open the wrong thing. The plugin gates the `Set*` calls on an overview-readiness poll
(`waitForAppOverview`, 1000ms fallback) rather than a fixed delay.

### Removal-churn can corrupt shortcut state

`SetAppLaunchOptions` on an existing shortcut is reliable (validated in
[#827](https://github.com/danielcopper/decky-romm-sync/issues/827); see
[Updating existing shortcuts](#updating-existing-shortcuts)) — the historical "property updates may not persist" warning
has been narrowed. The remaining hazard is **removal-churn**: adding and removing many shortcuts in one pass can corrupt
Steam's in-memory shortcut state. A Steam restart clears it. Two things keep churn down. The sync engine processes
removals before additions, and every launch-options write uses the fire-then-poll `setLaunchOptionsConfirmed` so a
silently dropped write is observable rather than assumed. And **mass removals are awaited and chunk-paced** through the
shared `removeShortcutsPaced` helper (`src/utils/shortcutRemoval.ts`, over `pacedForEach`,
[#977](https://github.com/danielcopper/decky-romm-sync/issues/977)): every bulk removal path — the DangerZone actions
(per-platform, Remove-All-RomM including the live-orphan sweep, and the Remove-Non-Steam bulk action) **and** the
sync-run stale-shortcut cleanup (`sync_stale`, fired at run finalize) — awaits each `removeShortcut` in sequence and
yields a 50ms breather every 25 removals, so the CEF renderer never blocks and thousands of removals can't stack as
fire-and-forget promises. (The `sync_stale` handler records its "removed" delta for the terminal toast up front, before
the first breather, so the paced removal can't leave the count partial when `sync_complete` interleaves.) Only
`exe`/name changes still go through delete + recreate — a fresh `AddShortcut`, which yields a new `appId`.

### AddShortcut / RemoveShortcut timing between shortcuts

Bulk shortcut loops corrupt Steam's internal store if driven too fast — added shortcuts may silently fail to register,
and removals churn the in-memory state (above). Both cadences live in one place: the shared paced loop `pacedForEach` in
`src/utils/pacedOps.ts`, which iterates awaiting each item and yields a breather between chunks (no trailing delay). The
two callers differ only in chunk size:

- **Add** (`syncManager.ts` — `processUnitShortcuts`, `processCoverRefreshes`) paces **one item at a time**: a 50ms
  breather after every `addShortcut()` / cover apply, plus the per-unit heartbeat + cancel hooks.
- **Remove** (`removeShortcutsPaced` in `src/utils/shortcutRemoval.ts`, shared by the DangerZone actions and the
  `sync_stale` cleanup) paces in **25-item chunks with a 50ms breather** between them. A removal is a single cheap call,
  so chunked yielding keeps a 5000-game teardown at ~seconds of overhead instead of the ~4 minutes strict 50ms/item
  would cost, while still letting the renderer breathe. DangerZone and `sync_stale` removals hold renewable prune
  conflict leases for the complete paced loop even though the backend does not await the frontend's stale removal.

### The apply is chunked; a heartbeat timeout must not discard a chunk's delivered bindings

A unit's emitted shortcuts are split into fixed-size chunks (200,
[ADR-0023](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0023-chunked-per-unit-apply.md)); the
pipeline emits one `sync_apply_unit` per chunk (carrying `chunk_index` / `chunk_count` / `chunk_offset` / `unit_total`,
`shortcuts` = the chunk slice), then waits for the frontend's `report_unit_results` ack — echoing the `chunk_index` back
— and commits that chunk's `roms` rows durably before emitting the next. A mid-unit crash, cancel, or timeout forfeits
only the in-flight chunk; every chunk committed before it stays committed. See
[Backend Architecture — per-unit apply](backend-architecture.md) for the full loop.

If the frontend stops heartbeating for longer than the per-chunk timeout (`_UNIT_HEARTBEAT_TIMEOUT_SEC`, 60s — e.g. a
chunk slow enough that real heartbeats lag), the wait gives up. But by then the frontend has **already created that
chunk's Steam shortcuts** and will still fire its late `report_unit_results`. Dropping that ack is data loss: the
bindings are never written to `roms`, so `get_app_id_rom_id_map` doesn't know about the shortcuts, and the next sync
re-creates them as **duplicates** (an unmapped exe-detected shortcut takes the `addShortcut` branch).

So a heartbeat **timeout** is handled differently from a **user cancel** (#1052 / #1367):

- **User cancel** — the in-flight chunk is intentionally discarded. The orchestrator clears the staging and nulls
  `unit_complete_event`, so a stray late ack can't commit a cancelled chunk.
- **Heartbeat timeout** — the orchestrator moves the abandoned chunk into an `abandoned_chunk` stash on
  `LibrarySyncStateBox` (`stash_abandoned_chunk`): its run/unit/chunk identity plus **this chunk's** ROMs (only the
  abandoned chunk), while keeping the whole-unit staging live for the commit to read and clearing the dispatch identity.
  The stash lives **outside** the run-lifecycle state and deliberately survives the run's teardown (`finish_run` nulls
  `current_sync_id`), because in production the late `report_unit_results` arrives **after** the run has wound down —
  the window an earlier design missed, where the active-unit ack check could no longer match and the recovery was
  unreachable (#1367). The late ack matches the stash **by identity** (`take_abandoned_chunk`) and drives
  `commit_unit_results` itself over the stashed rows (binding + metadata), never stamping a timed-out platform complete.
  Bounded lifetime: the next run's `try_begin_run` clears an unacked stash.

The committed binding self-heals the duplicate hazard: a bound `roms` row is mapped by `getExistingRomMShortcuts` next
sync, so `resolveShortcutAppId` takes the update branch. The orchestrator does **not** add active orphan deletion — a
Steam shortcut is the sole record of its tile (the "never delete data that exists nowhere else" invariant).
