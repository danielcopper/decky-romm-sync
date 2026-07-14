/**
 * SessionBudgetBanner tests — the persistent QAM session-budget banners (#1383).
 * Props in, banner (or nothing) out, plus the "Restart Steam now" button. Covers
 * the blue paused banner, the yellow high-heap banner, precedence, the live-number
 * render, the ``rssKb === null`` text-only degradation, the memory-value colour
 * helper, and the restart-button wiring / disabled + running-game guard.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { toaster } from "@decky/api";
import { SessionBudgetBanner, formatGb, formatSignedGb, memoryLevelColor, HIGH_HEAP_KB } from "./SessionBudgetBanner";

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

describe("formatSignedGb", () => {
  it("prefixes an explicit sign and drops the unit (rendered inline after the GB reading)", () => {
    expect(formatSignedGb(800000)).toBe("+0.8");
    expect(formatSignedGb(-300000)).toBe("-0.3");
    expect(formatSignedGb(0)).toBe("+0.0");
  });
});

describe("memoryLevelColor", () => {
  const WARN = 1_800_000;
  const CEIL = 2_200_000;
  it("greens up to the warn floor, yellows strictly above it, reds at/above the ceiling", () => {
    expect(memoryLevelColor(440_000, WARN, CEIL)).toBe("#59bf40");
    expect(memoryLevelColor(WARN - 1, WARN, CEIL)).toBe("#59bf40");
    expect(memoryLevelColor(WARN, WARN, CEIL)).toBe("#59bf40"); // at the floor → still green (strict, like the banner)
    expect(memoryLevelColor(WARN + 1, WARN, CEIL)).toBe("#d4a72c");
    expect(memoryLevelColor(CEIL - 1, WARN, CEIL)).toBe("#d4a72c");
    expect(memoryLevelColor(CEIL, WARN, CEIL)).toBe("#d4343c"); // at the ceiling → red
    expect(memoryLevelColor(2_500_000, WARN, CEIL)).toBe("#d4343c");
  });
});

describe("SessionBudgetBanner — paused (blue)", () => {
  it("shows the blue paused banner with the live number when last run is paused", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} />);
    const banner = queryByTestId("budget-paused-banner");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("Steam memory is full (2.2 GB). Restart Steam, then Resume Sync.");
    // The threshold parenthetical is gone — the user can't act on those numbers.
    expect(banner!.textContent).not.toContain("pauses when a chunk would cross");
    expect(banner!.textContent).not.toContain("Steam crashes near");
    // No tildes anywhere in the copy, and no provenance ("measured on Steam Deck").
    expect(banner!.textContent).not.toContain("~");
    expect(banner!.textContent).not.toContain("measured on Steam Deck");
    // The high-heap (yellow) banner is not also shown.
    expect(queryByTestId("budget-high-heap-banner")).toBeNull();
  });

  it("drops the number but keeps the guidance when rssKb is null", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={null} />);
    const banner = queryByTestId("budget-paused-banner");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("Steam memory is full. Restart Steam, then Resume Sync.");
    expect(banner!.textContent).not.toContain("GB");
  });

  it("takes precedence over the high-heap banner even at a high heap", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={HIGH_HEAP_KB + 500000} />);
    expect(queryByTestId("budget-paused-banner")).not.toBeNull();
    expect(queryByTestId("budget-high-heap-banner")).toBeNull();
  });
});

describe("SessionBudgetBanner — run progress ('X of Y games done')", () => {
  it("reports how far the paused run got when both counts are known", () => {
    const { queryByTestId } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={2300000} runDoneItems={1200} runTotalItems={2001} />,
    );
    expect(queryByTestId("budget-paused-banner")!.textContent).toContain(
      "Steam memory is full (2.3 GB). 1200 of 2001 games done. Restart Steam, then Resume Sync.",
    );
  });

  it("reports progress on the resume-ready branch too", () => {
    const { queryByTestId } = render(
      <SessionBudgetBanner
        lastAttemptStatus="paused"
        rssKb={440000}
        resumeReady={true}
        runDoneItems={1200}
        runTotalItems={2001}
      />,
    );
    expect(queryByTestId("budget-paused-banner")!.textContent).toContain(
      "Steam memory is free again (0.4 GB). 1200 of 2001 games done. Press Resume Sync to continue.",
    );
  });

  it("omits the sentence entirely when the counts are unknown (a backend reload wiped them)", () => {
    const { queryByTestId } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={2300000} runDoneItems={null} runTotalItems={null} />,
    );
    const text = queryByTestId("budget-paused-banner")!.textContent;
    expect(text).toContain("Steam memory is full (2.3 GB). Restart Steam, then Resume Sync.");
    expect(text).not.toContain("games done");
  });

  it("omits the sentence when only one of the two counts is known (never a placeholder)", () => {
    const { queryByTestId } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={2300000} runDoneItems={1200} runTotalItems={null} />,
    );
    expect(queryByTestId("budget-paused-banner")!.textContent).not.toContain("1200");
  });

  it("omits the sentence when the total is zero (never '0 of 0 games done')", () => {
    const { queryByTestId } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={2300000} runDoneItems={0} runTotalItems={0} />,
    );
    expect(queryByTestId("budget-paused-banner")!.textContent).not.toContain("games done");
  });

  it("renders a zero done count against a real total (a run that paused before its first chunk)", () => {
    const { queryByTestId } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={2300000} runDoneItems={0} runTotalItems={2001} />,
    );
    expect(queryByTestId("budget-paused-banner")!.textContent).toContain("0 of 2001 games done.");
  });
});

describe("SessionBudgetBanner — paused, resume ready (#38)", () => {
  function restartButton(container: HTMLElement) {
    return buttonByText(container, "Restart Steam now");
  }

  it("flips to 'memory is free' and hides the restart button when resumeReady is true", () => {
    const { queryByTestId, container } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={500000} resumeReady={true} />,
    );
    const banner = queryByTestId("budget-paused-banner");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("Steam memory is free again (0.5 GB). Press Resume Sync to continue.");
    // The restart guidance is gone, and the restart button is hidden (pointless).
    expect(banner!.textContent).not.toContain("Restart Steam");
    expect(restartButton(container)).toBeNull();
  });

  it("keeps the restart guidance + button when resumeReady is false (still high)", () => {
    const { queryByTestId, container } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} resumeReady={false} />,
    );
    const banner = queryByTestId("budget-paused-banner");
    expect(banner!.textContent).toContain("Steam memory is full (2.2 GB). Restart Steam, then Resume Sync.");
    expect(banner!.textContent).not.toContain("Steam memory is free again");
    expect(restartButton(container)).not.toBeNull();
  });

  it("keeps the restart guidance + button when resumeReady is null (undecidable → conservative)", () => {
    const { queryByTestId, container } = render(
      <SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} resumeReady={null} />,
    );
    expect(queryByTestId("budget-paused-banner")!.textContent).toContain("Restart Steam, then Resume Sync.");
    expect(restartButton(container)).not.toBeNull();
  });
});

describe("SessionBudgetBanner — high heap (yellow)", () => {
  it("shows the yellow banner when the live heap is above the threshold after a non-paused run", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="completed" rssKb={1900000} />);
    const banner = queryByTestId("budget-high-heap-banner");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain(
      "Steam memory is high: 1.9 GB of 2.4 GB — restart Steam before further large syncs.",
    );
    expect(banner!.textContent).not.toContain("~");
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

describe("SessionBudgetBanner — Restart Steam now button (#35)", () => {
  beforeEach(() => {
    // The setup-file SteamClient stub is unstubbed after each test, so re-stub a
    // fresh StartRestart spy here (matches the CustomPlayButton test pattern).
    vi.stubGlobal("SteamClient", { User: { StartRestart: vi.fn() } });
    vi.mocked(toaster.toast).mockReset();
  });

  it("renders the restart button on the paused banner and restarts Steam on click", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} />);
    const btn = buttonByText(container, "Restart Steam now");
    expect(btn).not.toBeNull();
    fireEvent.click(btn!);
    expect(vi.mocked(SteamClient.User.StartRestart)).toHaveBeenCalledWith(false);
  });

  it("renders the restart button on the high-heap banner too", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="completed" rssKb={1900000} />);
    const btn = buttonByText(container, "Restart Steam now");
    expect(btn).not.toBeNull();
    fireEvent.click(btn!);
    expect(vi.mocked(SteamClient.User.StartRestart)).toHaveBeenCalledWith(false);
  });

  it("disables the button when restartDisabled is true", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} restartDisabled />);
    expect(buttonByText(container, "Restart Steam now")!.disabled).toBe(true);
  });

  it("carries NO description on the enabled button (the banner body already says why)", () => {
    const { queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} />);
    // The ButtonItem mock renders a description into "button-desc" when one is
    // passed, so its absence is a real assertion, not a dropped prop.
    expect(queryByTestId("button-desc")).toBeNull();
  });

  it("disables the button while a game is running, and says why (the one kept description)", () => {
    vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 123, display_name: "Game" }] });
    const { container, queryByTestId } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} />);
    expect(buttonByText(container, "Restart Steam now")!.disabled).toBe(true);
    expect(queryByTestId("button-desc")!.textContent).toBe(
      "Close your running game first — restarting Steam would close it.",
    );
  });

  it("hard-guards the click so a game that started after render can't be killed", () => {
    // Rendered with no game → button enabled.
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="paused" rssKb={2199000} />);
    const btn = buttonByText(container, "Restart Steam now")!;
    expect(btn.disabled).toBe(false);
    // A game starts before the click lands — the click-time guard must win.
    vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: 123, display_name: "Game" }] });
    fireEvent.click(btn);
    expect(vi.mocked(SteamClient.User.StartRestart)).not.toHaveBeenCalled();
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Close your running game") }),
    );
  });

  it("shows no restart button when no banner is shown", () => {
    const { container } = render(<SessionBudgetBanner lastAttemptStatus="completed" rssKb={440000} />);
    expect(buttonByText(container, "Restart Steam now")).toBeNull();
  });
});
