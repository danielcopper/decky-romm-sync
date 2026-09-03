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

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { CSSProperties, FC, ReactNode } from "react";
import type { ScrollRegionProps } from "./ScrollRegion";

interface StubPanelProps {
  style?: CSSProperties;
  children?: ReactNode;
}

/** Stand-in for Steam's scroll panel: it keeps the style so bounds stay assertable. */
const StubScrollPanel: FC<StubPanelProps> = ({ style, children }) => (
  <div data-testid="scroll-panel" style={style}>
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
});
