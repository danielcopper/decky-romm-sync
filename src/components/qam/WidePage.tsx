/**
 * The shell every wide QAM page renders inside: the marker class that lets the
 * injected rule lift Steam's 300 px tab-panel cap, the Back row and title, an
 * optional L1/R1 tab bar, and a body of a definite, measured height.
 *
 * The page's own content is `children`, or — when the page has tabs — the
 * `content` of each tab, which Steam's `Tabs` renders itself.
 *
 * Structure and vocabulary: `docs/architecture/qam-panel.md`.
 */

import { useLayoutEffect, useRef, useState, type FC, type ReactNode } from "react";
import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
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

  // Only the untabbed body gets a scroll region of its own. Steam's tabbed page
  // brings one per tab, and the branch below it is the probe-missed fallback,
  // which renders the tab's content exactly as Steam's own page would have.
  let body: ReactNode = <ScrollRegion>{children}</ScrollRegion>;
  if (tabs) {
    body = Tabs ? (
      <Tabs tabs={tabs} activeTab={activeTab} onShowTab={onShowTab} />
    ) : (
      tabs.find((tab) => tab.id === activeTab)?.content
    );
  }

  return (
    <div className={WIDE_ROOT_CLASS} ref={rootRef}>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <div style={{ padding: "0 16px 8px", fontSize: "16px", fontWeight: 600, color: "#dcdedf" }}>{title}</div>
      <div ref={bodyRef} style={bodyStyle} data-testid="wide-page-body">
        {body}
      </div>
    </div>
  );
};
