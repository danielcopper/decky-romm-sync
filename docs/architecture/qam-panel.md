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

| Module                                                        | Responsibility                                                                                                                             |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `src/index.tsx` (`QAMPanel`)                                  | The router: one `Page` value, one mounted page, a module-level `currentPage` that survives a QAM remount                                   |
| `src/types/navigation.ts`                                     | The `Page` union — every page the router can land on                                                                                       |
| `src/components/MainPage.tsx`                                 | Main                                                                                                                                       |
| `src/components/LibraryPage.tsx`                              | Library — the frame, the two tabs and their state                                                                                          |
| `src/components/SettingsPage.tsx`, `src/components/settings/` | Settings and its sections                                                                                                                  |
| `src/components/DangerZone.tsx`, `RemovedGamesCleanup.tsx`    | Data Management                                                                                                                            |
| `src/components/DownloadQueue.tsx`                            | Downloads                                                                                                                                  |
| `src/components/library/`                                     | The Library page's tabs: `usePlatformsPage` (its reads and actions), `PlatformsTab`, `PlatformDetail`                                      |
| `src/utils/deckyUiInternals.ts`                               | Honest typing for `@decky/ui` values that come from a webpack probe: the frame's class names, `Tabs`, `ScrollPanel`, the controller glyph  |
| `src/utils/qamExpansion.ts`                                   | The panel's width: the expand and hide messages, the injected `max-width` rule, and the four paths that clear both                         |
| `src/components/qam/`                                         | The wide-page frame: `WidePage` (the Back/title line, tabs, measured height, entry focus), `ScrollRegion`, `Columns`, `ListDetail`, `pane` |
| `src/utils/entryFocus.ts`                                     | Which stop a page opens on, and the `.focus()` + `gpfocus` pair that places it — the frame's and the router's one implementation           |
| `src/utils/` module stores                                    | State that must outlive a page: sync progress, pending preview, downloads, prune, the game-detail caches                                   |

## Two widths

Every page is either narrow (348 px, 300 px of content) or wide (854 px, 806 px of content). The width belongs to the
page: a page is wide or it is not, and no view inside a page changes it. Main and Downloads are narrow; Sync, Library,
Settings and Data Management are wide.

**On the Deck, wide means full screen.** The Big Picture viewport is 854 × 534 CSS px at `devicePixelRatio` 1.5 (1280 ×
800 physical), so the narrow panel covers 41 % of the width and the wide one covers all of it — there is no library left
beside a wide page. A desktop Big Picture window is wider and shows one, which is why width judgements are made under
`mise run dev:ui-scale deck` and nowhere else.

**That 534 is the internal display and nothing more — do not size against it.** The dev loop's windowed Big Picture is a
different viewport on the same machine: measured through CEF during the third device round, the QAM view reported
`innerHeight` **764** at `devicePixelRatio` 1.71 on a 1496 × 842 screen, where the width still came out at 855. Both
numbers are real measurements of different configurations, and code that assumes either is wrong on the other — which is
why the frame measures the space it is actually given rather than deriving it from a recorded viewport.

How a page gets wide, measured on the device rather than read from documentation:

- Steam's main window holds the QAM in a sliding container: an absolutely positioned element as wide as the viewport and
  854 × 454 CSS px on the Deck — the 80 px above it is Steam's top bar — anchored right and pushed off-screen by
  `transform: translateX(506px)`, so 348 px stay visible. Steam's `Expanded` class sets `translateX(0)`. Its class names
  are hashed and there is no `ViewPlaceholder` to match on any more, so the container is found by geometry: absolutely
  positioned, a transform set, at least 800 × 400. The class follows one MobX observable on the FriendsUI store, which
  listens for `message` events on the SharedJSContext window — the window plugin code runs in. A wide page posts
  `{ message: "QamFriendsExpanded" }` to `window` on mount and `{ message: "QamFriendsHidden" }` when it lets go. The
  target origin is always `window.origin`, which addresses the message to that window and always matches it. A
  well-formed target origin that does not match is checked at delivery and the message is discarded in silence, so a
  literal one would leave the panel simply never widening.
- Every tab's content panel carries `max-width: 300px`; only Steam's Friends panel lifts it. A wide page injects one
  stylesheet whose `:has()` rule lifts the cap for a marker class on the plugin's own subtree. Class names come from
  `quickAccessMenuClasses`, which can be `undefined`; `[id^="quickaccess_content_"]` is the fallback selector. Decky
  registers one QAM tab (`QuickAccessTab.Decky = 999`), so the plugin's panel is `#quickaccess_content_999` — and
  `TabGroupPanel` sits on that same element, measured, which is why walking the DOM by id and writing the CSS against
  the class reach the same panel. That same sheet carries one rule that is not about width — Steam's own
  `outline: outset #fff 2px` for a **disabled** button under `.gpfocus`, which Steam's stylesheet omits. A wide page
  keeps its buttons rendered-and-disabled rather than hidden, so the stick lands on them and the focus ring would
  otherwise disappear for that row; it rides in this sheet because the sheet is already scoped to the wide root and a
  second injector for one selector would be a second thing to clear.
- Result: the visible panel goes from 348 px to 854 px and the tab panel from 300 px to 806 px (854 minus the 48 px tab
  rail). The QAM browser view itself is 854 px wide in both states, so only the sliding container's geometry, read
  through `findSP()`, proves an expansion.

The flag is Steam's and global, so the page that set it clears it: on unmount (navigation away, plugin closed), when the
Decky tab stops being the active QAM tab (the `ActiveTab` class on the panel's parent — a tab switch is a class change,
not an unmount), when the QAM closes (`useQuickAccessVisible`), and from `onDismount`.

Steam moves the same flag on its own, in both directions, and neither is a bug in the plugin. `OpenQuickAccessMenu`
clears it (`SetQAMFriendsChatExpanded(false)`) on every QAM tab change away from Friends, which is a second net under
the plugin's own `ActiveTab` observer; and the Friends tab's list expands it from `onFocusWithin`, so Friends goes wide
the moment gamepad focus enters it. A Friends panel that widens after a wide page closed is Steam doing that. Both are
in `chunk~2dcc5aaf7.js` in Steam's own bundle, where the receiver is also visible: `OnMessage` on the FriendsUI store
sets `m_bQamFriendsExpanded` from exactly the two messages the plugin sends, and Steam's own senders post with the
literal `"https://steamloopback.host"` — which is what `window.origin` is in the SharedJSContext.

