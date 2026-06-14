import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { createElement, type ComponentProps } from "react";
import { SettingsResetCard } from "./SettingsResetCard";
import { SETTINGS_RESET_TITLE, settingsResetCardMessage } from "./SettingsResetBanner";
import type { WarningCard } from "./WarningCard";

// Capture the props passed to WarningCard. Pinning the captured-props type
// to the real component keeps assertions in sync as WarningCard evolves.
type CapturedWarningCardProps = ComponentProps<typeof WarningCard>;
const capturedWarningCard: CapturedWarningCardProps[] = [];

vi.mock("./WarningCard", () => ({
  WarningCard: (props: CapturedWarningCardProps) => {
    capturedWarningCard.push(props);
    return createElement("div", { "data-testid": "warning-card" });
  },
}));

describe("SettingsResetCard", () => {
  it("delegates to WarningCard with the reset title + QAM-dismiss message (default compact=false)", () => {
    capturedWarningCard.length = 0;
    const { queryByTestId } = render(<SettingsResetCard backedUpTo="settings.json.corrupt-9" />);
    expect(queryByTestId("warning-card")).not.toBeNull();
    expect(capturedWarningCard).toHaveLength(1);
    expect(capturedWarningCard[0]).toEqual({
      title: SETTINGS_RESET_TITLE,
      message: settingsResetCardMessage("settings.json.corrupt-9"),
      compact: false,
    });
    expect(capturedWarningCard[0]?.message).toContain("settings.json.corrupt-9");
    // Informational only — the copy points the user to the QAM to dismiss.
    expect(capturedWarningCard[0]?.message).toContain("Open the RomM Sync menu (QAM) to dismiss this.");
  });

  it("forwards compact=true and the generic message when backup path is null", () => {
    capturedWarningCard.length = 0;
    render(<SettingsResetCard backedUpTo={null} compact />);
    expect(capturedWarningCard[0]?.compact).toBe(true);
    expect(capturedWarningCard[0]?.message).toContain("A backup of your old settings was saved.");
    expect(capturedWarningCard[0]?.message).toContain("Open the RomM Sync menu (QAM) to dismiss this.");
  });

  it("renders NO dismiss button — it delegates only to the (button-less) WarningCard", () => {
    capturedWarningCard.length = 0;
    const { queryByText } = render(<SettingsResetCard backedUpTo="settings.json.corrupt-9" />);
    // The card is informational; the ack control lives in the QAM banner.
    expect(queryByText("Dismiss")).toBeNull();
    expect(queryByText("Got it")).toBeNull();
  });
});
