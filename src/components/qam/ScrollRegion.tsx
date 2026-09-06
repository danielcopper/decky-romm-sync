/**
 * One scrolling region of a wide page, bounded by the height its parent gives it.
 *
 * The region is Steam's plain `ScrollPanel`, the container the QAM's own tab
 * panel is built from. It is not a focus stop of its own: the rows inside take
 * focus directly and Steam scrolls the focused row into view, which is how the
 * rest of the QAM scrolls. Reaching content is therefore the caller's side of
 * the bargain — every row a reader must reach is a focusable row, and text that
 * only accompanies one scrolls along with it.
 *
 * The one thing the caller cannot buy that way is the content OUTSIDE its
 * focusable rows: Steam scrolls a region only far enough to show the focused
 * element, so a heading or a column header over the topmost row stays off the
 * top, and a legend or a total under the last row stays off the bottom. That is
 * this component's own job, and it is here rather than on a page so that every
 * region built with `ScrollRegion` inherits it — see `revealEdge`. That is not
 * every region on every wide page: a TABBED page's content sits in Steam's own
 * `ScrollingTab`, which the frame does not wrap, so a tab that does not build
 * its own regions does not get this.
 *
 * The panel comes from a webpack probe that can miss, and a page whose regions
 * silently vanish would be worse than one that scrolls without what the panel
 * adds on top — so the fallback keeps the region, its bounds and its place in
 * the focus tree, and gives up Steam's scroll padding, its own focus-ring root
 * and the ref that scrolls the focused element back into view after a resize.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import type { CSSProperties, FC, FocusEvent, ReactNode } from "react";
import { Focusable } from "@decky/ui";
import { ScrollPanel } from "../../utils/deckyUiInternals";
// The shapes a focus stop takes, shared with the entry-focus finder that reads
// the same DOM for a different question. Disabled controls are in it: focus
// lands on them, so a reveal rule that skipped them would misjudge which stop
// is the first or the last.
import { FOCUS_STOPS } from "../../utils/entryFocus";
import { offsetWithinScroller } from "../../utils/scrollHelpers";

export interface ScrollRegionProps {
  /** Merged over the region's own bounds — the caller places it, not sizes it. */
  style?: CSSProperties;
  children?: ReactNode;
}

// `height: 100%` and `minHeight: 0` together are what let a flex child scroll
// rather than grow: without the floor reset a flex item's min-height is its
// content, so the region expands past its parent and the page clips instead.
//
// No overflow here — the panel branch must not set one. Steam's own `ScrollY`
// class carries `overflow-y: auto` with `overflow-x: hidden` and a 20 px scroll
// padding (`._29WypCpglgRKsR_fMPsoFX`, `css/chunk~2dcc5aaf7.css`), and an inline
// `overflow` shorthand beats both of those axes. The sideways scroll it would
// restore is one Steam clips on purpose: one over-wide row and every focus step
// inside the region would drag the whole pane left and right under the reader.
//
// `overscroll-behavior` is a different property and safe to set: it does not
// name an axis of `overflow`, so it cannot undo Steam's sideways clipping. It
// stops the WHEEL from chaining out of a region that has reached its end —
// measured on device, all three nested scrollers here compute `auto`, so a
// mouse at the bottom of the detail pane went on to scroll Steam's tab panel
// and took the Back row off the top with it. A controller never showed it,
// because Steam scrolls a region by moving focus rather than by wheel events.
const BOUNDS: CSSProperties = { height: "100%", minHeight: 0, overscrollBehavior: "contain" };

// Long enough to land after Steam's own focus scroll, which would otherwise
// undo this one. The same 50 ms every helper in `utils/scrollHelpers.ts` uses,
// for the same reason.
const AFTER_STEAMS_OWN_SCROLL_MS = 50;

/**
 * Reveal the region's top, once focus has reached the first thing in it that
 * can hold focus, and answer whether it did.
 *
 * **It cannot fight Steam's own scrolling, because it never moves the focused
 * element out of view**: the scroll happens only when that element still fits
 * within the region at offset zero. Where the content above the first row is
 * taller than the region itself there is no offset that shows both, and Steam
 * would immediately scroll the element back — so this refuses, and the caller
 * decides what else is worth trying.
 */
