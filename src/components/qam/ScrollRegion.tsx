/**
 * One scrolling region of a wide page, bounded by the height its parent gives it.
 *
 * Steam's gamepad navigation scrolls by moving focus, so a plain `overflow: auto`
 * box holding content nobody can focus — a detail pane of text rows — cannot be
 * scrolled with the controller at all, only with a mouse. Steam's scroll panel is
 * what closes that: it binds gamepad direction to scrolling, which is why the
 * QAM's own tab panel scrolls such content and a bare div does not.
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
const BOUNDS: CSSProperties = { height: "100%", minHeight: 0, overflow: "auto" };

export const ScrollRegion: FC<ScrollRegionProps> = ({ style, children }) => {
  const regionStyle = { ...BOUNDS, ...style };

  // Focusable, not a div, when the probe misses: the region is a level of the
  // focus tree either way, and a plain div would silently restructure gamepad
  // navigation around it.
  return ScrollPanelGroup ? (
    <ScrollPanelGroup style={regionStyle}>{children}</ScrollPanelGroup>
  ) : (
    <Focusable style={regionStyle}>{children}</Focusable>
  );
};
