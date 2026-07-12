/**
 * SessionBudgetBanner tests — the persistent QAM session-budget banners (#1383).
 * Props in, banner (or nothing) out, plus the "Free Steam memory" reload button.
 * Covers the blue paused banner, the yellow high-heap banner, precedence, the
 * live-number render, the ``rssKb === null`` text-only degradation, and the
 * reload-button wiring/disabled state.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { SessionBudgetBanner, formatGb, HIGH_HEAP_KB } from "./SessionBudgetBanner";
import * as backend from "../api/backend";

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement | null {
  return (Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text) ??
    null) as HTMLButtonElement | null;
}

describe("formatGb", () => {
  it("renders KB as one-decimal decimal-GB", () => {
    expect(formatGb(2252712)).toBe("2.3 GB");
    expect(formatGb(1900000)).toBe("1.9 GB");
    expect(formatGb(440000)).toBe("0.4 GB");
  });
});

describe("SessionBudgetBanner — paused (blue)", () => {
  it("shows the blue paused banner with the live number when last run is paused", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2299000} />);
    const banner = queryByTestId("budget-paused-banner");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("Restart Steam when convenient, then Resume Sync.");
    expect(banner!.textContent).toContain("Steam memory: 2.3 GB");
    expect(banner!.textContent).toContain("Steam crashes near ~2.4 GB, measured on Steam Deck");
    // The high-heap (yellow) banner is not also shown.
    expect(queryByTestId("budget-high-heap-banner")).toBeNull();
  });

  it("drops the number but keeps the guidance when rssKb is null", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={null} />);
    const banner = queryByTestId("budget-paused-banner");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("Restart Steam when convenient, then Resume Sync.");
    expect(banner!.textContent).not.toContain("Steam memory:");
    expect(banner!.textContent).not.toContain("GB");
  });

  it("takes precedence over the high-heap banner even at a high heap", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={HIGH_HEAP_KB + 500000} />);
    expect(queryByTestId("budget-paused-banner")).not.toBeNull();
    expect(queryByTestId("budget-high-heap-banner")).toBeNull();
  });
});

describe("SessionBudgetBanner — high heap (yellow)", () => {
  it("shows the yellow banner when the live heap is above the threshold after a non-paused run", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="completed" rssKb={1900000} />);
    const banner = queryByTestId("budget-high-heap-banner");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("Steam memory is high: 1.9 GB of ~2.4 GB");
    expect(banner!.textContent).toContain("restart Steam before further large syncs");
    expect(queryByTestId("budget-paused-banner")).toBeNull();
  });

  it("shows nothing when heap is at or below the threshold", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="completed" rssKb={HIGH_HEAP_KB} />);
    expect(queryByTestId("budget-high-heap-banner")).toBeNull();
    expect(queryByTestId("budget-paused-banner")).toBeNull();
  });

  it("shows nothing when the reading is unavailable and the run was not paused", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="cancelled" rssKb={null} />);
    expect(queryByTestId("budget-high-heap-banner")).toBeNull();
    expect(queryByTestId("budget-paused-banner")).toBeNull();
  });
});

describe("SessionBudgetBanner — Free Steam memory button (#31)", () => {
  beforeEach(() => {
    vi.mocked(backend.reloadSteamUi).mockReset();
    vi.mocked(backend.reloadSteamUi).mockResolvedValue({ success: true, message: "" });
  });

  it("renders the reload button on the paused banner and fires reloadSteamUi on click", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2299000} />);
    const btn = buttonByText(container, "Free Steam memory");
    expect(btn).not.toBeNull();
    fireEvent.click(btn!);
    expect(backend.reloadSteamUi).toHaveBeenCalledTimes(1);
  });

  it("renders the reload button on the high-heap banner too", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="completed" rssKb={1900000} />);
    const btn = buttonByText(container, "Free Steam memory");
    expect(btn).not.toBeNull();
    fireEvent.click(btn!);
    expect(backend.reloadSteamUi).toHaveBeenCalledTimes(1);
  });

  it("disables the button when reloadDisabled is true", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2299000} reloadDisabled />);
    const btn = buttonByText(container, "Free Steam memory");
    expect(btn).not.toBeNull();
    expect(btn!.disabled).toBe(true);
  });

  it("shows no reload button when no banner is shown", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="completed" rssKb={440000} />);
    expect(buttonByText(container, "Free Steam memory")).toBeNull();
  });
});
