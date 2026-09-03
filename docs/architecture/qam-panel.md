# QAM panel

The Quick Access Menu panel is the plugin's own surface inside Steam's QAM: the Decky tab, then the plugin's entry. It
opens on **Main** and reaches every other page from there. Steam renders the QAM 348 px wide; a page of this plugin can
widen it to 854 px — the width Steam's own Friends tab uses — for as long as that page is mounted. This page owns the
panel's structure: which pages exist, which are wide, how a page is navigated and laid out, and where each action has
its home. The game detail page is a Steam route, not part of the panel, and is out of scope here; the state it shares
across its surfaces is the **Game-detail store** (CONTEXT.md).

The structure below is the target decided in [#1809](https://github.com/danielcopper/romm-tender/issues/1809) and
rebuilt one page at a time under [#1808](https://github.com/danielcopper/romm-tender/issues/1808). Where today's panel
differs, the difference is stated; the PR that lands a page updates its row in the page table. The vocabulary — **QAM
page**, **Main**, **wide page**, **list and detail**, **notice**, **home** — is defined in CONTEXT.md and used here
without restating it. The width mechanism's decision record is
[ADR-0029](../adr/0029-wide-qam-pages-drive-steams-friends-expansion.md).

## Where the code lives

| Module                                                        | Responsibility                                                                                                       |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `src/index.tsx` (`QAMPanel`)                                  | The router: one `Page` value, one mounted page, a module-level `currentPage` that survives a QAM remount             |
| `src/types/navigation.ts`                                     | The `Page` union — every page the router can land on                                                                 |
| `src/components/MainPage.tsx`                                 | Main                                                                                                                 |
| `src/components/LibraryPage.tsx`                              | Library — Platforms and Collections                                                                                  |
| `src/components/SettingsPage.tsx`, `src/components/settings/` | Settings and its sections                                                                                            |
| `src/components/DangerZone.tsx`, `RemovedGamesCleanup.tsx`    | Data Management                                                                                                      |
| `src/components/DownloadQueue.tsx`                            | Downloads                                                                                                            |
| `src/components/SystemPage.tsx`                               | System — retires into Library › Platforms                                                                            |
| `src/utils/deckyUiInternals.ts`                               | Honest typing for `@decky/ui` values that come from a webpack probe; the wide frame's class names and `Tabs` go here |
| `src/utils/qamExpansion.ts`                                   | The panel's width: the expand and hide messages, the injected `max-width` rule, and the four paths that clear both   |
| `src/components/qam/`                                         | The wide-page frame: `WidePage` (Back row, title, tabs, measured height) and `ListDetail`                            |
| `src/utils/` module stores                                    | State that must outlive a page: sync progress, pending preview, downloads, prune, notices                            |

## Two widths

Every page is either narrow (348 px, 300 px of content) or wide (854 px, 806 px of content). The width belongs to the
page: a page is wide or it is not, and no view inside a page changes it. Main and Downloads are narrow; Sync, Library,
Settings and Data Management are wide.

How a page gets wide, measured on the device (Big Picture, CEF Chrome 126) rather than read from documentation:

- Steam's main window holds the QAM in `.ViewPlaceholder`, always 854 px wide, anchored right and pushed off-screen by
  `transform: translateX(506px)`, so 348 px stay visible. Steam's `Expanded` class sets `translateX(0)`. The class
  follows one MobX observable on the FriendsUI store, which listens for `message` events on the SharedJSContext window —
  the window plugin code runs in. A wide page posts `{ message: "QamFriendsExpanded" }` to `window` on mount and
  `{ message: "QamFriendsHidden" }` when it lets go. The target origin is always `window.origin`: `postMessage` throws
  on a mismatch, and one caller is `onDismount`, where a throw abandons the rest of the plugin's teardown.
- Every tab's content panel carries `max-width: 300px`; only Steam's Friends panel lifts it. A wide page injects one
  stylesheet whose `:has()` rule lifts the cap for a marker class on the plugin's own subtree. Class names come from
  `quickAccessMenuClasses`, which can be `undefined`; `[id^="quickaccess_content_"]` is the fallback selector. Decky
  registers one QAM tab (`QuickAccessTab.Decky = 999`), so the plugin's panel is `#quickaccess_content_999`.
- Result: the visible panel goes from 348 px to 854 px and the tab panel from 300 px to 806 px (854 minus the 48 px tab
  rail). The QAM browser view itself is 855 px wide in both states, so only the placeholder's geometry, read through
  `findSP()`, proves an expansion.

The flag is Steam's and global, so the page that set it clears it: on unmount (navigation away, plugin closed), when the
Decky tab stops being the active QAM tab (the `ActiveTab` class on the panel's parent — a tab switch is a class change,
not an unmount), when the QAM closes (`useQuickAccessVisible`), and from `onDismount`.

Steam's tabbed page fills its parent instead of growing, and nothing in the QAM chain provides a height. A wide page
therefore measures the viewport left below its header and takes that as its height; its regions scroll inside it. A
`min-height` is not enough — it clips.

## Pages

| Page            | Width | Holds                                                                                                         | Today                                                                                                                     |
| --------------- | ----- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Main            | 348   | notices, status, the Sync button, the download summary, the menu                                              | also holds the preview card, Skip preview, Force Full Sync, the session-budget card, and a System menu entry              |
| Sync            | 854   | preview as a table, the import choice, Skip preview, Force Full Sync, Steam memory, session budget, last runs | does not exist; its controls sit on Main, and the run list and the per-platform breakdown exist nowhere yet               |
| Library         | 854   | Platforms as list and detail (sync, core, BIOS files, removal); Collections as filter and list                | narrow; platform rows carry only the sync toggle, core and BIOS are on System, per-platform removal is on Data Management |
| Settings        | 854   | five sections, list and detail                                                                                | narrow; eight sections stacked                                                                                            |
| Data Management | 854   | five library-wide operations, list and detail                                                                 | narrow; also holds per-platform actions and opens the cleanup in a modal                                                  |
| Downloads       | 348   | the queue with its controls                                                                                   | unchanged                                                                                                                 |
| System          | —     | retires: its core picker and BIOS files move into Library › Platforms                                         | a narrow page with one titled section per synced platform                                                                 |

`Page` becomes `"main" | "sync" | "library" | "settings" | "data" | "downloads"`.

Main's menu opens Library, Settings and Data Management. The Sync page opens from the Sync button and from the **Last
sync** status row; Downloads opens from **View All** in the download summary, which is shown only while the queue is not
empty. Every page but Main opens with a **Back** row, which returns to Main. After a navigation the router scrolls to
the top and places gamepad focus on the page's first button, as it does today. The module-level `currentPage` survives a
QAM remount, so reopening the QAM lands on the page that was open, and a wide page re-expands on mount.

## Building blocks

### Tabs

Steam's tabbed page, switched with L1/R1, for two to four peer views of one page. Wide pages only: at 300 px the bumper
glyphs overlap the labels, which is why the Library page's tab bar is hand-rolled today. Only Library has tabs.

### List and detail

The list takes about a third of the width (264 px in the prototype), the detail the rest. Focus selects: moving through
the list changes the detail at once, as Steam's own settings do. A list row may carry a toggle; A operates it, never the
selection. Both regions scroll independently inside the page's measured height.

A list that is grouped or sorted by state computes its order when the page mounts and keeps it while the page is open,
so toggling a row does not move it out from under the focus. The next mount shows the new order.

### Tables

Anything with more than two facts per row is a table with a header row: BIOS files (Wanted, On disk, Contents), the
preview (a row per platform; New, Updated, Removed), registered devices, cleanup candidates, collections. Today those
facts are folded into a field's label and description, which is why #1803's third axis has no slot on the System page's
rows.

### Destructive actions

Last in their group, red, behind the confirmation they carry today — two-tap or modal. Nothing here changes the
backup-or-confirm rule in the invariant register.

### Notices and homes

A notice on Main names a condition and jumps to its home; the action exists only there. A condition with no home in the
plugin stays a card without a jump, with Dismiss where the condition has a sensible end.

| Condition                                   | On Main                             | Home                                                  |
| ------------------------------------------- | ----------------------------------- | ----------------------------------------------------- |
| Settings were reset                         | text, backup path, Dismiss          | none — the card is the whole of it                    |
| Cross-device playtime needs a fresh sign-in | text, **Open Connections**, Dismiss | Settings › Connections, where the accounts are        |
| RetroDECK paths missing or unreadable       | warning card, no action             | none — the fix is outside the plugin                  |
| RetroArch `input_driver` is wrong           | text, **Open Controller**           | Settings › Controller, which holds the Fix button     |
| Save-file sorting changed                   | text, **Open Save Sync**            | Settings › Save Sync, which holds Migrate and Dismiss |
| Sync paused on the session budget           | text, **Open Sync**                 | Sync, which holds Restart Steam now and Resume        |

Today the `input_driver` fix has a button on Main and another in Settings, the save-sort card exists on both pages, the
session-budget card with **Restart Steam now** sits on Main, and the playtime notice has Dismiss but no jump. The two
full-page states — a version error and a pending RetroDECK migration — are not notices; they replace the page and stay
as they are.

## Main

Narrow, in this order: notices; the **Status** section, titled so the notices above it read as a separate block, which
is what #1442 asks for by another route — Connection, Last sync, Library, Steam memory; the Sync button; the download
summary (up to two rows, an overflow count, a completed count, View All); the menu — Library, Settings, Data Management.

**Last sync** is a row that opens the Sync page. The Sync button is one button with four states:

| State                                                                             | Label                    | Press                                                                              |
| --------------------------------------------------------------------------------- | ------------------------ | ---------------------------------------------------------------------------------- |
| no preview pending                                                                | Sync Library             | starts a preview and opens the Sync page, which shows it when it arrives           |
| a preview is pending and not expired                                              | Review changes · _N_ new | opens the Sync page with that preview; nothing is recomputed                       |
| an incomplete run can be resumed — cancelled, interrupted or paused (`canResume`) | Resume Sync              | resumes the run, as today; the Sync page offers the same next to Restart Steam now |
| a run is in flight                                                                | progress and Cancel Sync | as today                                                                           |

An expired preview counts as none; the backend drops one past its 30-minute TTL (`PREVIEW_MAX_AGE_SECONDS`). **Main
never discards a preview.** A preview ends only on the Sync page: Apply, Cancel, or Refresh (which replaces it with a
fresh one). Today a second press of Sync Library discards the pending preview on both sides; that path goes away, and
the invariant register's pending-preview entry names the Sync page's three paths in its place once the page lands. Its
fourth path — a cancel that lands just after a preview was staged and is discharged server-side alone — is unchanged.
With **Skip preview** on, the button starts the run directly and Main shows progress as today. The last run's one-line
result stays on Main for a moment after a run, as today; the run itself is on the Sync page's list.

## Sync

Wide. Left, the preview: a table with one row per platform that changes and one for collections, columns New, Updated,
Removed, a total row, the estimated duration, the hints about progress being saved and long runs. Under it the import
choice (#1364, later; the page leaves the space) and Apply / Cancel. Right: Skip preview as a persisted setting (today
it is local state and off again on the next mount), Force Full Sync with its explanation, Steam memory now and the last
run's delta, the session-budget card with **Restart Steam now** and Resume, and the last runs.

What the backend has and lacks for it: the preview answer carries library-wide totals (`SyncPreviewSummary`: new,
changed, unchanged and removed counts, the platform and collection counts, and more), the names of new and changed
games, and the added and removed collection names (`collection_diff`), not a per-platform breakdown — that breakdown is
the Sync page's backend half. The run list reads the `sync_runs` history (`SyncRun`: started, finished, status, planned
counts, completed platforms and collections), which exists and needs a read callable. Skip preview becomes a user-intent
setting in `settings.json`, written by its owner (`adapters/persistence.py`). Everything else the page shows is what
Main shows today, moved.

## Library

Wide, two tabs.

**Platforms** is list and detail. The list holds every platform RomM reports with at least one ROM — what
`get_platforms` returns today; a platform with nothing to sync is not listed — in two groups, **Synced** (the toggle is
on) above **Available**, each alphabetical, with the sync toggle in the row and a dot for the BIOS state. The dot uses
the shared mapping every platform-level BIOS dot renders through (`src/utils/biosColor.ts`: green complete, amber
partial, red missing, grey for a missing level; the per-file rows on the System page and the game page hard-code the
same four colours today). Showing no dot for a platform without a BIOS entry is the list's own rule on top of the
helper, which would answer grey. Enable all and Disable all sit above the groups. The order freezes while the page is
open. The detail shows, for the focused platform:

- **Sync** — the toggle, ROMs on RomM, shortcuts in Steam. Always.
- **Emulator core** — the current core with the same context menu the System page opens today, and the save-
  compatibility caveat. Only when the platform has synced games (`has_games` in the firmware status); otherwise one
  sentence says to sync the platform first. The frontend half of #1016 lands here: today a failed switch is silent —
  `handleSystemCoreChange` does nothing on `success: false` and the label keeps the old core — and the detail shows it
  instead.
- **BIOS files** — the summary (required, or files), then a table: Wanted, On disk, Contents (#1803's columns; the third
  is the new one), and a **Download** button on every missing row (#164). Below it Download required, Download all,
  Delete BIOS. Same condition as the core.
- **Remove** — Remove _N_ shortcuts and Delete save files, the actions the Data Management platform modal offers today,
  without Delete BIOS (it is one group up). Only when the platform has shortcuts or save files. Red, last, confirmed.

Three list-shaped platform reads exist today: `get_platforms` (RomM's platforms with ROMs, for the toggles),
`get_firmware_status` (core and BIOS files for the platforms that have firmware on the server, each tagged `has_games`;
the synced filter is the System page's own) and `get_registry_platforms` (shortcut counts, for the removal actions). The
Library page needs them joined per platform, and a platform with games but no firmware entry gets no core label from the
firmware read, so its core needs a read of its own; whether the join is a new read or done in the frontend is the
Library issue's call.

**Collections** has no per-entry detail, so it is one wide list: the favorites toggle and the Mine / All owner scope on
top, the kind filter (Standard, Smart, Virtual — with the Franchise / IGDB Collection split inside Virtual), the fuzzy
search with its 50-row render cap, Enable all / Disable all with today's semantics, and rows with name, kind, a **mine**
marker (the payload carries `is_own`, not an owner name), ROM count and the toggle. The collections tab's permanent
brick on one transient failure (#1020) is fixed as part of the rewrite.

## Settings

Wide, list and detail: the sections on the left, the focused section on the right. Five sections instead of today's
eight — the save-sort migration becomes a notice with its actions inside Save Sync, Registered Devices moves into Save
Sync, and SteamGridDB joins the other external services under Connections.

| Section       | Holds                                                                                                                                                                                                                                                                                                                             |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Connections   | the services the plugin talks to: RomM (URL, account, Sign out, Allow insecure SSL), RetroAchievements (account and sign-in state; #1627 leaves the badge's home open between the game page, the System page and global settings — it lands here, and the System page retires), SteamGridDB (the API key). Home of every sign-in. |
| Save Sync     | the toggle, device, before-launch and after-exit, default slot, history limit, Sync all now; the registered devices as a table; home of the save-sort migration                                                                                                                                                                   |
| Controller    | Steam Input mode, Apply to all shortcuts, the `input_driver` fix. Home of the fix.                                                                                                                                                                                                                                                |
| Steam Library | preferred region, collection games in platform groups, collection types in Steam names — today's **Library** section, renamed because a Library page now exists: the page is the RomM side (what is synced), the section is the Steam side (which version, in which groups, under which name)                                     |
| Advanced      | log level                                                                                                                                                                                                                                                                                                                         |

Text input stays in modals — RomM URL, account, API key, default slot — because the on-screen keyboard needs the room.
The sticky pending URL and the unguarded Apply-to-all double press (#1020) are fixed in the rewrite.

## Data Management

Wide, list and detail: the operations on the left with a count where one waits, the focused operation on the right with
its explanation, its confirmation, and its progress or result. Five operations, all library-wide: Removed RomM games,
Remove all shortcuts, Uninstall all ROMs, Orphaned grid images, Non-Steam games with the whitelist. The per-platform
actions leave for Library › Platforms, and with them the platform modal.

The removed-games cleanup stops being a modal: the candidate table (game, platform, installed size, recovery-bundle
toggle), the free-space line and Start cleanup are the operation's detail pane. Its rules do not change; they live in
[removed-game-cleanup.md](removed-game-cleanup.md).

## Downloads

Narrow and unchanged: the active rows with progress, their Pause / Resume / Cancel as rows below the list, the finished
rows, Clear Completed. Reached through View All on Main while the queue is not empty; an empty Downloads page has no
menu entry.

## One home per action

| Action                             | Today                          | Target                                       |
| ---------------------------------- | ------------------------------ | -------------------------------------------- |
| Start a sync                       | Main                           | Main; the button opens the Sync page         |
| Review and apply a preview         | Main, one line                 | Sync, as a table                             |
| Force Full Sync, Skip preview      | Main                           | Sync                                         |
| Restart Steam now (session budget) | Main                           | Sync; Main shows the notice                  |
| Sync a platform on or off          | Library                        | Library › Platforms                          |
| Choose the emulator core           | System                         | Library › Platforms                          |
| Download BIOS files                | System                         | Library › Platforms                          |
| Delete BIOS files                  | System **and** Data Management | Library › Platforms                          |
| Remove one platform's shortcuts    | Data Management, modal         | Library › Platforms                          |
| Delete one platform's save files   | Data Management, modal         | Library › Platforms                          |
| Fix the RetroArch `input_driver`   | Main **and** Settings          | Settings › Controller; Main shows the notice |
| Migrate the save-file sorting      | Settings; Main links           | Settings › Save Sync; Main shows the notice  |
| Pause or cancel a download         | Downloads                      | Downloads                                    |
| Clean up removed RomM games        | Data Management, modal         | Data Management, as a page                   |

## Sequence

The pages land in this order under #1808, each with the open work that already sits in its file:

1. **The wide frame** ([#1813](https://github.com/danielcopper/romm-tender/issues/1813)) — the two levers and their
   clearing paths, the Back row, tabs, list and detail, the measured height. No page uses it yet; it is the one piece
   that rests on Steam internals and is reviewed on its own.
2. **Library** ([#1815](https://github.com/danielcopper/romm-tender/issues/1815)) — Platforms and Collections; System
   and the Data Management platform modal retire. Carries #164, #1803's frontend half, #1016's frontend half, #1020's
   collections fix. May land as two PRs, Platforms then Collections. Second on purpose: the emu-atlas work under #1735
   renders its BIOS and core changes into the new Platforms detail instead of the retiring System page.
3. **Sync** ([#1814](https://github.com/danielcopper/romm-tender/issues/1814)) — the new page, Main's four-state button
   and the Last sync row, Skip preview persisted, the run-list read, the per-platform preview breakdown. Carries #886's
   presentation half.
4. **Settings** ([#1816](https://github.com/danielcopper/romm-tender/issues/1816)) — the sections, Steam Library, the
   homes for the `input_driver` fix and the save-sort migration with their notices on Main. Carries #1020's URL and
   double-press fixes.
5. **Data Management** ([#1817](https://github.com/danielcopper/romm-tender/issues/1817)) — the operations, the cleanup
   as a pane. After Library, which removes the platform modal.

Main has no issue of its own: each change to Main lands with the page that gives it a home. Downloads has none either.
i18n (#133, #1524) comes after the rebuild, and the pages avoid copy that breaks when German or French expands it; the
store screenshots (#830) are taken after.

## Design record

- [#1808](https://github.com/danielcopper/romm-tender/issues/1808) — the epic; its sub-issues are the sequence above.
- [#1809](https://github.com/danielcopper/romm-tender/issues/1809) — the design issue this page answers.
- The static prototype the decisions were made on: [qam-prototype.html](../assets/qam-prototype.html), a single
  self-contained page kept in `docs/assets/`. Every page at device size with numbered notes; its example data is
  invented, and it reflects the decisions as of this page's first version.
- [ADR-0029](../adr/0029-wide-qam-pages-drive-steams-friends-expansion.md) — why the panel widens through Steam's
  Friends expansion and not a full-screen route, and what that rests on.
