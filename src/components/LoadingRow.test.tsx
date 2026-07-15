import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { createElement as ce } from "react";
import { LoadingRow, LOADING_SPINNER_SIZE } from "./LoadingRow";

// Re-mock @decky/ui so the Spinner echoes its width/height — LoadingRow exists
// to pin a small fixed size, so the test must be able to see those props (the
// global stub in test-setup.ts drops them). Per-file mock hoisting wins.
vi.mock("@decky/ui", () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  return {
    PanelSectionRow: (p: AnyProps) => ce("div", { "data-testid": "panel-row" }, p.children as never),
    Spinner: (p: { width?: number; height?: number }) =>
      ce("div", { "data-testid": "spinner", "data-width": p.width, "data-height": p.height }),
  };
});

describe("LoadingRow (#1414)", () => {
  it("renders a spinner constrained to the fixed LOADING_SPINNER_SIZE", () => {
    const { getByTestId } = render(<LoadingRow />);
    const spinner = getByTestId("spinner");
    expect(spinner.getAttribute("data-width")).toBe(String(LOADING_SPINNER_SIZE));
    expect(spinner.getAttribute("data-height")).toBe(String(LOADING_SPINNER_SIZE));
  });

  it("pins a small size so the SVG can't fill the row (guards the oversized-spinner regression)", () => {
    expect(LOADING_SPINNER_SIZE).toBeGreaterThan(0);
    expect(LOADING_SPINNER_SIZE).toBeLessThanOrEqual(32);
  });

  it("renders inside a PanelSectionRow", () => {
    const { getByTestId } = render(<LoadingRow />);
    expect(getByTestId("panel-row")).not.toBeNull();
  });

  it("shows no caption text when no label is given", () => {
    const { container } = render(<LoadingRow />);
    expect(container.textContent).toBe("");
  });

  it("renders the optional label beside the spinner", () => {
    const { getByText } = render(<LoadingRow label="Loading platforms…" />);
    expect(getByText("Loading platforms…")).not.toBeNull();
  });
});