Steam's tabbed page fills its parent instead of growing, and nothing in the QAM chain provides a height. A wide page
therefore measures the space left below its header and takes that as its height; its regions scroll inside it. A
`min-height` is not enough — it clips. Under Decky's title bar, the frame's Back row, its title and a tab bar, that
leaves a body of roughly 260 px inside the 454 px view.

**That measurement has to be free of the scrolling panel's own offset, and only a layout-relative one is**: the body's
position inside the scroller's content — its viewport top minus the scroller's, plus the scroller's `scrollTop` —
subtracted from the scroller's `clientHeight`. Every **viewport-relative** form fails, because the panel's own
`scrollTop` moves the body's rect and leaves the panel's own rect where it is. That made the measurement feed itself,
and the loop has no fixed point: a body measured part-way down comes out that much too tall, the panel then has that
much more to scroll, and nothing re-measures. It reached a device as a page that scrolled as one piece, tab row and all
— 1245 px of body inside a 750 px panel.

Measured live in the QAM over the mounted page, at panel offsets 0 / 200 / 500 / 634 px, `window.innerHeight - top`
answers 648 / 848 / 1148 / 1283 — and so does `scroller.getBoundingClientRect().bottom - top`, because the panel's rect
bottom is 764.3 against an `innerHeight` of 764. **Bounding to the panel instead of the window is therefore not the
fix**; it changes no number at any offset. The layout-relative form answers 648 at all four. What makes a non-zero
offset reachable at all is that `QAMPanel` resets the panel's scroll inside a `requestAnimationFrame`, a frame after the
page's own layout effect has already measured.

A region scrolls the way the rest of the QAM scrolls: by moving focus. Every scrolling region goes through
`ScrollRegion`, which renders Steam's plain `ScrollPanel` — the container the QAM's own tab panel is built from, and the
one Steam's tabbed page wraps each tab's content in. It is an `overflow-y: auto` box that takes no focus of its own, so
the rows inside it take focus directly and Steam scrolls the focused row into view.

**Every row a reader must be able to reach is a focusable row.** A toggle, a button, or — where a table row carries no
action of its own, so the reader can still walk the table — a `Focusable` with an `onActivate` handler. The handler is
what makes it a stop: `FocusableProps` exposes no `focusable` prop, an activate handler is what sets one, and a bare
`Focusable` is a container that passes focus on to its children rather than taking it. Plain text that only accompanies
a row, a hint under a group, scrolls with its neighbours and need not be reachable itself. This is what focus-driven
scrolling costs: content nobody can focus cannot be scrolled to.

**The one place a page cannot buy its way out of that is the content OUTSIDE its focusable rows**, and the frame handles
it rather than each page: a heading, a counts line or a column header sitting over the topmost row, and a legend, a
total or a hint under the last one, are not focusable and have no neighbour to ride along with, so once the reader has
scrolled past them Steam has no reason to bring them back — it scrolls only far enough to show the focused element.
`ScrollRegion` therefore scrolls itself to the top when focus reaches the first stop in it, and to its end when focus
reaches the last. Every region **built with `ScrollRegion`** gets that, which is not the same as every region on every
wide page: a tabbed page's own tab content sits in Steam's `ScrollingTab`, so a tab that does not build its own regions
— Collections today — is not covered. Two properties make it safe rather than a fight with Steam's own scrolling. The
triggers are **"nothing focusable is above me"** and **"nothing focusable is below me"**, never "I am the first match"
or "the last" — a container `Focusable` renders `tabindex="0"` of its own and precedes in document order every row it
wraps, so it is never the last match and a wrapped row is never the first, and an equality test against either end would
silently never fire wherever a page wraps its rows, which `ListDetail` does for every row. So the first rule discounts
the focused element's own ancestors and the second its own descendants. And each acts only where the focused element
still fits in the region at the offset it would move to: where the content beyond it is taller than the region there is
no offset showing both, Steam would scroll the element straight back, so nothing is done at all. A stop at both ends at
once reveals the top where the top fits, and otherwise the end where that fits.

The set of shapes it counts as a focus stop is measured in the running QAM, not assumed: Steam's own components render
`div[tabindex="0"]` and a `DialogButton` is a native `button` carrying no tabindex attribute at all.

**A region also keeps the wheel to itself.** Its `overscroll-behavior` is `contain`, because all three nested scrollers
here — the region, Steam's `ScrollingTab` above it, the QAM panel above that — compute `auto` by default, so a mouse
that reached the end of one went on to scroll the panel and took the frame's Back row off the top with it. A controller
never showed it: Steam scrolls a region by moving focus, not by wheel events. The property names no axis of `overflow`,
so it cannot undo the sideways clipping the bounds deliberately leave to Steam.

**A list-and-detail page's detail region is keyed on the selection**, so choosing another entry mounts a fresh region
and its detail opens at its own top rather than at the offset the previous one was left at. A key rather than a ref,
because the panel is reached through a webpack probe and nothing establishes that it forwards one; and it is safe
because focus is in the list when the selection changes — that is what changed it — so nothing focused is unmounted.

`ScrollPanelGroup` — the sibling that binds gamepad direction to scrolling, and would carry unfocusable content — was
tried on the device and rejected. It is focusable and its OK button focuses its first visible child, so each region
became a focus stop of its own: the whole list outlined as one block, A to step into it, and only then rows taking
focus. Steam's own QAM does not behave that way.

Both of a list-and-detail page's regions are `ScrollRegion`s, and so is the frame's body when the page has no tabs and
does not say it owns its regions. A tabbed body gets none from the frame, and neither does one whose page passes
`ownRegions`: see "Building blocks → Tabs" for whose job it is instead.

## Pages

| Page            | Width | Holds                                                                                                         | Today                                                                                                         |
| --------------- | ----- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Main            | 348   | notices, status, the Sync button, the download summary, the menu                                              | also holds the preview card, Skip preview, Force Full Sync and the session-budget card                        |
| Sync            | 854   | preview as a table, the import choice, Skip preview, Force Full Sync, Steam memory, session budget, last runs | does not exist; its controls sit on Main. Its backend half is built: the breakdown, the run read, the setting |
| Library         | 854   | Platforms as list and detail (sync, core, BIOS files, removal); Collections as filter and list                | Platforms is built; Collections still carries the narrow page's controls and list                             |
| Settings        | 854   | five sections, list and detail                                                                                | narrow; eight sections stacked                                                                                |
| Data Management | 854   | five library-wide operations, list and detail                                                                 | narrow; opens the cleanup in a modal                                                                          |
| Downloads       | 348   | the queue with its controls                                                                                   | unchanged                                                                                                     |

