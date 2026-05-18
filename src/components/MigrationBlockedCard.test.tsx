import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { createElement, type ComponentProps } from "react";
import { MigrationBlockedCard } from "./MigrationBlockedCard";
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

describe("MigrationBlockedCard", () => {
  it("delegates to WarningCard with the migration title + message (default compact=false)", () => {
    capturedWarningCard.length = 0;
    const { queryByTestId } = render(<MigrationBlockedCard />);
    expect(queryByTestId("warning-card")).not.toBeNull();
    expect(capturedWarningCard).toHaveLength(1);
    expect(capturedWarningCard[0]).toEqual({
      title: "RetroDECK Migration Required",
      message:
        "Open the plugin QAM to migrate files or dismiss the migration before playing.",
      compact: false,
    });
  });

  it("forwards compact=true", () => {
    capturedWarningCard.length = 0;
    render(<MigrationBlockedCard compact />);
    expect(capturedWarningCard[0]?.compact).toBe(true);
  });
});
