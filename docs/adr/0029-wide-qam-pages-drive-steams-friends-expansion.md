# A QAM page widens the panel by driving Steam's Friends-tab expansion, and the width belongs to the page

## Status

Accepted. Records the width decision behind [#1809](https://github.com/danielcopper/romm-tender/issues/1809) (the
panel's target structure), the first step of [#1808](https://github.com/danielcopper/romm-tender/issues/1808) (the QAM
restructure). The structure itself lives in [qam-panel.md](../architecture/qam-panel.md).

## Context

Steam renders the Quick Access Menu 348 px wide, and every tab's content panel is capped at 300 px. The plugin's panel
was built inside that number: six pages behind a router, each a full-screen replacement with a Back row, because a
narrow column cannot show a list and its detail at once; sections without titles; rows that fold two facts into a label
and a description because a third column has nowhere to go.

Steam's own Friends tab runs at 854 px. The main window's `.ViewPlaceholder` is always 854 px wide, anchored right and
pushed off-screen by `transform: translateX(506px)`; Steam's `Expanded` class sets `translateX(0)`. That class follows
one MobX observable, `qamFriendsExpanded`, on the FriendsUI store, which registers a `message` listener on the
SharedJSContext window — the window plugin code runs in — and flips on `QamFriendsExpanded` / `QamFriendsHidden`. The
per-tab 300 px cap is a second, independent rule; only `.TabGroupPanel.tab_Friends` lifts it.

Measured on the device (Big Picture, CEF Chrome 126), not read from documentation: posting the message widens the
visible panel from 348 px to 854 px; lifting the cap widens the plugin's tab panel from 300 px to 806 px (854 minus the
48 px tab rail). Steam ships no API for either, and Decky Loader has no notion of a wide plugin tab. Both levers are
Steam internals with no compatibility promise.

Three alternatives were weighed:

- **Stay at 348 px** and keep compressing. No open issue exists _because_ of the cap; its cost shows up as compromises
  inside otherwise-real issues (#164's missing per-file action, the totals line #886 offers as its direction (b) — a
  line, because the preview renders as one — and #1803's third column with no slot). The drill-down router is the
  largest such compromise and appears in no issue at all.
- **A full-screen Steam route** via `routerHook.addRoute`. It leaves the QAM entirely: the whole screen, no overlay over
  a running game, its own navigation model. The project's stated direction (#269) is surfaces that feel native, and a
  custom route is the opposite of that; a wide QAM tab is what Steam itself does for Friends.
- **Width per view inside a page** — a page that widens only when a detail is open. Both rules were walked on the device
  and are indistinguishable in use; the per-view rule has two places to set the flag and two to clear it.

## Decision

A page of the panel may be **wide**, and the width belongs to the page: a page is wide or it is not, no view inside a
page changes it. Main and Downloads stay narrow; Sync, Library, Settings and Data Management are wide.

A wide page, while mounted, holds both levers: it posts `QamFriendsExpanded` to `window` with `window.origin` as the
target origin, and it injects one stylesheet whose `:has()` rule lifts the tab panel's `max-width` for a marker class on
the plugin's own subtree. The class names come from `quickAccessMenuClasses`, a webpack probe that can be `undefined`
and is therefore read through the repo's honest-typing module (`src/utils/deckyUiInternals.ts`), with
`[id^="quickaccess_content_"]` as the fallback selector.

Whoever sets the flag clears it. The page posts `QamFriendsHidden` on unmount (navigation away, plugin closed), when the
Decky tab stops being the active QAM tab (the `ActiveTab` class on the panel's parent — a tab switch is only a class
change, no unmount), when the QAM closes (`useQuickAccessVisible`), and from the plugin's `onDismount`.

## Consequences

- **The wide state rests on undocumented Steam internals** — the message name, the placeholder, the `Expanded` class,
  the per-tab cap. A Steam update can silently stop the expansion; the plugin's own rule would still lift the cap, so a
  wide page would render 806 px of content inside a 348 px panel and clip. No fallback switch is built for that case:
  the rebuild proceeds, and a switch or a width-dependent navigation is reconsidered only if updates keep breaking it.
  The expansion is measurable in the dev loop through the placeholder's geometry (`findSP()`); the QAM browser view
  itself is 855 px wide in both states and proves nothing.
- **The flag is Steam's and global.** A page that leaks it leaves Steam's own QAM expanded until the Friends tab toggles
  it back. The four clearing paths above are the whole discipline.
- **`window.origin`, never a literal origin.** `postMessage` throws on a target-origin mismatch, and one of the callers
  is `onDismount`, where a throw abandons the rest of the plugin's teardown. The spike found this with 72 failing tests.
- **A wide page needs a definite height.** Steam's tabbed page fills its parent instead of growing, and nothing in the
  QAM chain provides a height; a `min-height` clips. The wide frame measures the remaining viewport and lets its regions
  scroll inside it.
- **L1/R1 tabs are a wide-only control.** At 300 px the bumper glyphs overlap the labels, which is why the Library
  page's tab bar is hand-rolled today. Narrow pages keep buttons.