`Page` is `"main" | "library" | "settings" | "data" | "downloads"`, and becomes
`"main" | "sync" | "library" | "settings" | "data" | "downloads"` once the Sync page lands. **System is gone** — its
core picker and BIOS files are in Library › Platforms, and the value, the router branch and the menu entry left with it.

Main's menu opens Library, Settings and Data Management. The Sync page opens from the Sync button and from the **Last
sync** status row; Downloads opens from **View All** in the download summary, which is shown only while the queue is not
empty. Every page but Main opens with a **Back** chip, which returns to Main. The chip shares its line with the page
title — one row, not the three a full-width button plus a title line used to cost, which on the Deck's body is most of
what a detail pane has to spend. Back is also on **B**, and the binding lives in the panel's router (`src/index.tsx`)
rather than on a page: one `Focusable` with `onCancelButton` wraps the mounted content **only while `page` is not
`main`**, so every sub-page — wide and narrow — answers B from wherever focus sits, and Main answers nothing, so Decky's
own B still leaves the plugin. That condition is what makes taking B safe: the escape route is never removed, it is
exactly as far away as the user walked in, and the last press is never swallowed. Steam already prints "B ZURÜCK" in its
footer legend, which this makes true rather than misleading, so no legend entry of ours is needed. The chip stays as the
discoverable half and as the mouse path, and it carries **Steam's own B glyph** — drawn for the controller in the user's
hands, so it is ○ on a PlayStation pad and the swapped face button under a Nintendo layout. `@decky/ui` does not
re-export that component, so `src/utils/deckyUiInternals.ts` reaches it by a module probe and types it as possibly
absent; the chip falls back to its chevron the day the probe misses. The button number it passes is Steam's own
action-button enum (`A=0, B=1, X=2, Y=3`), **not** `@decky/ui`'s `GamepadButton`, where 1 is A — the two disagree on
every value, and the wrong one draws the wrong glyph without failing.

**A tabbed wide page has to get out of the way for that to work.** Steam's tabbed page renders its content pane as
`onCancelButton: !cancelSkipTabHeader && <focus the tab row>` (`chunk~2dcc5aaf7.js`), so without the flag the first B
inside a tab is spent moving focus to the tab row and never reaches the router. `WidePage` passes `cancelSkipTabHeader`
— Steam's own prop, which it uses in its controller-configurator dialogs, and which upstream's `TabsProps` predates;
`src/utils/deckyUiInternals.ts` types it. After a navigation the router scrolls the panel to the top, and gamepad focus
is placed on the page's first stop — by the router for a narrow page, and by the frame itself for a wide one, which says
so on its root so the router leaves it alone (see "Building blocks → Tabs"). The module-level `currentPage` survives a
QAM remount, so reopening the QAM lands on the page that was open, and a wide page re-expands on mount.

## Building blocks

### Tabs

Steam's tabbed page, switched with L1/R1, for two to four peer views of one page. Wide pages only: at 300 px the bumper
glyphs overlap the labels, which is why the Library page's tab bar is hand-rolled today. Only Library has tabs.

Entry focus lands in the content — the active tab's, so the list of a list-and-detail page — and the bumper glyphs
follow, because Steam draws them only while gamepad focus is within the tabbed page. Back stays reachable by moving up.

**Entry focus belongs to the frame, on every wide page.** `WidePage` marks its root as placing its own, so the panel's
router leaves the page alone rather than focusing its first button, which is the Back chip above the body. Where Steam's
tabbed page renders, its `autoFocusContents` does the placing; everywhere else — an untabbed page, and a tabbed one
whose `Tabs` probe missed — the frame focuses the first stop inside the body itself, on the same 50 ms delay the router
uses, because Steam's navigation resolves a focus pointer it retained across the page swap after the mount.

**The stop it picks is the first enabled focus stop in document order that contains no focus stop at all.** Document
order rather than "the first button", because a page's first button is not its first row: on a list-and-detail page
whose list rows carry no control, the first button in the body is in the DETAIL pane, so a button-first rule would open
the page inside the detail and move as the detail's content changed. Innermost, because a container `Focusable` carries
`tabindex="0"` of its own and precedes every row it wraps, so the first match would be the container and the reader
would start a step away from the row. Enabled, because a page opening on a dead control says nothing about where the
reader is — the reveal rules in `ScrollRegion` read the same shapes and do NOT skip a disabled control, since focus
still lands on one.

**The two halves read different selectors, and the difference is load-bearing.** A candidate has to be enabled, but a
container is skipped for holding a stop of ANY kind: a button row whose every button is disabled is still a container
Steam does not stop on, so treating it as the innermost candidate would put the focus ring on it and take it off the
next real row. The test cannot tell that row from one carrying an activate handler, so it skips both; a row whose only
inner stop is disabled is stepped over, which no body's first column produces today. Where that leaves no candidate — no
enabled stop that is free of stops inside it — nothing is placed and the page keeps whatever Steam's retained pointer
resolves to. The narrow pages the router still covers keep a button-first rule — their first button, now skipped past a
disabled one — because a narrow page is one column of Steam's own full-width rows and there the first button IS the
first row. Both finders, the shared set of shapes and the `.focus()` + `gpfocus` pair are `src/utils/entryFocus.ts`.

**A tab's content is the page's business, not the frame's.** The frame wraps an untabbed body in a `ScrollRegion` and a
tabbed one in nothing: Steam's tabbed page already wraps each tab's content in this same plain scroll panel, so a region
from the frame would only nest a second scroller around it. Rows of one column therefore scroll in a tab with nothing
added. A page that needs more than that one scroller — a list and a detail scrolling independently side by side — builds
its regions with `ScrollRegion` itself, which is what Library's tabs do, and an untabbed page that does the same says so
with `ownRegions` so the frame wraps its body in none either.

### Columns

One row of side-by-side scrolling regions, which is what every wide page whose content is more than one column is built
from: a `Focusable` the stick crosses horizontally, and a `ScrollRegion` per column. A column names a fixed width or
takes what is left, and may name a **region key** — joined to the column's id to form the React key its region carries —
so that changing it remounts that one column and its content opens at its own top rather than at the offset the previous
content was left at. A column that names none keeps one constant key and is never remounted. A key rather than a ref
that scrolls the region back: Steam's scroll panel is reached through a webpack probe and nothing establishes that it
forwards one.

List and detail is `Columns` with two columns. The Sync page's table beside its controls column is the next.

### The pane primitives