function revealTop(region: HTMLElement, focused: HTMLElement): boolean {
  const focusedBottom = offsetWithinScroller(focused, region) + focused.getBoundingClientRect().height;
  if (focusedBottom > region.clientHeight) return false;
  setTimeout(() => region.scrollTo({ top: 0, behavior: "smooth" }), AFTER_STEAMS_OWN_SCROLL_MS);
  return true;
}

/**
 * Reveal the region's end, once focus has reached the last thing in it that can
 * hold focus — the mirror of {@link revealTop}, for the legend, the total line
 * or the hint a page puts under its last row.
 *
 * It never moves the focused element out of view either, and by the same test
 * read from the other end: the scroll happens only where everything from that
 * element to the end of the content fits within the region, so the element is
 * still on screen once the region sits at its end. It answers whether it
 * scrolled for symmetry with {@link revealTop}; nothing follows it today.
 */
function revealBottom(region: HTMLElement, focused: HTMLElement): boolean {
  if (region.scrollHeight - offsetWithinScroller(focused, region) > region.clientHeight) return false;
  setTimeout(() => region.scrollTo({ top: region.scrollHeight, behavior: "smooth" }), AFTER_STEAMS_OWN_SCROLL_MS);
  return true;
}

/**
 * Reveal whichever end of the region focus has just reached, if either.
 *
 * Both rules are stated against what else can hold focus, never against a
 * position in the match list: a container `Focusable` renders `tabindex="0"` of
 * its own and precedes in document order every row it wraps, so it is never the
 * last match and a wrapped row is never the first — an equality test against
 * either end would silently never fire wherever a page wraps its rows. So
 * "nothing focusable is above me" discounts the focused element's own ancestors,
 * and "nothing focusable is below me" discounts its own descendants.
 *
 * A stop at both ends at once reveals the top where the top fits, and otherwise
 * the end where that fits. Reaching such a stop is entering the region, so the
 * top is what a reader has yet to see; but a region whose one row sits below a
 * screenful of heading has no offset showing that row and its top together, and
 * there the end is still worth revealing.
 */
function revealEdge(event: FocusEvent<HTMLElement>): void {
  const region = event.currentTarget;
  const focused = event.target;
  // Asked of the node's OWN view, never the module's global. Plugin code runs
  // in the SharedJSContext window and these nodes belong to the QAM's separate
  // document, so a bare `focused instanceof HTMLElement` names a different
  // realm's constructor and is false for every node this handler will ever
  // see — measured in the running client, where the two documents do not even
  // share a URL. `WidePage` takes its view from `ownerDocument` for the same
  // reason.
  const view = region.ownerDocument.defaultView;
  if (!view || !(focused instanceof view.HTMLElement) || region.scrollHeight <= region.clientHeight) return;

  const stops = [...region.querySelectorAll(FOCUS_STOPS)];
  const reached = stops.indexOf(focused);
  if (reached < 0) return;

  const isFirst = !stops.slice(0, reached).some((stop) => !stop.contains(focused));
  const isLast = !stops.slice(reached + 1).some((stop) => !focused.contains(stop));
  if (isFirst && revealTop(region, focused)) return;
  if (isLast) revealBottom(region, focused);
}

export const ScrollRegion: FC<ScrollRegionProps> = ({ style, children }) =>
  // Focusable, not a div, when the probe misses: it is the very base panel
  // Steam's scroll panel renders, so the region keeps the same place in the
  // focus tree and takes focus no more than the panel does. That branch has no
  // Steam class behind it, which makes it the one place the overflow is ours.
  //
  // `onFocus` reaches the DOM on both branches, and on the panel branch by the
  // same route `ListDetail`'s row handlers already take: the panel spreads
  // whatever it does not destructure into the base panel `Focusable` renders,
  // and that is the element the attribute lands on. React delivers it through
  // `focusin`, so it fires for focus landing anywhere inside the region.
  ScrollPanel ? (
    <ScrollPanel style={{ ...BOUNDS, ...style }} onFocus={revealEdge}>
      {children}
    </ScrollPanel>
  ) : (
    <Focusable style={{ ...BOUNDS, overflow: "auto", ...style }} onFocus={revealEdge}>
      {children}
    </Focusable>
  );
