/**
 * ScrollRegion tests — that the region reaches Steam's scroll panel when the
 * probe found one, that it still is a bounded region of the focus tree when the
 * probe missed, and that a caller can place it without losing its bounds.
 *
 * What the panel then does with a gamepad is Steam's, and happy-dom has neither
 * the panel nor a gamepad; the device round covers the scrolling itself.
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
const StubScrollPanelGroup: FC<StubPanelProps> = ({ style, children }) => (
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
  vi.doMock("../../utils/deckyUiInternals", () => ({ ScrollPanelGroup: panel }));
  return (await import("./ScrollRegion")).ScrollRegion;
}

/** The bounds every region carries, whichever element ends up carrying them. */
function expectBounds(el: HTMLElement): void {
  expect(el.style.height).toBe("100%");
  expect(el.style.overflow).toBe("auto");
  // Without the floor reset a flex child's min-height is its content, so the
  // region would grow past its parent instead of scrolling inside it. happy-dom
  // keeps the "0" verbatim where a browser would normalise it to "0px".
  expect(el.style.minHeight).toBe("0");
}

describe("ScrollRegion", () => {
  it("puts its content in Steam's scroll panel when the probe found one", async () => {
    const ScrollRegion = await loadScrollRegion(StubScrollPanelGroup);

    render(
      <ScrollRegion>
        <div>region content</div>
      </ScrollRegion>,
    );

    // Steam's gamepad navigation scrolls by moving focus, so content nobody can
    // focus scrolls only from inside the panel. A bare div here would leave a
    // detail pane of text rows reachable with a mouse and nothing else.
    const panel = screen.getByTestId("scroll-panel");
    expect(panel).toContainElement(screen.getByText("region content"));
    expectBounds(panel);
  });

  it("keeps the region, its bounds and its place in the focus tree when the probe missed", async () => {
    const ScrollRegion = await loadScrollRegion(undefined);

    render(
      <ScrollRegion>
        <div>region content</div>
      </ScrollRegion>,
    );

    expect(screen.queryByTestId("scroll-panel")).not.toBeInTheDocument();
    // A Focusable rather than a div: the region is a level of the focus tree
    // either way, so a missed probe costs the scrolling and nothing else.
    const region = screen.getByTestId("focusable");
    expect(region).toContainElement(screen.getByText("region content"));
    expectBounds(region);
  });

  it("lets the caller place the region without losing its bounds", async () => {
    const ScrollRegion = await loadScrollRegion(StubScrollPanelGroup);

    render(<ScrollRegion style={{ flex: "0 0 264px", width: "264px" }}>{null}</ScrollRegion>);

    const panel = screen.getByTestId("scroll-panel");
    expect(panel.style.flex).toBe("0 0 264px");
    expect(panel.style.width).toBe("264px");
    expectBounds(panel);
  });
});
