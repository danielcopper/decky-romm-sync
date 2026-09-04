/**
 * The shell every wide QAM page renders inside: the marker class that lets the
 * injected rule lift Steam's 300 px tab-panel cap, one header line carrying Back
 * and the title, an optional L1/R1 tab bar, and a body of a definite, measured
 * height.
 *
 * Back is on **Y** as well as on the chip, through `FooterLegendProps` on the
 * frame's own `Focusable` — so Steam draws it in the footer legend beside its
 * own A/B entries and it works wherever focus is. B is deliberately untouched:
 * that is Decky's back, and it leaves the plugin altogether. The chip stays
 * because it is the discoverable half — the legend says the shortcut exists, the
 * chip says the page has a way out at all.
 *
 * The page's own content is `children`, or — when the page has tabs — the
 * `content` of each tab, which Steam's `Tabs` renders itself. A tabbed page
 * also opens with focus in that content rather than on the Back row.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import { useLayoutEffect, useRef, useState, type FC, type ReactNode } from "react";
import { DialogButton, Focusable } from "@decky/ui";
import { Tabs } from "../../utils/deckyUiInternals";
import { WIDE_ROOT_CLASS, useWideQamPanel } from "../../utils/qamExpansion";
import { ScrollRegion } from "./ScrollRegion";

export interface WidePageTab {
  id: string;
  title: string;
  content: ReactNode;
}

/**
 * A page has all three tab props or none of them, and `children` belongs to the
 * second case alone: a tabbed page's body is the active tab's `content`, so
 * children passed beside `tabs` would render nowhere.
 */
type TabProps =
  | { tabs: WidePageTab[]; activeTab: string; onShowTab: (tabId: string) => void; children?: undefined }
  | { tabs?: undefined; activeTab?: undefined; onShowTab?: undefined; children?: ReactNode };

export type WidePageProps = {
  title: string;
  onBack: () => void;
} & TabProps;

/**
 * Marks a page that places its own entry focus, which the panel's router reads
 * to keep its first-button focus away. A tabbed page sets it, because the
 * `autoFocusContents` it hands Steam's tabbed page is what puts focus in the
 * content; a page that cannot place focus itself never sets it, so the router
 * still covers it.
 */
export const OWNS_ENTRY_FOCUS_ATTR = "data-romm-owns-entry-focus";

// Floor under the measured body, so a viewport read taken before Steam has laid
// the panel out cannot collapse the page to nothing.
const MIN_BODY_HEIGHT = 240;

// Breathing room under the body, kept off the measurement so the page never
// ends flush against the panel's bottom edge.
const BODY_BOTTOM_GAP = 12;

/**
 * The remaining viewport below `body`, which is the height a wide page gets to
 * work with: Steam's tabbed page fills its parent rather than growing, and
 * nothing in the QAM chain hands the plugin's panel a height.
 */
function remainingViewportHeight(body: HTMLElement, view: Window): number {
  return Math.max(MIN_BODY_HEIGHT, view.innerHeight - body.getBoundingClientRect().top - BODY_BOTTOM_GAP);
}

export const WidePage: FC<WidePageProps> = ({ title, onBack, tabs, activeTab, onShowTab, children }) => {
  const rootRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const [bodyHeight, setBodyHeight] = useState<number | null>(null);

  useWideQamPanel(rootRef);

  useLayoutEffect(() => {
    const body = bodyRef.current;
    const view = body?.ownerDocument.defaultView;
    if (!body || !view) return;

    const measure = () => setBodyHeight(remainingViewportHeight(body, view));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(body.ownerDocument.documentElement);
    return () => observer.disconnect();
  }, []);

  // A `min-height` is not enough here — Steam's tabbed page fills its parent and
  // clips instead of growing, so the body needs a definite height.
  const bodyStyle = { height: bodyHeight === null ? undefined : `${bodyHeight}px`, overflow: "hidden" };

  // Only the untabbed body gets a region from the frame. Steam's tabbed page
  // already wraps each tab's content in this same plain scroll panel
  // (`ScrollingTab<id>`, `scrollDirection: "y"`, in `chunk~2dcc5aaf7.js`), so
  // one from the frame would nest a second scroller inside it. A tab that needs
  // more than that one — a list and a detail scrolling side by side — builds its
  // regions with `ScrollRegion` itself.
  let body: ReactNode = <ScrollRegion>{children}</ScrollRegion>;
  if (tabs) {
    // `autoFocusContents` lands entry focus in the active tab's content — the
    // list of a list-and-detail page — which is also what makes Steam draw the
    // L1/R1 glyphs: its tab row shows them only while gamepad focus is within
    // the tabbed page, and the Back row above the tabs is outside it
    // (`chunk~2dcc5aaf7.js`, the tab row's `showGlyphs`).
    body = Tabs ? (
      <Tabs tabs={tabs} activeTab={activeTab} onShowTab={onShowTab} autoFocusContents />
    ) : (
      tabs.find((tab) => tab.id === activeTab)?.content
    );
  }

  // Claimed only where the claim is true: without Steam's tabbed page nothing
  // here places focus, so the router's own must still run.
  const ownsEntryFocus = Boolean(tabs && Tabs);

  return (
    <div className={WIDE_ROOT_CLASS} ref={rootRef} {...(ownsEntryFocus ? { [OWNS_ENTRY_FOCUS_ATTR]: "" } : {})}>
      {/* One line, not three: the full-width Back row and the title on its own
          line cost two of the four rows the Deck's body has to spend. */}
      <Focusable
        style={{ display: "flex", alignItems: "center", gap: "10px", padding: "4px 16px 6px" }}
        onSecondaryButton={onBack}
        onSecondaryActionDescription="Back"
      >
        <DialogButton
          style={{ flex: "0 0 auto", minWidth: 0, width: "auto", padding: "4px 10px", fontSize: "13px" }}
          onClick={onBack}
        >
          ‹ Back
        </DialogButton>
        <span style={{ fontSize: "16px", fontWeight: 600, color: "#dcdedf" }}>{title}</span>
      </Focusable>
      <div ref={bodyRef} style={bodyStyle} data-testid="wide-page-body">
        {body}
      </div>
    </div>
  );
};
