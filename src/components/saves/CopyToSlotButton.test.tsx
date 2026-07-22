import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { renderCopyToSlotButton, type CopyToSlotRowProps } from "./CopyToSlotButton";

function renderButton(overrides: Partial<CopyToSlotRowProps> = {}) {
  const onCopy = vi.fn();
  const copy: CopyToSlotRowProps = { onCopy, sourceSlot: "backup", isOffline: false, ...overrides };
  const utils = render(renderCopyToSlotButton("copy-7", 7, copy));
  return { ...utils, onCopy };
}

describe("renderCopyToSlotButton", () => {
  it("renders the 'Copy to slot…' label", () => {
    const { getByText } = renderButton();
    expect(getByText("Copy to slot…")).toBeInTheDocument();
  });

  it("hands (saveId, sourceSlot) to onCopy on click", () => {
    const { getByText, onCopy } = renderButton({ sourceSlot: "backup" });
    fireEvent.click(getByText("Copy to slot…"));
    // Non-vacuous: the exact save id and source slot flow through.
    expect(onCopy).toHaveBeenCalledWith(7, "backup");
  });

  it("is disabled and inert while offline", () => {
    const { getByText, onCopy } = renderButton({ isOffline: true });
    const btn = getByText("Copy to slot…").closest("button");
    expect(btn).toBeDisabled();
    // A disabled button never fires onCopy.
    fireEvent.click(getByText("Copy to slot…"));
    expect(onCopy).not.toHaveBeenCalled();
  });
});
