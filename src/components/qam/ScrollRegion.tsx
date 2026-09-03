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
 * The panel comes from a webpack probe that can miss, and a page whose regions
 * silently vanish would be worse than one that scrolls without what the panel
 * adds on top — so the fallback keeps the region, its bounds and its place in
 * the focus tree, and gives up Steam's scroll padding, its own focus-ring root
 * and the ref that scrolls the focused element back into view after a resize.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import type { CSSProperties, FC, ReactNode } from "react";
import { Focusable } from "@decky/ui";
import { ScrollPanel } from "../../utils/deckyUiInternals";

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

export const ScrollRegion: FC<ScrollRegionProps> = ({ style, children }) =>
  // Focusable, not a div, when the probe misses: it is the very base panel
  // Steam's scroll panel renders, so the region keeps the same place in the
  // focus tree and takes focus no more than the panel does. That branch has no
  // Steam class behind it, which makes it the one place the overflow is ours.
  ScrollPanel ? (
    <ScrollPanel style={{ ...BOUNDS, ...style }}>{children}</ScrollPanel>
  ) : (
    <Focusable style={{ ...BOUNDS, overflow: "auto", ...style }}>{children}</Focusable>
  );
