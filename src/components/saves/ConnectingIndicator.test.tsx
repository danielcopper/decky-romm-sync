import { describe, it, expect, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import { ConnectingIndicator } from "./ConnectingIndicator";
import { setServerRetryProgress } from "../../utils/connectionState";

describe("ConnectingIndicator (#1345)", () => {
  beforeEach(() => {
    setServerRetryProgress(null);
  });

  it("renders the default label + spinner with no attempt suffix when no retry is in flight", () => {
    const { container } = render(<ConnectingIndicator />);
    expect(container.textContent).toContain("Connecting to RomM…");
    expect(container.textContent).not.toContain("attempt");
    expect(container.querySelector(".romm-throbber")).not.toBeNull();
  });

  it("appends the live attempt count when the retry store has a value", () => {
    setServerRetryProgress({ attempt: 2, maxAttempts: 3 });
    const { container } = render(<ConnectingIndicator />);
    expect(container.textContent).toContain("Connecting to RomM… (attempt 2/3)");
  });

  it("advances live as the retry store increments, then drops the suffix when cleared", () => {
    const { container } = render(<ConnectingIndicator />);
    act(() => setServerRetryProgress({ attempt: 2, maxAttempts: 3 }));
    expect(container.textContent).toContain("(attempt 2/3)");
    act(() => setServerRetryProgress({ attempt: 3, maxAttempts: 3 }));
    expect(container.textContent).toContain("(attempt 3/3)");
    act(() => setServerRetryProgress(null));
    expect(container.textContent).toContain("Connecting to RomM…");
    expect(container.textContent).not.toContain("attempt");
  });

  it("uses a custom label when provided", () => {
    setServerRetryProgress({ attempt: 2, maxAttempts: 3 });
    const { container } = render(<ConnectingIndicator label="Setting up" />);
    expect(container.textContent).toContain("Setting up… (attempt 2/3)");
  });
});
