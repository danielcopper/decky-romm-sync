/**
 * WidePage tests — the frame's own contract: the Back row, the title, a body
 * with a definite measured height, a tab bar that survives Steam's `Tabs` probe
 * coming back undefined, and the two ends of the width the frame is for.
 *
 * `Tabs` is read at module scope, so each test loads a fresh copy of the
 * component through `loadWidePage` with the probe answer it wants. What the
 * width levers themselves do once engaged is pinned in
 * `src/utils/qamExpansion.test.tsx`, against its own fake page; what is pinned
 * here is that this frame is what engages them.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { FC, ReactNode } from "react";
import { WIDE_ROOT_CLASS } from "../../utils/qamExpansion";
import { OWNS_ENTRY_FOCUS_ATTR, type WidePageProps, type WidePageTab } from "./WidePage";

interface StubTabsProps {
  tabs: WidePageTab[];
  activeTab: string;
  onShowTab: (tabId: string) => void;
  autoFocusContents?: boolean;
}

/**
 * Stand-in for Steam's tabbed page: a bar of titles plus the active content.
 * `autoFocusContents` is surfaced as an attribute — the real page consumes it to
 * place focus, which happy-dom's gamepad-free DOM cannot show happening.
 */
const StubTabs: FC<StubTabsProps> = ({ tabs, activeTab, onShowTab, autoFocusContents }) => (
  <div data-testid="steam-tabs" data-auto-focus-contents={String(Boolean(autoFocusContents))}>
    {tabs.map((tab) => (
      <button key={tab.id} onClick={() => onShowTab(tab.id)}>
        {tab.title}
      </button>
    ))}
    <div>{tabs.find((tab) => tab.id === activeTab)?.content}</div>
  </div>
);

/** Stand-in for Steam's scroll panel, which the frame reaches via `ScrollRegion`. */
const StubScrollPanelGroup: FC<{ children?: ReactNode }> = ({ children }) => (
  <div data-testid="scroll-panel">{children}</div>
);

async function loadWidePage(
  tabs: FC<StubTabsProps> | undefined,
  scrollPanelGroup?: FC<{ children?: ReactNode }>,
): Promise<FC<WidePageProps>> {
  vi.resetModules();
  // Every export the graph under test imports has to be named here — Vitest
  // throws on an import of a name the factory left out, which is how the scroll
  // panel reaches this file at all: `ScrollRegion` asks for it, `WidePage` does not.
  vi.doMock("../../utils/deckyUiInternals", () => ({
    quickAccessMenuClasses: undefined,
    Tabs: tabs,
    ScrollPanelGroup: scrollPanelGroup,
  }));
  return (await import("./WidePage")).WidePage;
}

const TAB_SET: WidePageTab[] = [
  { id: "platforms", title: "Platforms", content: <div>platform detail</div> },
  { id: "collections", title: "Collections", content: <div>collection detail</div> },
];

function body(): HTMLElement {
  return screen.getByTestId("wide-page-body");
}

