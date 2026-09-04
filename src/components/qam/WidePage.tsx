/**
 * The shell every wide QAM page renders inside: the marker class that lets the
 * injected rule lift Steam's 300 px tab-panel cap, one header line carrying Back
 * and the title, an optional L1/R1 tab bar, and a body of a definite, measured
 * height.
 *
 * Back on the gamepad is **B**, bound once by the panel's router for every
 * sub-page (`src/index.tsx`) rather than here — so the narrow pages get it too,
 * and Main, which has nowhere to go back to, keeps Decky's own B. What the frame
 * has to do is get out of the way: Steam's tabbed page binds the content pane's
 * `onCancelButton` to "focus the tab row" unless `cancelSkipTabHeader` is
 * passed, so the first B inside a tab would be swallowed there. The chip stays
 * as the discoverable half and as the mouse path.
 *
 * The page's own content is `children`, or — when the page has tabs — the
 * `content` of each tab, which Steam's `Tabs` renders itself. A tabbed page
 * also opens with focus in that content rather than on the Back row.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import { useLayoutEffect, useRef, useState, type FC, type ReactNode } from "react";
import { DialogButton, Focusable } from "@decky/ui";
import { ControllerGlyph, GLYPH_BUTTON_B, Tabs } from "../../utils/deckyUiInternals";
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
 * What the Back chip reads: the button's own glyph and the word.
 *
 * The glyph is Steam's, drawn for whatever controller is connected, so the chip
 * names the same button the footer legend does rather than a letter this plugin
 * picked. Where the probe misses, the chevron the chip carried before it takes
 * over — a chip that says Back and no glyph is still true, and a hand-drawn B
 * would be wrong for a PlayStation pad.
 */
const BackChipLabel: FC = () =>
  ControllerGlyph ? (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
      <ControllerGlyph button={GLYPH_BUTTON_B} bKnockout style={{ width: "13px", height: "13px" }} />
      Back
    </span>
  ) : (
    <>‹ Back</>
  );

/**
 * The nearest ancestor that scrolls `body`, or `null` when nothing does.
 *
 * The QAM's own tab panel is one (`#quickaccess_content_999` computes
 * `overflow-y: auto`), and it is the element whose bottom edge bounds the page.
 */
function scrollingAncestor(body: HTMLElement, view: Window): HTMLElement | null {
  for (let el = body.parentElement; el; el = el.parentElement) {
    const overflowY = view.getComputedStyle(el).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return el;
  }
  return null;
}

/**
 * The space left below `body` inside whatever scrolls it — the height a wide
 * page gets to work with, because Steam's tabbed page fills its parent rather
 * than growing and nothing in the QAM chain hands the plugin's panel a height.
 *
 * **Both ends are viewport-relative, so their difference does not move when
 * anything scrolls.** `innerHeight - top` is not the same quantity: it equals
 * the space below only at scroll offset zero, and it grows as the page scrolls
 * — which made the measurement feed itself. A body measured a few hundred
 * pixels down the panel came out that much too tall, the panel then scrolled
 * because the body no longer fit, and the next measurement read a negative
 * `top` and grew it again. Measured on the device at 1245px of body inside a
 * 750px panel, with the tab row pushed off the top; the same DOM at scroll
 * offset zero answers 648px both ways.
 *
 * With nothing scrolling above it there is no offset to be wrong about, so the
 * viewport is the honest bound in that case.
 */
function remainingBodyHeight(body: HTMLElement, view: Window): number {
  const scroller = scrollingAncestor(body, view);
  const bottom = scroller ? scroller.getBoundingClientRect().bottom : view.innerHeight;
  return Math.max(MIN_BODY_HEIGHT, bottom - body.getBoundingClientRect().top - BODY_BOTTOM_GAP);
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

    const measure = () => setBodyHeight(remainingBodyHeight(body, view));
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
      // `cancelSkipTabHeader` leaves the content pane's cancel handler unset, so
      // B reaches the router's binding instead of being spent moving focus to
      // the tab row.
      <Tabs tabs={tabs} activeTab={activeTab} onShowTab={onShowTab} autoFocusContents cancelSkipTabHeader />
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
      <Focusable style={{ display: "flex", alignItems: "center", gap: "10px", padding: "4px 16px 6px" }}>
        <DialogButton
          style={{ flex: "0 0 auto", minWidth: 0, width: "auto", padding: "4px 10px", fontSize: "13px" }}
          onClick={onBack}
        >
          <BackChipLabel />
        </DialogButton>
        <span style={{ fontSize: "16px", fontWeight: 600, color: "#dcdedf" }}>{title}</span>
      </Focusable>
      <div ref={bodyRef} style={bodyStyle} data-testid="wide-page-body">
        {body}
      </div>
    </div>
  );
};
