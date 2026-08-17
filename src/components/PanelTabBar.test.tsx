// Which tabs exist for a given ROM is decided by the panel and asserted there
// (RomMGameInfoPanel.test.tsx). What this file exists for are the two contracts
// that end at this component's own edge: which button carries the active
// marking, and that a press reports the tab id rather than acting on it — the
// panel can only observe the pane that follows, which a wrong id would still
// produce for the wrong tab.

import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { PanelTabBar } from "./PanelTabBar";

describe("PanelTabBar", () => {
  it("reports the pressed tab's id", () => {
    const onSelect = vi.fn();
    const { getByText } = render(<PanelTabBar activeTab="info" hasAchievements hasSaves hasBios onSelect={onSelect} />);
    fireEvent.click(getByText("SAVES"));
    expect(onSelect).toHaveBeenCalledWith("saves");
  });

  it("marks the active tab and only the active tab", () => {
    const { getByText } = render(<PanelTabBar activeTab="bios" hasAchievements hasSaves hasBios onSelect={vi.fn()} />);
    expect(getByText("BIOS").className).toContain("romm-tab-active");
    expect(getByText("GAME INFO").className).not.toContain("romm-tab-active");
  });

  it("renders the tabs in their fixed order", () => {
    const { getAllByRole } = render(
      <PanelTabBar activeTab="info" hasAchievements hasSaves hasBios onSelect={vi.fn()} />,
    );
    expect(getAllByRole("button").map((b) => b.textContent)).toEqual(["GAME INFO", "ACHIEVEMENTS", "SAVES", "BIOS"]);
  });

  it("drops the tabs whose pane has nothing to show", () => {
    const { getByText, queryByText } = render(
      <PanelTabBar activeTab="info" hasAchievements={false} hasSaves={false} hasBios={false} onSelect={vi.fn()} />,
    );
    expect(getByText("GAME INFO")).not.toBeNull();
    expect(queryByText("ACHIEVEMENTS")).toBeNull();
    expect(queryByText("SAVES")).toBeNull();
    expect(queryByText("BIOS")).toBeNull();
  });
});
