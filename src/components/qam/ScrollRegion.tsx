/**
 * One scrolling region of a wide page, bounded by the height its parent gives it.
 *
 * Steam's gamepad navigation scrolls by moving focus, so a plain `overflow: auto`
 * box holding content nobody can focus — a detail pane of text rows — cannot be
 * scrolled with the controller at all, only with a mouse. `ScrollPanelGroup`
 * closes that by binding gamepad direction to scrolling. Its plain sibling
 * `ScrollPanel` does not, which is why reaching for the panel Steam's own QAM tab
 * uses would not have fixed anything: that one is the plain kind.
 *
 * The panel comes from a webpack probe that can miss, and a page whose regions
 * silently vanish would be worse than one that scrolls only under a mouse — so
 * the fallback keeps the region, its bounds and its place in the focus tree.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import type { CSSProperties, FC, ReactNode } from "react";
import { Focusable } from "@decky/ui";
import { ScrollPanelGroup } from "../../utils/deckyUiInternals";

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
// class carries `overflow-y: auto` with `overflow-x: hidden`
// (`._29WypCpglgRKsR_fMPsoFX`, `css/chunk~2dcc5aaf7.css`), and an inline
// `overflow` shorthand beats both. Restoring horizontal room is not cosmetic: the
// gamepad-direction handler this panel binds consumes left and right whenever
// there is room to scroll sideways, and left↔right is how a reader crosses from
// the list to the detail — one over-wide row would strand focus in the list.
const BOUNDS: CSSProperties = { height: "100%", minHeight: 0 };

export const ScrollRegion: FC<ScrollRegionProps> = ({ style, children }) =>
  // Focusable, not a div, when the probe misses: the region is a level of the
  // focus tree either way, and a plain div would silently restructure gamepad
  // navigation around it. That branch has no Steam class behind it, which makes
  // it the one place the overflow is ours to set.
  ScrollPanelGroup ? (
    <ScrollPanelGroup style={{ ...BOUNDS, ...style }}>{children}</ScrollPanelGroup>
  ) : (
    <Focusable style={{ ...BOUNDS, overflow: "auto", ...style }}>{children}</Focusable>
  );