The pieces a detail pane is built from, in `src/components/qam/pane.tsx` so that the next pane is written against the
same scale rather than a second literal for the same size: the 11 px every secondary line is set in, the verdict
palette, the two button shapes (`FLAT_BUTTON` for a button sharing a row, `ROW_BUTTON` for a table row's action column),
a section title, a muted line, a row of buttons, and the two lines that report an action — the status line bound to the
entry and the group it belongs under, and the sentence saying which other entry is working while this pane's buttons are
disabled.

### List and detail

The list takes about a third of the width (264 px in the prototype), the detail the rest. Focus selects: moving through
the list changes the detail at once, as Steam's own settings do. A list row may carry a toggle; A operates it, never the
selection. Both regions scroll independently inside the page's measured height, and both scroll by moving focus — so a
detail pane is built from focusable rows, not from paragraphs.

A list whose rows carry no control of their own — a label and nothing else, which is what Settings and Data Management
have — asks for `selectOnActivate`, and every row wrapper takes an activate handler that selects it. That handler is
what makes the wrapper a focus stop rather than a container, so without it those rows are unreachable and the list
cannot be scrolled. It is off by default, because a row that does carry a control must leave A to it.

A list that is grouped or sorted by state computes its order when the page mounts and keeps it while the page is open,
so toggling a row does not move it out from under the focus. The next mount shows the new order.

A control that acts on the whole list — Enable all, Disable all — goes in the layout's `listHeader`, above the first row
and inside the same scrolling region. It sits outside every row on purpose: focus moving onto it must not report a
selection, because a page may do real work on one.

It spans exactly what a row spans, and that span is **not symmetric**: a row is inset on the left by its own selection
marker (a 3 px bar and a 5 px gap) and runs flush to the column's right edge. Steam's `Field`, which every row is built
from, adds nothing horizontally inside the QAM — it renders in its `Classic` mode there, whose only padding is 10 px top
and bottom — so there is no Steam inset to match and a symmetric padding on the header is simply narrower than the rows.
Measured on the device through CEF at the Deck's 854 px: rows run 79.6 → 335.9 in a 264 px list column, and the header's
pair now runs 79.9 → 335.9.

### Tables

Anything with more than two facts per row is a table with a header row: BIOS files (File, On disk, Contents), the
preview (a row per platform; New, Updated, Removed), registered devices, cleanup candidates, collections. Today those
facts were folded into a field's label and description, which is why #1803's third axis had no slot on the rows the
System page drew; the platform detail's BIOS table is where that column now sits.

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
choice (#1364, later; the page leaves the space) and Apply / Cancel. Right: Skip preview as a persisted setting (the
setting exists; the control on Main is still local state and off again on the next mount), Force Full Sync with its
explanation, Steam memory now and the last run's delta, the session-budget card with **Restart Steam now** and Resume,
and the last runs.

What the backend holds for it: the preview answer carries library-wide totals (`SyncPreviewSummary`: new, changed,
unchanged and removed counts, the platform and collection counts, and more), the names of new and changed games, and the
added and removed collection names (`collection_diff`). The same counts split per platform ride the summary as
`platform_breakdown` — one row per platform holding at least one non-zero count, ordered by display name, each carrying
`synced` for whether the platform is in the run's platform list. A `synced: false` row is a platform outside it: its
toggle went off, RomM stopped listing it, or the only route to it is an enabled collection, which is not filtered by
platform enablement. The causes compose, so one row can carry removals for the ROMs the run no longer fetches and new or
changed counts for the ROMs a collection still reaches. Its name is the run's where there is one, else a real name
carried on one of the platform's fetched entries — a reconstructed collection member carries the slug there and does not
count — else what the backend recorded, and the bare slug where no tier answers. There is no collections row there:
`collection_diff` on the same summary already carries the added and removed collection names. `get_sync_runs` answers
the ten newest `sync_runs` rows of any status, newest first, each verbatim from the `SyncRun` aggregate (id, started,
finished, status, planned counts, completed platforms and collections, error) — a field a run never recorded stays null,
and the status is what says why. Skip preview is a user-intent setting in `settings.json` written by its owner
(`adapters/persistence.py`) and reported by `get_settings`. No backend sync path consults it and nothing reads it back
yet: it is the value the page's toggle will choose between the preview and the run with. Everything else the page shows
is what Main shows today, moved.

## Library

Wide, two tabs.

**Platforms** is list and detail. The list holds every platform RomM reports with at least one ROM — what
`get_platforms` returns; a platform with nothing to sync is not listed — in two groups, **Synced** (the toggle is on)
above **Available**, each alphabetical: **a dot, the name, the toggle, and nothing else**. The dot is the row's whole
BIOS signal, through the shared mapping every platform-level BIOS dot renders through (`src/utils/biosColor.ts`: green
complete, amber partial, red missing, grey for a missing level; the per-file rows on the platform detail and the game
page hard-code the same four colours). It is drawn on every row, taking exactly the helper's grey where there is no
level to state: one that came and went shifted every name beside it, and the list is meant to be scanned down its left
edge — which matters more now that the dot carries the state alone rather than reinforcing a number beside it. The
number itself is the row's `title`, in the detail pane's own wording, and the pane's header badge states it in full.

**The row carried the ratio (`3 / 5`, an em dash where nothing is required) until the second device round, and that is
superseded rather than forgotten.** The first device round asked for it and it was added; using it decided the opposite
— a number in a line you scan past earns nothing when the pane one keypress away states it properly, with the files it
is made of. The layout study still draws it; on this point the study is superseded, and so is the earlier round's
finding. Do not restore it as a regression. Enable all and Disable all sit above the groups, in the list column and
outside every row, so reaching them reports no selection. The order freezes while the page is open.

**Every sync write is optimistic, and a write that does not take says so.** The row flips before the backend answers, so
a write that is refused or never lands puts the row — or, for Enable all and Disable all, the whole list — back where it
was, and states why in a line under those two buttons: in the list column, scrolling with the rows, plain text rather
than a focus stop. It carries the backend's own message, or a short fixed sentence where there is none, and the next
write that succeeds takes it back. A refusal resolves rather than throwing (the migration gate answers one, and so does
the RomM listing Enable all needs), so without the line a revert is indistinguishable from a toggle that never moved.

The detail offers no sync control of its own — the row already is one, focus is already there and A works the toggle,
and the list's two header buttons act on every row at once — so it opens with one header line instead of a Sync section:
the platform's name, `N on RomM · M in Steam · <core name>`, and the core picker's icon button, right-aligned.

**Both counts on that line are ROM files.** `N` is RomM's own `rom_count` for the platform; `M` is `reachable_count` —
how many of the platform's ROMs a reader can get to through a shortcut, which is every member of a sibling group that
holds a binding, because one shortcut serves the group (ADR-0021 §2) and the game's page switches versions across it.
`M` is **not** the number of shortcuts: a fully-synced 665-ROM platform behind 458 shortcuts reads `665 · 665`, where
counting bindings read `665 · 458` and so reported 207 games as missing when none was. The number of shortcuts is
`count` on the same payload, and it is what the Remove group says and acts on — the two must not be folded, or the
button offers to remove more shortcuts than exist. Where a whole game never reached Steam the two halves genuinely
differ (`3084 on RomM · 8 in Steam` for a platform with one applied game), and that difference is the line doing its
job.

Two things the line does not claim. The halves count **different populations** — the left is what RomM holds now, the
right is what our own rows say — so ROMs added on RomM since the last sync widen the gap, and equality means "nothing
outstanding as of the last sync" rather than a fresh server-side proof. And **a version RomM no longer serves is not
reachable and is not counted**: nothing deletes such a row — ADR-0007 keeps it as an identity anchor and only the
removed-game cleanup removes one — but the picker refuses a switch to it, so no reader can reach it through the group's
shortcut. (A refusal rather than a disabling: the row still renders enabled, which is how it opens the cleanup.)
`reachable_count` excludes the rows the platform's last completed fetch did not return, which
`domain/fetch_generation.py::prune_candidate_ids` already answers for the cleanup's own discovery; where no usable stamp
exists it names nothing and every row counts, so the exclusion's worst case is the number printed before it.

**That leaves one window in which the line can read right > left.** Between a ROM's deletion on RomM and the next
completed fetch of its platform, the left number has already dropped while our rows still carry the previous generation,
so the right can exceed it until that platform syncs again. Closing it would need a live server call, which this read
deliberately does not make — `get_registry_platforms` answers offline, and that is what keeps the pane useful with RomM
unreachable.

The exclusion also means **`reachable_count` is not bounded below by `count`**: a _bound_ row the last fetch did not
return raises the shortcut count without raising the header, so a pane can read `2 on RomM · 3 in Steam` beside
`Remove 4 shortcuts`. Two shapes reach it — a bound version deleted on RomM, in the gap before that run's stale-removal
scan, and a collection-added row on an already-stamped platform, which commits with no generation — and both heal on
that platform's next complete sync. The direction is a conservative under-count, which is why it is recorded rather than
guarded.

**The BIOS ratio is not on that line** — it was, and its width is what wrapped the line three times on a platform with a
long name and a long core label. It is stated once instead, beside `BIOS FILES` eight pixels below, in the colour
`biosColor.ts` gives the list's dot, so the two places that state a platform's BIOS state agree by construction. Under
it, for the focused platform:

- **Emulator core** — a **microchip icon button in the header line**, opening the same context menu the game page uses
  (`buildEmulatorMenu`). It is the game page's own button and its own colour coding: grey `#8f98a0` when the active core
  is the default option, gold `#d4a72c` when it is an override, read off the payload's `is_default` for the option
  carrying `active_core_label`. The **core clause beside it takes the same two colours from the same condition**, so the
  name and the icon cannot disagree. A full-width button under the header, with the save-compatibility caveat under
  that, is what this replaced: two rows for one action, on the pane where rows are the scarce thing. The caveat is not
  lost — `buildEmulatorMenu` renders it as the menu's first item, so the copy on the page that opens the menu was the
  same sentence twice.

  **The clause names the core; "Default" is not one of the names it can take.** `resolve_platform_label` answers with
  the real label in both ordinary cases. `null` means no option is **bakeable**, which is not the same as there being
  none, and the two are different sentences:

  - **Options exist, none bakeable, and the fallback can run** — the plain RetroDECK launch is baked and RetroDECK
    resolves the emulator itself. The clause reads `RetroDECK decides` in the muted colour and the line under it says
    the plugin cannot pin one; neither promises a launch, because what the fallback then finds is between RetroDECK and
    the machine.
  - **Options exist, none bakeable, and the fallback is not installed** — the same unpinnable state, with the opposite
    outcome. `run_game.sh` takes `command[1]` for the system when no alternate emulator is set and `options_to_payload`
    keeps ES-DE's document order, so `emulators[0]` **is** that command; when its own `reason` is `not_installed`, the
    fallback names a binary that is not there. The clause reads `no emulator installed` in **red** and the line names
    the emulator RetroDECK would have used. Only `downgrade_if_not_installed` ever sets that reason, and only on an
    otherwise-bakeable option, so the branch fires exactly where the first command's standalone emulator is missing. A
    first command unbakeable for another reason (`quoting`) whose emulator is also missing keeps the muted sentence —
    `macintosh` is that shape — because installedness is not established for an option that was never bakeable. **Apple
    I is the muted case, not this one**: ES-DE gives it two live commands, both MAME, and the first is a _libretro_ one
    whose core is installed, so it reads `RetroDECK decides` and its games start. The three standalone entries in that
    block are commented out and are not commands at all.
  - **No options at all** — `_resolve_system` falls through to the raw RomM slug for a platform its map does not name,
    and `get_emulator_options` answers `available: true` with an empty list for a system `es_systems.xml` does not list;
    `vic-20`, `acorn-electron`, `nintendo-dsi`, `ps5`, `browser` and `win` are in neither. RetroDECK's own launch then
    reads `command[1]` for the system, finds nothing, and exits 1 (`libexec/run_game.sh`). The clause reads
    `no emulator` in **red** and the line says the games will not launch, because they will not.

  The chip is disabled for all three, never withheld. Printing "Default" for any of them said the plugin had chosen;
  printing `no emulator` for all three said the games would not start where they do. Both were wrong, in opposite
  directions, and the middle state is why the split is three rather than two: it is unpinnable like the first and does
  not start like the last.

  **The button is always rendered**, and opens a menu only when there is something to pick: the platform has games in
  Steam, the core read landed, RetroDECK was found, at least one option is bakeable, and there are at least two. In
  every other case it is the same chip, disabled, with the reason in its `title` — the ruling the Remove group already
  follows, and what keeps the header's shape constant across panes. A disabled button is still a focus stop and the wide
  page's own sheet gives it Steam's focus outline, so a reader walking the header lands on it and is told why.

  **Which of those cases also keeps a line under the header is a judgement about what it reports, not about the chip.**
  "Nothing to switch" states — sync this platform first, the read in flight, one emulator on the menu — say it in the
  tooltip alone: a sentence would spend a row of the pane reporting that nothing can be done, which is what the device
  round asked to remove. States that report a PROBLEM keep their line, because a tooltip is a hover and the Deck's
  controller cannot perform one: the read failed, RetroDECK was not found, **ES-DE lists no emulator at all**, nothing
  on its menu is bakeable, and the fallback is not installed. The first two of those three are the split above and they
  are checked in that order: an empty menu is the case where RetroDECK's own fallback fails too, so it is answered
  before the not-bakeable one, and the surviving count branch then speaks only for a menu that really does hold one
  bakeable option. The frontend half of #1016 lands in the same place: a switch the backend refuses is reported there,
  and the header keeps naming the core that is actually active. A switch takes the page's busy hold from the moment it
  is picked until it is over; an accepted one re-bakes the launch command of every bound shortcut, which is why the hold
  has to cover the whole of it. The chip and the pane's buttons disable, another platform's pane says `Working on X`,
  and the acting pane says `Switching to <emulator>…` in the same status line the outcome lands in — a success takes
  that line back, a refusal replaces it, and a continuation cancelled by leaving the page takes it back too, because
  such a switch either committed or never ran and there is no pane left to report to either way.
- **BIOS files** — the summary (required, or files, and the two unknown states), then a table: File, On disk, Contents,
  and a **Download** button on every row that is missing and in the RomM library (#164) — never on a folder declaration,
  whatever its state, because the emulator opens that name as a directory — and a **Delete** button on every row a
  download record of ours still holds. That covers a declared **folder** too, where no record carries the row's name and
  the button counts the distinct files our records name underneath it (`Delete (N)`): a folder is never a download,
  which says nothing about the files already inside one. Same authority as `Delete BIOS`, described below. Below the
  table one row of buttons: Download required (_N_), Download all, Delete BIOS behind a `ConfirmModal`. **All three are
  always rendered and disable when there is nothing to do**, the ruling the Remove group already had: on PS2 all three
  vanished at once, and a button that disappears is a state the reader has to work out. A disabled `DialogButton` is
  still a focus stop, so the row stays walkable.

  **A running download is said by the button that started it.** The pressed button — bulk or per-row — becomes a
  spinner, every other download button on the pane disables, and when it finishes the rows re-read. There is no
  "Downloaded _X_" notice any more: a success says itself. A **failure** still gets words, in the same status line under
  the section, carrying the backend's own message; for a DOWNLOAD that line is failure-only, and the pressed button
  itself says `Failed` in red for two seconds before everything returns. The platform-wide Delete BIOS still writes its
  result there on success too ("Deleted 3 BIOS file(s)"), which is the one outcome on this pane no row can show; a row's
  own Delete says it by the row changing. The spinner is keyed on the run's slug as well as the button's identity, so
  walking to another platform mid-download shows disabled buttons and the "Working on _X_" line rather than a spinner
  that belongs elsewhere.

  **The per-row Delete is authorised by the download record and nothing else** — the row carries `deletable_count`,
  which the backend derives from the same records the platform count comes from (`_stamp_deletable`), and the unlink
  re-reads the record and takes the path it holds. `downloaded` is `os.path.exists` and is equally true of firmware
  RetroDECK ships: `dolphin-emu/Sys/codehandler.bin` sits one row above a real download on a GameCube pane, no RomM
  library can hand it back, and authorising on presence destroyed exactly that file on a real device. All three buttons
  — the platform's, a file row's and a folder row's — run **one** removal loop (`_delete_recorded_io`) under different
  record predicates, because a second copy of that loop is exactly what the register's BIOS-delete rule warns about.

  **`On disk` holds marks and never text**, and it is the only place presence is stated. A cell carries **one or two**
  of them.

  **Mark 1, on every row**, carries two facts at once. The **glyph is the verdict** — `✓` met, `✗` not met, `?` nothing
  could establish it — read off `BiosFileEntry.satisfied` and never off presence, because for a folder declaration the
  two come apart entirely. The **colour is the need**: strong where the launching core requires the file (green `✓`, red
  `✗`), muted where it does not (pale green `✓`, grey `✗`), keyed on `required_by_active` so the table and the summary
  above it cannot mean different things by "required". Two states have no place in that four-way scheme and are **not**
  folded into it, and they are **not the same state either**. A verdict nothing could establish is an amber `?` — the
  glyph channel has nothing to say. A row no installed emulator could be asked about keeps its verdict, which IS
  established, and goes amber on the colour channel alone: an amber `✓` or `✗`. Reading the need axis first would spend
  the glyph on a need-axis fact and throw the verdict away, on exactly the platform made entirely of such rows.
  `optional` and `not_needed` do share the muted branch: for the core about to launch, neither is a gap.

  **Mark 2, `⊘` in violet, appears beside mark 1 wherever `on_server` is `false` and the declaration is a file** — the
  RomM library does not hold this one. A declared **folder** is excluded, and not as a special case: no library holds a
  folder, so the backend stamps every folder row `on_server: False` unconditionally and the mark would say "your library
  does not hold this" about something nothing could. That is the sentence `biosFileNote` already refuses to produce, and
  the reason the download filter and the download batch refuse those rows too. It is **additive and never a
  replacement**: a present file you could not fetch again keeps its green `✓` and gains the `⊘`, and a required missing
  one keeps its red `✗`. Folding the two axes into one colour channel is what would collapse required and optional among
  exactly the rows that cannot be downloaded. It reads the field only for display; the readiness count, the progress
  ratio and the download affordance each read it their own way, and the invariant register in `CLAUDE.md` owns that
  rule.

  A legend under the table names the marks it actually contains, **one entry per line** — an entry for a state no row is
  in explains nothing and costs a row, and mark 2 is inside that filter with one line of its own rather than one per
  verdict it can stand beside. The legend is the only one of the three wordings a controller user can reach (the others
  are `title` attributes), so it words the amber rows as what they are — nothing could say whether the file is wanted —
  and never as "nothing asked for it", which is the `not_needed` claim and a synonym of the grey "missing, not required"
  two lines below it.

  On a platform nothing could answer for, the rows nothing could be asked about are **counted once**, in the line under
  the table that also says where to report the gap; the summary above it states the condition without a number, because
  the count up there was the same sentence twice on one screen. Counts on this pane are pluralised (`1 file`,
  `2 files`), never written as `file(s)`.

  Everything a row says in words goes **under the row, full width** — `biosFileNote`'s note first, then a folder's
  images — because a 48px cell wraps one sentence across three lines. The one note that does not appear there is the
  library one ("not in your RomM library" and its missing variant), which mark 2 now carries: the helper flags it as
  `fromLibrary` so this surface can drop it without re-deriving the helper's precedence, and the game page's BIOS tab,
  which has room, still prints it. Notes are rare on a healthy install — over the `.info` corpus the rows that carry one
  are the handful RetroDECK supplies itself and PS2's folder declaration. That is not a bound on the vocabulary:
  `biosFileNote`'s caveat wording ("its location could not be read", "a folder is here, where the emulator opens a
  file") appears wherever a destination cannot be read, which no corpus predicts.

  The file name is printed once. The description beside it is **not RomM's** — `_group_server_firmware` builds no
  description at all and `_wanted_fields` overwrites what came in, so what arrives is the core's own `firmwareN_desc`,
  or the file name itself for a row no placement covers. Both spell the name into the words, and across the 292 `.info`
  files a stock RetroDECK ships (695 declared entries) they do it in three shapes: the description IS the name (35%),
  the name then prose (47%), or the name with its directory then prose (17%). The rule is to drop a leading token that
  names this file — as itself or at the end of a path — and keep the rest verbatim, with a first half that strips the
  name where the description opens with it verbatim, which is the only way a name containing spaces can be seen
  (`"7800 BIOS (U).rom (7800 BIOS)"`). Surrounding quotes are stripped before that comparison, which is what reaches the
  corpus's one folder declaration (`"'pcsx2/bios' folder"`, on a row whose name line already shows that path). Together
  they fire on 690 of the 695; of the five printed whole, three name a folder the file sits in and two are upstream
  misspellings of the file.

  **The description is on its own line under the row**, muted and clipped to one line, not beside the name: at the
  Deck's scale the `File` column is ~150 px and a fifty-character parenthesis was clipped mid-word on every row that had
  one. The **declared folder** goes the other way, onto the name line as a muted prefix (`dc/` **`dc_boot.bin`**), where
  it belongs to the file's identity — `declared_path` carries it, because `file_name` is a basename and `local_path` is
  joined under a root the frontend does not know. 207 of the 695 declarations name a subdirectory and their descriptions
  spell it in only 115, so the description was never a substitute. A row can therefore carry two lines under it — the
  description first, then `biosFileNote`'s note — and neither is in a cell any more. Contents is answered for a folder
  declaration only: the count of images it holds (the resolver's verbatim strings are listed full-width under the row,
  `pre-wrap`, because the padding in them is what makes a line matchable against the emulator's own picker), or that it
  holds none, or that nothing could establish its contents. A file row reads an em dash, and that em dash means the
  question was never asked — the machine-wide reading is deliberately unverified, #1803 is what will ask it, and until
  then the dash must not come to mean "asked, and nothing found". The section appears whenever the firmware read speaks
  for the platform, synced or not — there is nothing to say about one it does not cover.
- **Remove** — Remove _N_ shortcuts and Delete _N_ save files on one row, the actions the Data Management platform modal
  used to offer, without Delete BIOS (it is one group up). Red, last, each behind a confirmation, and with **no heading
  over them**: both buttons name what they remove and are drawn in red, so a title says nothing they do not. **Both
  buttons are always rendered and disable when there is nothing to delete; neither is ever hidden.** Hiding the group on
  the shortcut count alone strands a platform whose shortcuts were removed and whose saves remain — those saves are then
  unreachable, and this is the only page that offers them. Only the shortcut removal is gated on a running sync
  (`remove_platform_shortcuts` carries `@sync_active_blocked`; `delete_platform_saves` deliberately does not), so the
  hint under the pair names that button rather than reading as though it covered both. A count still being read is
  neither a zero nor a failure, and the saves button must not look like either: while it is coming the button is
  disabled and carries a **spinner**, which claims nothing — a pressable plain label would invite a press over an
  unknown set, and a `0` would state an emptiness nobody established. A read that **failed** is the third case and says
  so in a line under the pair, because with a spinner above it a silent failure is a spinner that never stops; the
  button stays pressable there, since a failed count is not evidence that there is nothing to delete.

Five reads feed the tab. Three are list-shaped and run once per page mount: `get_platforms` (RomM's platforms with ROMs,
the list itself), `get_firmware_status` (BIOS state for the platforms it can speak for) and `get_registry_platforms`
(ROMs bound to a Steam shortcut per platform — the shortcut counts, and what "has synced games" means here). Only the
first gates the list; the other two fill in beside it, and **each says so on the pane when it fails**, because for both
of them a failure and an answer arrive the same way — as an absence. A failed `get_registry_platforms` read as zero
shortcuts would empty the header, withdraw the core picker behind "sync this platform first" and disable the removal,
three claims about a platform nothing was learned about; the counts go to `null` instead, which is not zero, and a line
under the header says the number is missing while the removal stays live (it needs only the slug). A failed
`get_firmware_status` is worded apart from a platform the overview genuinely has no entry for, which is a finished
answer — and a failed **re-read** is that finished answer again, not a third state: an answer set is still held, so a
pane with no entry of its own goes on saying "nothing is known about this platform's BIOS files" and the notice above it
warns that the whole answer may be stale. Only a first read that never landed leaves a pane with nothing to say.

The other two are **per-platform-slug reads issued once per selection** and cached for the life of the page.
`get_system_core_info` exists because neither list read can answer for the focused platform: `get_platform_core_info` is
keyed by ROM and layers that ROM's own pin on top, and the firmware overview carries no entry at all for a platform it
has nothing to say about. It costs one ES-DE options read and a `settings.json` lookup, and opens no database
transaction. `count_platform_saves` answers how many save files the platform holds, for the Delete _N_ save files
button: nothing else knows the number, because the delete finds its files through the platform's installed ROMs and
counts only what it removed, afterwards. It walks that same path without deleting, and **that path is 3N+1 short
`BEGIN IMMEDIATE` transactions** in the ordinary case, not one. The platform's id read opens one
(`SaveService._installed_rom_ids_on_platform`), and `find_save_files` → `RomInfo.get_rom_save_info` opens three more per
ROM: `rom_installs.get`, then `current_save_sorting()` — which is unconditional — asking `pending_sort_settings()` and,
because nothing is normally pending, `_read_current_sort_settings()` behind the same `or`. Nothing memoises the sorting
answer, so both are re-read for every ROM. A pending save-sort migration makes it **2N+1**: the `or` short-circuits.
`sort_by_core` recorded makes it **4N+1** and adds an ES-DE read per ROM, because `resolve_retroarch_corename` →
`ActiveCoreResolver.active_core_for_rom` opens a fourth transaction for the ROM and its install and then resolves
through `get_emulator_options(system)` — the heavy read, which globs each option's emulator install through the find
rules, not the cheaper `get_default_emulator`. On a 128-ROM platform that is 385 lock acquisitions ordinarily and 513
with sort-by-core. That cost is deliberate and is not the read's to fix: it must walk exactly what the delete walks, or
the number offered stops being the number taken. What keeps it out of the way is that it is offloaded off the event loop
and asked once per selection, and that a failure — `SQLITE_BUSY` among them — degrades to a line saying the count could
not be read rather than to a wrong number — and that failure forgets the slug, so re-selecting the platform asks again,
which is what the line says and is the only failure on this pane that does not need the page reopened. It is asked again
after a delete, so the button stops offering saves that are gone.

**Collections** has no per-entry detail, so it is one wide list: the favorites toggle and the Mine / All owner scope on
top, the kind filter (Standard, Smart, Virtual — with the Franchise / IGDB Collection split inside Virtual), the fuzzy
search with its 50-row render cap, Enable all / Disable all with today's semantics, and rows with name, kind, a **mine**
marker (the payload carries `is_own`, not an owner name), ROM count and the toggle. The collections tab's permanent
brick on one transient failure (#1020) is fixed as part of the rewrite.

## Settings

Wide, list and detail: the sections on the left, the focused section on the right. Five sections instead of today's
eight — the save-sort migration becomes a notice with its actions inside Save Sync, Registered Devices moves into Save
Sync, and SteamGridDB joins the other external services under Connections.

| Section       | Holds                                                                                                                                                                                                                                                                                                      |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Connections   | the services the plugin talks to: RomM (URL, account, Sign out, Allow insecure SSL), RetroAchievements (account and sign-in state; #1627 left the badge's home open between the game page, the retired System page and global settings — it lands here), SteamGridDB (the API key). Home of every sign-in. |
| Save Sync     | the toggle, device, before-launch and after-exit, default slot, history limit, Sync all now; the registered devices as a table; home of the save-sort migration                                                                                                                                            |
| Controller    | Steam Input mode, Apply to all shortcuts, the `input_driver` fix. Home of the fix.                                                                                                                                                                                                                         |
| Steam Library | preferred region, collection games in platform groups, collection types in Steam names — today's **Library** section, renamed because a Library page now exists: the page is the RomM side (what is synced), the section is the Steam side (which version, in which groups, under which name)              |
| Advanced      | log level                                                                                                                                                                                                                                                                                                  |

Text input stays in modals — RomM URL, account, API key, default slot — because the on-screen keyboard needs the room.
The sticky pending URL and the unguarded Apply-to-all double press (#1020) are fixed in the rewrite.

## Data Management

Wide, list and detail: the operations on the left with a count where one waits, the focused operation on the right with
its explanation, its confirmation, and its progress or result. Five operations, all library-wide: Removed RomM games,
Remove all shortcuts, Uninstall all ROMs, Orphaned grid images, Non-Steam games with the whitelist. The per-platform
actions have left for Library › Platforms, and the platform modal with them.

The removed-games cleanup stops being a modal: the candidate table (game, platform, installed size, recovery-bundle
toggle), the free-space line and Start cleanup are the operation's detail pane. Its rules do not change; they live in
[removed-game-cleanup.md](removed-game-cleanup.md).

## Downloads

Narrow and unchanged: the active rows with progress, their Pause / Resume / Cancel as rows below the list, the finished
rows, Clear Completed. Reached through View All on Main while the queue is not empty; an empty Downloads page has no
menu entry.

## One home per action

| Action                             | Today                  | Target                                       |
| ---------------------------------- | ---------------------- | -------------------------------------------- |
| Start a sync                       | Main                   | Main; the button opens the Sync page         |
| Review and apply a preview         | Main, one line         | Sync, as a table                             |
| Force Full Sync, Skip preview      | Main                   | Sync                                         |
| Restart Steam now (session budget) | Main                   | Sync; Main shows the notice                  |
| Sync a platform on or off          | Library                | Library › Platforms                          |
| Choose the emulator core           | Library › Platforms    | Library › Platforms                          |
| Download BIOS files                | Library › Platforms    | Library › Platforms                          |
| Delete BIOS files                  | Library › Platforms    | Library › Platforms                          |
| Remove one platform's shortcuts    | Library › Platforms    | Library › Platforms                          |
| Delete one platform's save files   | Library › Platforms    | Library › Platforms                          |
| Fix the RetroArch `input_driver`   | Main **and** Settings  | Settings › Controller; Main shows the notice |
| Migrate the save-file sorting      | Settings; Main links   | Settings › Save Sync; Main shows the notice  |
| Pause or cancel a download         | Downloads              | Downloads                                    |
| Clean up removed RomM games        | Data Management, modal | Data Management, as a page                   |

## Sequence

The pages land in this order under #1808, each with the open work that already sits in its file:

1. **The wide frame** ([#1813](https://github.com/danielcopper/romm-tender/issues/1813)) — the two levers and their
   clearing paths, the Back row, tabs, list and detail, the measured height. No page uses it yet; it is the one piece
   that rests on Steam internals and is reviewed on its own.
2. **Library** ([#1815](https://github.com/danielcopper/romm-tender/issues/1815)) — Platforms and Collections; System
   and the Data Management platform modal retire. Carries #164, #1803's column, #1016's frontend half, #1020's
   collections fix. Lands as two PRs, Platforms then Collections. Second on purpose: the emu-atlas work under #1735
   renders its BIOS and core changes into the new Platforms detail instead of the retired System page. **Platforms has
   landed**; Collections keeps the narrow page's controls and list until its own PR.
3. **Sync** ([#1814](https://github.com/danielcopper/romm-tender/issues/1814)) — the new page, Main's four-state button
   and the Last sync row, Skip preview persisted, the run-list read, the per-platform preview breakdown. Carries #886's
   presentation half. **The backend half has landed**; the page and Main's button are still to come.
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
- The layout study the Platforms tab was chosen from: [library-layouts.html](../assets/library-layouts.html) — the three
  layouts weighed for this page (list and detail, detail with a header, one wide table) at the Deck's real size, each
  with what it costs. The second is what shipped. **Superseded on two points by the device rounds**: the list row's BIOS
  ratio (dropped — the row is dot, name, toggle) and the core picker's full-width button (now an icon in the header
  line). The study is a record of a choice, not a description of the page.
- The static prototype the decisions were made on: [qam-prototype.html](../assets/qam-prototype.html), a single
  self-contained page kept in `docs/assets/`. Every page at device size with numbered notes; its example data is
  invented, and it reflects the decisions as of this page's first version. Redrawn to the Deck's real 854 × 534 CSS px —
  Steam's 80 px top bar over a 454 px panel — once the device round had measured them.
- [ADR-0029](../adr/0029-wide-qam-pages-drive-steams-friends-expansion.md) — why the panel widens through Steam's
  Friends expansion and not a full-screen route, and what that rests on.
