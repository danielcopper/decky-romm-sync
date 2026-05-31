import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { TextInputModal, pendingEdits } from "./TextInputModal";

// Override the global @decky/ui stub: capture ConfirmModal props (onOK,
// bDisableBackgroundDismiss, closeModal, strTitle) — the global passthrough
// <div> drops them. Mirrors the NewSlotModal test pattern.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface CapturedConfirm {
  onOK?: () => void;
  bDisableBackgroundDismiss?: boolean;
  closeModal?: unknown;
  strTitle?: string;
}
const captured: CapturedConfirm & { textFieldPassword?: boolean } = {};

vi.mock("@decky/ui", () => ({
  ConfirmModal: (p: AnyProps & CapturedConfirm) => {
    captured.onOK = p.onOK;
    captured.bDisableBackgroundDismiss = p.bDisableBackgroundDismiss;
    captured.closeModal = p.closeModal;
    captured.strTitle = p.strTitle;
    return createElement("div", { "data-testid": "confirm-modal" }, p.children as never);
  },
  TextField: (
    p: AnyProps & {
      value?: string;
      bIsPassword?: boolean;
      onChange?: (e: { target: { value: string } }) => void;
    },
  ) => {
    captured.textFieldPassword = p.bIsPassword;
    return createElement("input", {
      value: p.value ?? "",
      type: p.bIsPassword ? "password" : "text",
      onChange: (e: unknown) => p.onChange?.(e as { target: { value: string } }),
    });
  },
}));

function reset() {
  for (const k of Object.keys(captured) as Array<keyof typeof captured>) {
    delete captured[k];
  }
  delete pendingEdits.url;
  delete pendingEdits.username;
  delete pendingEdits.password;
}

describe("TextInputModal", () => {
  beforeEach(reset);

  it("renders the ConfirmModal with the provided label as title", () => {
    render(<TextInputModal label="RomM URL" value="" onSubmit={vi.fn()} />);
    expect(captured.strTitle).toBe("RomM URL");
  });

  it("disables background dismiss", () => {
    render(<TextInputModal label="x" value="" onSubmit={vi.fn()} />);
    expect(captured.bDisableBackgroundDismiss).toBe(true);
  });

  it("seeds the input with the initial value", () => {
    render(<TextInputModal label="x" value="http://romm.local" onSubmit={vi.fn()} />);
    const input = document.querySelector("input") as HTMLInputElement;
    expect(input.value).toBe("http://romm.local");
  });

  it("updates the input value when the user types", () => {
    render(<TextInputModal label="x" value="old" onSubmit={vi.fn()} />);
    const input = document.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "new" } });
    expect(input.value).toBe("new");
  });

  it("invokes onSubmit with the current value when OK is pressed", () => {
    const onSubmit = vi.fn();
    render(<TextInputModal label="x" value="initial" onSubmit={onSubmit} />);
    const input = document.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "edited" } });
    captured.onOK?.();
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("edited");
  });

  it("does NOT trim — submits whitespace as-is (parent is responsible)", () => {
    const onSubmit = vi.fn();
    render(<TextInputModal label="x" value="" onSubmit={onSubmit} />);
    const input = document.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  spaced  " } });
    captured.onOK?.();
    expect(onSubmit).toHaveBeenCalledWith("  spaced  ");
  });

  it("forwards bIsPassword=true to the TextField as a password input", () => {
    render(<TextInputModal label="Password" value="" bIsPassword onSubmit={vi.fn()} />);
    expect(captured.textFieldPassword).toBe(true);
    const input = document.querySelector("input");
    expect(input?.getAttribute("type")).toBe("password");
  });

  it("renders a non-password TextField when bIsPassword is omitted", () => {
    render(<TextInputModal label="URL" value="" onSubmit={vi.fn()} />);
    expect(captured.textFieldPassword).toBeUndefined();
    const input = document.querySelector("input");
    expect(input?.getAttribute("type")).toBe("text");
  });

  it("forwards closeModal to ConfirmModal", () => {
    const closeModal = vi.fn();
    render(<TextInputModal label="x" value="" closeModal={closeModal} onSubmit={vi.fn()} />);
    expect(captured.closeModal).toBe(closeModal);
  });

  it("works without closeModal", () => {
    render(<TextInputModal label="x" value="" onSubmit={vi.fn()} />);
    expect(captured.closeModal).toBeUndefined();
  });

  describe("pendingEdits persistence", () => {
    it("writes the current value to pendingEdits[field] when field='url'", () => {
      render(<TextInputModal label="URL" value="" field="url" onSubmit={vi.fn()} />);
      const input = document.querySelector("input") as HTMLInputElement;
      fireEvent.change(input, { target: { value: "http://romm.local" } });
      captured.onOK?.();
      expect(pendingEdits.url).toBe("http://romm.local");
      expect(pendingEdits.username).toBeUndefined();
      expect(pendingEdits.password).toBeUndefined();
    });

    it("writes pendingEdits.username when field='username'", () => {
      render(<TextInputModal label="User" value="" field="username" onSubmit={vi.fn()} />);
      const input = document.querySelector("input") as HTMLInputElement;
      fireEvent.change(input, { target: { value: "daniel" } });
      captured.onOK?.();
      expect(pendingEdits.username).toBe("daniel");
    });

    it("writes pendingEdits.password when field='password'", () => {
      render(<TextInputModal label="Pwd" value="" field="password" bIsPassword onSubmit={vi.fn()} />);
      const input = document.querySelector("input") as HTMLInputElement;
      fireEvent.change(input, { target: { value: "s3cret" } });
      captured.onOK?.();
      expect(pendingEdits.password).toBe("s3cret");
    });

    it("does NOT touch pendingEdits when field is omitted", () => {
      render(<TextInputModal label="x" value="" onSubmit={vi.fn()} />);
      const input = document.querySelector("input") as HTMLInputElement;
      fireEvent.change(input, { target: { value: "nope" } });
      captured.onOK?.();
      expect(pendingEdits.url).toBeUndefined();
      expect(pendingEdits.username).toBeUndefined();
      expect(pendingEdits.password).toBeUndefined();
    });

    it("still calls onSubmit when field is set (pendingEdits write does not short-circuit)", () => {
      const onSubmit = vi.fn();
      render(<TextInputModal label="x" value="" field="url" onSubmit={onSubmit} />);
      captured.onOK?.();
      expect(onSubmit).toHaveBeenCalledWith("");
    });
  });
});
