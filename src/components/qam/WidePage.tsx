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
import { offsetWithinScroller } from "../../utils/scrollHelpers";
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
//
// **Not a knob for absorbing leftover scroll.** With a correctly measured body
// the panel still reports ~38 px of overflow on the reference machine, and the
// arithmetic localises it: the body is `clientHeight - offset - GAP`, so a
// panel whose content runs to `clientHeight - GAP + T` puts T at about 50 px —
// a box below the plugin's root, inside Steam's own, since the panel's padding
// measures 0 top and bottom and no child of ours accounts for it. Growing this
// to swallow T would pin every wide page's height to a box we do not render,
// cannot watch change, and would not notice moving; the wheel is contained at
// the region instead (`ScrollRegion`). Learning what T is takes one read of
// `panel.scrollHeight - (root.offsetTop + root.offsetHeight)`.
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
 * **The quantity has to be free of the scroller's own offset, and only a
 * layout-relative one is.** Every viewport-relative form grows as the panel
 * scrolls, which makes the measurement feed itself: a body measured part-way
 * down comes out that much too tall, the panel then has that much more to
 * scroll, and nothing re-measures. It can be measured part-way down because
 * `QAMPanel` resets the panel's scroll inside a `requestAnimationFrame`, a
 * frame after this page's layout effect has already run.
 *
 * Measured live in the QAM over the mounted page, at panel offsets 0 / 200 /
 * 500 / 634 px: `innerHeight - top` answers 648 / 848 / 1148 / 1283, and so
 * does the scroller's own `bottom - top` — the panel's rect bottom is 764.3
 * against an `innerHeight` of 764, so bounding to the panel rather than the
 * window changes no number at any offset. This form answers 648 at all four.
 *
 * Both branches assume the scroller is an element other than the viewport's own.
 * The document element would break the first — it IS the viewport's scroller, so
 * its rect moves under its own scroll and the difference is already offset-free,
 * making `+ scrollTop` a double count. And the `null` branch is reached when no
 * ancestor computes `overflow-y: auto|scroll`, which is not the same as nothing
 * scrolling: a document scrolling at the viewport level is exactly that case,
 * and there the fallback is the self-amplifying form again. Neither is reachable
 * from the QAM, where the panel is an ordinary element and Steam's own document
 * does not scroll.
 */
function remainingBodyHeight(body: HTMLElement, view: Window): number {
  const scroller = scrollingAncestor(body, view);
  const remaining = scroller
    ? scroller.clientHeight - offsetWithinScroller(body, scroller)
    : view.innerHeight - body.getBoundingClientRect().top;
  return Math.max(MIN_BODY_HEIGHT, remaining - BODY_BOTTOM_GAP);
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
    // The view's own constructor, not the module's: plugin code runs in the
    // SharedJSContext window and these nodes are the QAM's.
    const observer = new view.ResizeObserver(measure);
    observer.observe(body.ownerDocument.documentElement);
    // And the panel itself, which is what actually changes size here.
    //
    // **The first measurement provably runs before the panel is widened**, and
    // that follows from the effect kinds rather than from timing: this is a
    // LAYOUT effect, while `useWideQamPanel` widens the panel from a passive
    // one, and React runs every layout effect of a commit before any passive
    // effect of it. So on every mount the page is measured against the panel
    // Steam has not expanded yet. The device figure is the other half — a body
    // of 1245 px where the settled geometry answers 694 implies a mount-time
    // `clientHeight` near 1257, not 750 — so the panel's box really does differ
    // between mount and settle, and nothing else would have corrected it: the
    // document element never resizes, because the viewport does not change.
    //
    // Only the TRIGGER is pinned to the mount-time scroller; the answer
    // re-resolves it on every `measure()`.
    const scroller = scrollingAncestor(body, view);
    if (scroller) observer.observe(scroller);
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
