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
 * The one thing the caller cannot buy that way is the content ABOVE its first
 * focusable row: Steam scrolls a region only far enough to show the focused
 * element, so a heading, a counts line or a column header sitting over the
 * topmost row stays off the top once the reader has scrolled past it. That is
 * this component's own job, and it is here rather than on a page so that every
 * wide page inherits it — see `revealTop`.
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
const BOUNDS: CSSProperties = { height: "100%", minHeight: 0 };

// Every shape Steam gives a focus stop inside a region, measured in the running
// QAM rather than assumed: its own components render `div[tabindex="0"]`
// (`Focusable`, a toggle row, a table row carrying an activate handler) and a
// `DialogButton` is a native `button` with no tabindex attribute at all. A
// container `Focusable` can carry `tabindex="0"` too, which is why the rule
// below excludes the focused element's own ancestors rather than comparing
// against the first match.
const FOCUS_STOPS = "[tabindex], button, a[href], input, select, textarea";

// Long enough to land after Steam's own focus scroll, which would otherwise
// undo this one. The same 50 ms every helper in `utils/scrollHelpers.ts` uses,
// for the same reason.
const AFTER_STEAMS_OWN_SCROLL_MS = 50;

/**
 * Reveal the region's top once focus reaches the first thing in it that can
 * hold focus.
 *
 * The rule is "nothing focusable is above me", not "I am the first match":
 * a container `Focusable` renders `tabindex="0"` of its own and precedes the
 * row inside it in document order, so an equality test would silently never
 * fire wherever a page wraps its rows. An ancestor of the focused element is
 * therefore not something above it.
 *
 * **It cannot fight Steam's own scrolling, because it never moves the focused
 * element out of view**: the scroll happens only when that element still fits
 * within the region at offset zero. Where the content above the first row is
 * taller than the region itself there is no offset that shows both, and Steam
 * would immediately scroll the element back — so nothing is done at all and the
 * reader keeps the behaviour they had.
 */
function revealTop(event: FocusEvent<HTMLElement>): void {
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
  if (reached < 0 || stops.slice(0, reached).some((stop) => !stop.contains(focused))) return;

  if (offsetWithinScroller(focused, region) + focused.getBoundingClientRect().height > region.clientHeight) return;
  setTimeout(() => region.scrollTo({ top: 0, behavior: "smooth" }), AFTER_STEAMS_OWN_SCROLL_MS);
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
    <ScrollPanel style={{ ...BOUNDS, ...style }} onFocus={revealTop}>
      {children}
    </ScrollPanel>
  ) : (
    <Focusable style={{ ...BOUNDS, overflow: "auto", ...style }} onFocus={revealTop}>
      {children}
    </Focusable>
  );
