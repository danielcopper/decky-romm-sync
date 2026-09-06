/**
 * ScrollRegion tests — that the region reaches Steam's scroll panel when the
 * probe found one, that it still is a bounded region of the focus tree when the
 * probe missed, and that a caller can place it without losing its bounds.
 *
 * What the panel then does with focus is Steam's: it takes none of its own, and
 * the focused row is what scrolls into view. happy-dom has neither the panel nor
 * a gamepad, so nothing here can pin that the region is not a focus stop — the
 * device round is what pins it.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import type { CSSProperties, FC, FocusEventHandler, ReactNode } from "react";
import type { ScrollRegionProps } from "./ScrollRegion";

interface StubPanelProps {
  style?: CSSProperties;
  children?: ReactNode;
  onFocus?: FocusEventHandler<HTMLDivElement>;
}

/** Stand-in for Steam's scroll panel: it keeps the style so bounds stay
 *  assertable, and forwards the rest the way the real panel does — it spreads
 *  whatever it does not destructure onto the element it renders. */
const StubScrollPanel: FC<StubPanelProps> = ({ style, children, ...rest }) => (
  <div data-testid="scroll-panel" style={style} {...rest}>
    {children}
  </div>
);

/**
 * A fresh copy of the component with `panel` as the webpack probe's answer.
 * The probe is read at module scope, so both outcomes need their own load.
 */
async function loadScrollRegion(panel: FC<StubPanelProps> | undefined): Promise<FC<ScrollRegionProps>> {
  vi.resetModules();
  vi.doMock("../../utils/deckyUiInternals", () => ({ ScrollPanel: panel }));
  return (await import("./ScrollRegion")).ScrollRegion;
}

/** The bounds every region carries, whichever element ends up carrying them. */
function expectBounds(el: HTMLElement): void {
  expect(el.style.height).toBe("100%");
  // Without the floor reset a flex child's min-height is its content, so the
  // region would grow past its parent instead of scrolling inside it. happy-dom
  // keeps the "0" verbatim where a browser would normalise it to "0px".
  expect(el.style.minHeight).toBe("0");
}

