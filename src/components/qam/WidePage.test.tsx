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
import type { WidePageProps, WidePageTab } from "./WidePage";

interface StubTabsProps {
  tabs: WidePageTab[];
  activeTab: string;
  onShowTab: (tabId: string) => void;
}

/** Stand-in for Steam's tabbed page: a bar of titles plus the active content. */
const StubTabs: FC<StubTabsProps> = ({ tabs, activeTab, onShowTab }) => (
  <div data-testid="steam-tabs">
    {tabs.map((tab) => (
      <button key={tab.id} onClick={() => onShowTab(tab.id)}>
        {tab.title}
      </button>
    ))}
    <div>{tabs.find((tab) => tab.id === activeTab)?.content}</div>
  </div>
);

async function loadWidePage(tabs: FC<StubTabsProps> | undefined): Promise<FC<WidePageProps>> {
  vi.resetModules();
  vi.doMock("../../utils/deckyUiInternals", () => ({ quickAccessMenuClasses: undefined, Tabs: tabs }));
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
