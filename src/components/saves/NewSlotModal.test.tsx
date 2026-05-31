import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { NewSlotModal } from "./NewSlotModal";

// Override the global @decky/ui stub so we can capture the ConfirmModal props
// (onOK, bDisableBackgroundDismiss, closeModal) — the global passthrough <div>
// silently drops them.
type AnyProps = Record<string, unknown> & { children?: unknown };
const captured: {
  onOK?: (() => void) | undefined;
  bDisableBackgroundDismiss?: boolean | undefined;
  closeModal?: unknown;
  strTitle?: string | undefined;
} = {};

vi.mock("@decky/ui", () => ({
  ConfirmModal: (
    p: AnyProps & {
      onOK?: () => void;
      bDisableBackgroundDismiss?: boolean;
      closeModal?: unknown;
      strTitle?: string;
    },
  ) => {
    captured.onOK = p.onOK;
    captured.bDisableBackgroundDismiss = p.bDisableBackgroundDismiss;
    captured.closeModal = p.closeModal;
    captured.strTitle = p.strTitle;
    return createElement("div", { "data-testid": "confirm-modal" }, p.children as never);
  },
  TextField: (p: AnyProps & { value?: string; onChange?: (e: unknown) => void }) =>
    createElement("input", {
      value: p.value ?? "",
      onChange: (e: unknown) => p.onChange?.(e),
    }),
}));

describe("NewSlotModal", () => {
  it("renders a ConfirmModal with the slot-name text field", () => {
    render(<NewSlotModal onSubmit={vi.fn()} />);
    const input = document.querySelector("input");
    expect(input).not.toBeNull();
    expect(input?.value).toBe("");
    expect(captured.strTitle).toBe("New Save Slot");
  });

  it("disables background dismiss", () => {
    render(<NewSlotModal onSubmit={vi.fn()} />);
    expect(captured.bDisableBackgroundDismiss).toBe(true);
  });

  it("controls the input value via React state", () => {
    render(<NewSlotModal onSubmit={vi.fn()} />);
    const input = document.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "speedrun" } });
    expect(input.value).toBe("speedrun");
  });

  it("submits the trimmed value when OK is pressed", () => {
    const onSubmit = vi.fn();
    render(<NewSlotModal onSubmit={onSubmit} />);
    const input = document.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  myslot  " } });
    captured.onOK?.();
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("myslot");
  });

  it("submits an empty string when OK is pressed with empty input", () => {
    const onSubmit = vi.fn();
    render(<NewSlotModal onSubmit={onSubmit} />);
    captured.onOK?.();
    expect(onSubmit).toHaveBeenCalledWith("");
  });

  it("submits an empty string when input contains only whitespace (trim collapses)", () => {
    const onSubmit = vi.fn();
    render(<NewSlotModal onSubmit={onSubmit} />);
    const input = document.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "    " } });
    captured.onOK?.();
    expect(onSubmit).toHaveBeenCalledWith("");
  });

  it("forwards closeModal to the underlying ConfirmModal", () => {
    const closeModal = vi.fn();
    render(<NewSlotModal closeModal={closeModal} onSubmit={vi.fn()} />);
    expect(captured.closeModal).toBe(closeModal);
  });

  it("works without closeModal (optional prop)", () => {
    render(<NewSlotModal onSubmit={vi.fn()} />);
    expect(captured.closeModal).toBeUndefined();
  });
});