describe("WidePage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the Back row, the title and the page body", async () => {
    const WidePage = await loadWidePage(StubTabs);
    const onBack = vi.fn();

    render(
      <WidePage title="Settings" onBack={onBack}>
        <div>page body</div>
      </WidePage>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    expect(screen.getByText("Settings")).toBeInTheDocument();
    expect(screen.getByText("page body")).toBeInTheDocument();
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("gives the body a definite height taken from the remaining viewport", async () => {
    const WidePage = await loadWidePage(StubTabs);

    render(
      <WidePage title="Settings" onBack={vi.fn()}>
        <div>page body</div>
      </WidePage>,
    );

    // happy-dom reports every rect at the origin, so the body's top is 0 and the
    // measurement is the viewport minus the frame's bottom gap.
    expect(window.innerHeight).toBeGreaterThan(240);
    expect(body().style.height).toBe(`${window.innerHeight - 12}px`);
    // Steam's tabbed page fills its parent instead of growing: a min-height
    // leaves the body with no height at all and the page clips.
    expect(body().style.minHeight).toBe("");
  });

  it("never measures the body below its floor", async () => {
    // Undone by the `vi.unstubAllGlobals()` in src/test-setup.ts's global
    // afterEach, so the viewport is back to its default for the next test.
    vi.stubGlobal("innerHeight", 100);
    const WidePage = await loadWidePage(StubTabs);

    render(
      <WidePage title="Settings" onBack={vi.fn()}>
        <div>page body</div>
      </WidePage>,
    );

    expect(body().style.height).toBe("240px");
  });

  it("renders tabs through Steam's tabbed page and reports a switch", async () => {
    const WidePage = await loadWidePage(StubTabs);
    const onShowTab = vi.fn();

    render(<WidePage title="Library" onBack={vi.fn()} tabs={TAB_SET} activeTab="platforms" onShowTab={onShowTab} />);
    fireEvent.click(screen.getByRole("button", { name: "Collections" }));

    expect(screen.getByTestId("steam-tabs")).toBeInTheDocument();
    expect(screen.getByText("platform detail")).toBeInTheDocument();
    expect(onShowTab).toHaveBeenCalledWith("collections");
  });

  it("renders the active tab's content without a bar when the Tabs probe missed", async () => {
    const WidePage = await loadWidePage(undefined);

    render(<WidePage title="Library" onBack={vi.fn()} tabs={TAB_SET} activeTab="collections" onShowTab={vi.fn()} />);

    expect(screen.queryByTestId("steam-tabs")).not.toBeInTheDocument();
    expect(screen.getByText("collection detail")).toBeInTheDocument();
    expect(screen.queryByText("platform detail")).not.toBeInTheDocument();
  });

  it("marks its root with the class the injected width rule matches", async () => {
    const WidePage = await loadWidePage(StubTabs);

    const { container } = render(
      <WidePage title="Settings" onBack={vi.fn()}>
        <div>page body</div>
      </WidePage>,
    );

    // The injected `:has()` rule keys on this class: drop it and every wide page
    // renders 300 px wide inside an 854 px panel, with nothing raised anywhere.
    // It sits on the root because that is the element the width hook is handed —
    // one decision, which this pins as one.
    expect(container.firstElementChild).toHaveClass(WIDE_ROOT_CLASS);
  });

  it("engages the panel's width levers while it is mounted", async () => {
    const WidePage = await loadWidePage(StubTabs);
    const post = vi.spyOn(window, "postMessage").mockImplementation(() => {});

    render(
      <WidePage title="Settings" onBack={vi.fn()}>
        <div>page body</div>
      </WidePage>,
    );

    // The frame holding `useWideQamPanel` is the whole reason a page is wide.
    // Dropping the hook while the module stays imported elsewhere leaves the
    // rest of this file green.
    expect(post).toHaveBeenCalledWith({ message: "QamFriendsExpanded" }, window.origin);
  });

  it("gives an untabbed body a scroll panel of its own", async () => {
    const WidePage = await loadWidePage(StubTabs, StubScrollPanelGroup);

    render(
      <WidePage title="Settings" onBack={vi.fn()}>
        <div>page body</div>
      </WidePage>,
    );

    expect(screen.getByTestId("scroll-panel")).toContainElement(screen.getByText("page body"));
  });

  it("leaves a tabbed body's scrolling to Steam's tabbed page", async () => {
    const WidePage = await loadWidePage(StubTabs, StubScrollPanelGroup);

    render(<WidePage title="Library" onBack={vi.fn()} tabs={TAB_SET} activeTab="platforms" onShowTab={vi.fn()} />);

    // Steam's tabbed page brings a scroll panel per tab. A second one wrapped
    // around it would nest two scrollers on the same content.
    expect(screen.queryByTestId("scroll-panel")).not.toBeInTheDocument();
    expect(screen.getByTestId("steam-tabs")).toBeInTheDocument();
  });

  it("has Steam's tabbed page place entry focus, and says so on its root", async () => {
    const WidePage = await loadWidePage(StubTabs);

    const { container } = render(
      <WidePage title="Library" onBack={vi.fn()} tabs={TAB_SET} activeTab="platforms" onShowTab={vi.fn()} />,
    );

    // Focus in the content is also what draws the L1/R1 glyphs: Steam's tab row
    // shows them only while focus is within the tabbed page, and the Back row
    // is above the tabs. The marker is what keeps the router's own focus —
    // which would land on that Back row — away from the page.
    expect(screen.getByTestId("steam-tabs")).toHaveAttribute("data-auto-focus-contents", "true");
    expect(container.firstElementChild).toHaveAttribute(OWNS_ENTRY_FOCUS_ATTR);
  });

  it("claims no entry focus without tabs, where nothing here places any", async () => {
    const WidePage = await loadWidePage(StubTabs);

    const { container } = render(
      <WidePage title="Settings" onBack={vi.fn()}>
        <div>page body</div>
      </WidePage>,
    );

    expect(container.firstElementChild).not.toHaveAttribute(OWNS_ENTRY_FOCUS_ATTR);
  });

  it("claims no entry focus when the Tabs probe missed, so the router still covers the page", async () => {
    const WidePage = await loadWidePage(undefined);

    const { container } = render(
      <WidePage title="Library" onBack={vi.fn()} tabs={TAB_SET} activeTab="platforms" onShowTab={vi.fn()} />,
    );

    // The fallback renders the tab's content raw, with nothing to autofocus. A
    // marker here would opt the page out of the only focus it would get.
    expect(container.firstElementChild).not.toHaveAttribute(OWNS_ENTRY_FOCUS_ATTR);
  });

  it("takes no children beside tabs, whose body is the active tab's content", async () => {
    const WidePage = await loadWidePage(StubTabs);

    render(
      // @ts-expect-error `children` sits in the no-tabs branch of WidePageProps: passed here it would render nowhere.
      <WidePage title="Library" onBack={vi.fn()} tabs={TAB_SET} activeTab="platforms" onShowTab={vi.fn()}>
        <div>dropped</div>
      </WidePage>,
    );

    expect(screen.getByText("platform detail")).toBeInTheDocument();
    expect(screen.queryByText("dropped")).not.toBeInTheDocument();
  });

  it("still renders the frame when the active tab has no content", async () => {
    const WidePage = await loadWidePage(undefined);
    const emptyTabs: WidePageTab[] = [{ id: "platforms", title: "Platforms", content: null as ReactNode }];

    render(<WidePage title="Library" onBack={vi.fn()} tabs={emptyTabs} activeTab="missing" onShowTab={vi.fn()} />);

    expect(screen.getByText("Library")).toBeInTheDocument();
    expect(body()).toBeEmptyDOMElement();
  });
});