describe("ScrollRegion", () => {
  it("puts its content in Steam's scroll panel when the probe found one", async () => {
    const ScrollRegion = await loadScrollRegion(StubScrollPanel);

    render(
      <ScrollRegion>
        <div>region content</div>
      </ScrollRegion>,
    );

    // The panel is the container the QAM's own tab panel is built from: rows
    // inside it take focus directly and the focused one is scrolled into view.
    const panel = screen.getByTestId("scroll-panel");
    expect(panel).toContainElement(screen.getByText("region content"));
    expectBounds(panel);
  });

  it("leaves the panel's overflow to Steam, which clips it sideways", async () => {
    const ScrollRegion = await loadScrollRegion(StubScrollPanel);

    render(
      <ScrollRegion>
        <div>region content</div>
      </ScrollRegion>,
    );

    // Steam's ScrollY class is `overflow-y: auto` with `overflow-x: hidden`, and
    // an inline shorthand beats both axes. The sideways scroll it would restore
    // is one Steam clips on purpose: one over-wide row and every focus step
    // would drag the pane left and right under the reader.
    expect(screen.getByTestId("scroll-panel").style.overflow).toBe("");
  });

  it("keeps the wheel inside a region that has reached its end", async () => {
    const ScrollRegion = await loadScrollRegion(StubScrollPanel);

    render(
      <ScrollRegion>
        <div>region content</div>
      </ScrollRegion>,
    );

    // Measured on device: all three nested scrollers compute `auto`, so a mouse
    // at the bottom of one went on to scroll Steam's tab panel and took the
    // frame's Back row off the top. A controller never showed it — Steam scrolls
    // a region by moving focus, not by wheel events.
    expect(screen.getByTestId("scroll-panel").style.overscrollBehavior).toBe("contain");
  });

  it("keeps the region, its bounds and its place in the focus tree when the probe missed", async () => {
    const ScrollRegion = await loadScrollRegion(undefined);

    render(
      <ScrollRegion>
        <div>region content</div>
      </ScrollRegion>,
    );

    expect(screen.queryByTestId("scroll-panel")).not.toBeInTheDocument();
    // A Focusable rather than a div: it is the base panel Steam's scroll panel
    // itself renders, so the region stays one level of the focus tree. What a
    // missed probe costs is what the panel adds on top — its scroll padding, its
    // own focus-ring root, and the ref that scrolls the focused element back
    // into view after a resize — not the structure.
    const region = screen.getByTestId("focusable");
    expect(region).toContainElement(screen.getByText("region content"));
    expectBounds(region);
    // Nothing here carries a Steam class, so this branch owns its own overflow.
    expect(region.style.overflow).toBe("auto");
  });

  it("lets the caller place the region without losing its bounds", async () => {
    const ScrollRegion = await loadScrollRegion(StubScrollPanel);

    render(<ScrollRegion style={{ flex: "0 0 264px", width: "264px" }} />);

    const panel = screen.getByTestId("scroll-panel");
    expect(panel.style.flex).toBe("0 0 264px");
    expect(panel.style.width).toBe("264px");
    expectBounds(panel);
  });

  /**
   * Revealing the region's two ends.
   *
   * happy-dom lays nothing out and has no gamepad, so the geometry every branch
   * reads is mocked and what is pinned is the DECISION, not the scroll a reader
   * would see. That the panel and the reader agree about which element is
   * topmost or last, and that the scroll looks right on a controller, is the
   * device round's to settle.
   */
  describe("revealing what sits outside the first and last focusable rows", () => {
    /** A region 500 tall over `scrollHeight` of content, each stop placed by data. */
    function mockGeometry(scrollHeight: number): void {
      vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(scrollHeight);
      vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(500);
      vi.spyOn(HTMLElement.prototype, "clientTop", "get").mockReturnValue(0);
      vi.spyOn(Element.prototype, "scrollTop", "get").mockReturnValue(0);
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
        return { top: Number(this.dataset.top ?? 0), height: Number(this.dataset.h ?? 20) } as DOMRect;
      });
    }

    async function renderRegion(children: ReactNode, scrollHeight = 1200) {
      const ScrollRegion = await loadScrollRegion(StubScrollPanel);
      mockGeometry(scrollHeight);
      render(<ScrollRegion>{children}</ScrollRegion>);
      const region = screen.getByTestId("scroll-panel");
      const scrollTo = vi.spyOn(region, "scrollTo").mockImplementation(() => {});
      return { scrollTo };
    }

    beforeEach(() => vi.useFakeTimers());
    afterEach(() => {
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    });

    it("scrolls to the top when focus reaches the first stop in it", async () => {
      const { scrollTo } = await renderRegion(
        <>
          <div>a heading nobody can focus</div>
          <button data-testid="first" data-top="90">
            first
          </button>
          <button data-top="140">second</button>
        </>,
      );

      fireEvent.focusIn(screen.getByTestId("first"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
    });

    it("leaves the scroll alone for a stop with focusable rows on both sides", async () => {
      const { scrollTo } = await renderRegion(
        <>
          <button data-top="90">first</button>
          <button data-testid="second" data-top="140">
            second
          </button>
          <button data-top="190">third</button>
        </>,
      );

      fireEvent.focusIn(screen.getByTestId("second"));
      vi.advanceTimersByTime(50);

      // Steam already scrolls the focused element into view; moving the region
      // as well would drag the page under a reader walking down it.
      expect(scrollTo).not.toHaveBeenCalled();
    });

    it("counts a wrapper around the first stop as its ancestor, not as something above it", async () => {
      // A container `Focusable` renders `tabindex="0"` of its own, so it
      // precedes the row inside it in document order. Comparing against the
      // first match would silently never fire wherever a page wraps its rows —
      // which `ListDetail` does for every row it lays out.
      const { scrollTo } = await renderRegion(
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- reproducing verbatim what Steam's container Focusable renders, which is the shape under test; giving it an interactive role would make it a different node from the one the handler has to see through.
        <div tabIndex={0} data-top="90">
          <button data-testid="wrapped" data-top="90">
            first
          </button>
        </div>,
      );

      fireEvent.focusIn(screen.getByTestId("wrapped"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
    });

    it("does nothing where the region has nothing to scroll", async () => {
      const { scrollTo } = await renderRegion(
        <button data-testid="only" data-top="90">
          only
        </button>,
        500,
      );

      fireEvent.focusIn(screen.getByTestId("only"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).not.toHaveBeenCalled();
    });

    it("refuses to scroll the first stop out of view to show what is above it", async () => {
      // Content taller than the region itself: there is no offset that shows
      // both, so Steam would scroll the element straight back and the two would
      // fight. Doing nothing leaves the reader the behaviour they had.
      const { scrollTo } = await renderRegion(
        <button data-testid="deep" data-top="600" data-h="40">
          first
        </button>,
      );

      fireEvent.focusIn(screen.getByTestId("deep"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).not.toHaveBeenCalled();
    });

    it("asks the node's own view what an element is, not this module's global", async () => {
      // Plugin code runs in the SharedJSContext window while these nodes belong
      // to the QAM's own document, so a bare `instanceof HTMLElement` names a
      // constructor from the wrong realm and rejects every node. Standing in
      // for that here: a view whose HTMLElement nothing is an instance of. The
      // module-global form would still scroll, because in one realm every node
      // passes.
      const { scrollTo } = await renderRegion(
        <button data-testid="first" data-top="90">
          first
        </button>,
      );
      const foreign = { HTMLElement: class Foreign {} } as unknown as Window;
      const owner = screen.getByTestId("scroll-panel").ownerDocument;
      const real = Object.getOwnPropertyDescriptor(Document.prototype, "defaultView");
      Object.defineProperty(owner, "defaultView", { configurable: true, get: () => foreign });
      try {
        // Dispatched directly: `fireEvent` reads `defaultView` to build the
        // event, which is the very thing this test replaces.
        screen.getByTestId("first").dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
        vi.advanceTimersByTime(50);
        expect(scrollTo).not.toHaveBeenCalled();
      } finally {
        if (real) Object.defineProperty(owner, "defaultView", real);
        else delete (owner as unknown as Record<string, unknown>).defaultView;
      }
    });

    it("does nothing when the node has no view at all", async () => {
      const { scrollTo } = await renderRegion(
        <button data-testid="first" data-top="90">
          first
        </button>,
      );
      const owner = screen.getByTestId("scroll-panel").ownerDocument;
      const real = Object.getOwnPropertyDescriptor(Document.prototype, "defaultView");
      Object.defineProperty(owner, "defaultView", { configurable: true, get: () => null });
      try {
        screen.getByTestId("first").dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
        vi.advanceTimersByTime(50);
        expect(scrollTo).not.toHaveBeenCalled();
      } finally {
        if (real) Object.defineProperty(owner, "defaultView", real);
        else delete (owner as unknown as Record<string, unknown>).defaultView;
      }
    });

    it("scrolls to the end when focus reaches the last stop in it", async () => {
      // The mirror of the heading above the first row: a legend, a total or a
      // hint under the last row has nothing below it to ride along with, and
      // Steam scrolls only far enough to show the focused element.
      const { scrollTo } = await renderRegion(
        <>
          <button data-top="90">first</button>
          <button data-testid="last" data-top="800">
            last
          </button>
        </>,
      );

      fireEvent.focusIn(screen.getByTestId("last"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
    });

    it("counts the row inside a focused wrapper as its descendant, not as something below it", async () => {
      // The same shape as the first-stop case read from the other end: a
      // container `Focusable` carries `tabindex="0"` and precedes the row it
      // wraps, so focus landing on the wrapper has a match after it that is its
      // own child. Counting that as something below would make the rule
      // silently never fire wherever a page wraps its rows.
      const { scrollTo } = await renderRegion(
        <>
          <button data-top="90">first</button>
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- reproducing verbatim what Steam's container Focusable renders, which is the shape under test; giving it an interactive role would make it a different node from the one the handler has to see through. */}
          <div tabIndex={0} data-testid="wrapper" data-top="800">
            <button data-top="800">last</button>
          </div>
        </>,
      );

      fireEvent.focusIn(screen.getByTestId("wrapper"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
    });

    it("refuses to scroll the last stop out of view to show what is below it", async () => {
      // Content below it taller than the region: there is no offset that shows
      // both, so Steam would scroll the element straight back and the two would
      // fight. Doing nothing leaves the reader the behaviour they had.
      const { scrollTo } = await renderRegion(
        <>
          <button data-top="50">first</button>
          <button data-testid="last" data-top="100">
            last
          </button>
        </>,
      );

      fireEvent.focusIn(screen.getByTestId("last"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).not.toHaveBeenCalled();
    });

    it("reveals the top rather than the end for a stop at both ends at once", async () => {
      // A region only a little taller than its viewport, where one stop
      // qualifies for both ends — 1200/500 cannot produce that, since no offset
      // is both within one screen of the top and within one of the end.
      // Reaching such a stop is entering the region, so the top wins.
      const { scrollTo } = await renderRegion(
        <button data-testid="only" data-top="150">
          only
        </button>,
        600,
      );

      fireEvent.focusIn(screen.getByTestId("only"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
      expect(scrollTo).toHaveBeenCalledTimes(1);
    });

    it("falls through to the end for a stop at both ends whose top does not fit", async () => {
      // The region's one row sits below a screenful of heading: no offset shows
      // that row and the region's top together, so revealing the top is refused
      // — and the end, which does fit, is still worth revealing. Stopping at the
      // refusal would leave whatever sits under the row permanently off-screen.
      const { scrollTo } = await renderRegion(
        <button data-testid="only" data-top="800" data-h="40">
          only
        </button>,
      );

      fireEvent.focusIn(screen.getByTestId("only"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
      expect(scrollTo).toHaveBeenCalledTimes(1);
    });

    it("reveals the top on the fallback branch too", async () => {
      const ScrollRegion = await loadScrollRegion(undefined);
      mockGeometry(1200);
      render(
        <ScrollRegion>
          <button data-testid="first" data-top="90">
            first
          </button>
        </ScrollRegion>,
      );
      const scrollTo = vi.spyOn(screen.getByTestId("focusable"), "scrollTo").mockImplementation(() => {});

      fireEvent.focusIn(screen.getByTestId("first"));
      vi.advanceTimersByTime(50);

      expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
    });
  });
});
