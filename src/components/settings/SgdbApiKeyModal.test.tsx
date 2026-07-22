import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, forwardRef } from "react";
import { SgdbApiKeyModal } from "./SgdbApiKeyModal";

// Local @decky/ui mock (mirrors ConnectModal.test.tsx): ModalRoot passes its
// children through (the modal owns its own footer buttons); DialogButton renders
// a real <button> forwarding onClick + disabled + children so Save / Cancel are
// clickable and their disabled state is assertable (a disabled button swallows
// the click in happy-dom, mirroring the real gate); TextField forwards label +
// value + onChange + onKeyDown to a real <input>; Focusable forwards its ref to
// a wrapping div.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface TextFieldProps {
  label?: string;
  value?: string;
  bIsPassword?: boolean;
  onChange?: (e: { target: { value: string } }) => void;
  onKeyDown?: (e: unknown) => void;
}

vi.mock("@decky/ui", () => ({
  ModalRoot: (p: AnyProps) => createElement("div", { "data-testid": "modal-root" }, p.children as never),
  Focusable: forwardRef<HTMLDivElement, AnyProps>((p, ref) =>
    createElement("div", { ref, style: p.style }, p.children as never),
  ),
  DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children as never),
  TextField: (p: TextFieldProps) =>
    createElement("input", {
      "data-testid": `field-${p.label ?? ""}`,
      "data-is-password": p.bIsPassword ? "true" : "false",
      value: p.value ?? "",
      onChange: (e: unknown) => p.onChange?.(e as { target: { value: string } }),
      onKeyDown: (e: unknown) => p.onKeyDown?.(e),
    }),
}));

// Resolved handlers: onVerify defaults to a valid key, onSave to a no-op.
const verifyOk = (message = "API key is valid") => vi.fn().mockResolvedValue({ success: true, message });
const verifyFail = (message: string) => vi.fn().mockResolvedValue({ success: false, message });
const saveOk = () => vi.fn().mockResolvedValue(undefined);

// Flush the floating submit() promise (onClick fires `void submit()`), then let
// React apply the resulting state updates. A few microtask ticks cover the
// `await onVerify(...)` + `await onSave(...)` + finally.
async function flushSubmit() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

const field = (getByTestId: (id: string) => HTMLElement): HTMLInputElement =>
  getByTestId("field-API Key") as HTMLInputElement;

describe("SgdbApiKeyModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders an obscured, empty API-key field (write-only) plus Save and Cancel", () => {
    const { getByTestId, getByText } = render(<SgdbApiKeyModal onVerify={verifyOk()} onSave={saveOk()} />);
    const input = field(getByTestId);
    expect(input.value).toBe("");
    expect(input.getAttribute("data-is-password")).toBe("true");
    expect(getByText("Save")).toBeTruthy();
    expect(getByText("Cancel")).toBeTruthy();
  });

  it("Cancel closes the modal without verifying or saving", () => {
    const closeModal = vi.fn();
    const onVerify = verifyOk();
    const onSave = saveOk();
    const { getByText } = render(<SgdbApiKeyModal closeModal={closeModal} onVerify={onVerify} onSave={onSave} />);
    fireEvent.click(getByText("Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onVerify).not.toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });

  describe("submit gating", () => {
    it("disables Save until the field is non-empty", () => {
      const { getByTestId, getByText } = render(<SgdbApiKeyModal onVerify={verifyOk()} onSave={saveOk()} />);
      expect(getByText("Save")).toBeDisabled();
      fireEvent.change(field(getByTestId), { target: { value: "sgdb_key" } });
      expect(getByText("Save")).not.toBeDisabled();
    });

    it("keeps Save disabled for a whitespace-only value", () => {
      const { getByTestId, getByText } = render(<SgdbApiKeyModal onVerify={verifyOk()} onSave={saveOk()} />);
      fireEvent.change(field(getByTestId), { target: { value: "   " } });
      expect(getByText("Save")).toBeDisabled();
    });

    it("does not verify when Save is clicked with an empty field (disabled swallows the click)", () => {
      const onVerify = verifyOk();
      const { getByText } = render(<SgdbApiKeyModal onVerify={onVerify} onSave={saveOk()} />);
      fireEvent.click(getByText("Save"));
      expect(onVerify).not.toHaveBeenCalled();
    });
  });

  describe("success path (valid key)", () => {
    it("verifies the entered key, then saves it, then closes — no error shown", async () => {
      const closeModal = vi.fn();
      const onVerify = verifyOk();
      const onSave = saveOk();
      const { getByTestId, getByText, queryByTestId } = render(
        <SgdbApiKeyModal closeModal={closeModal} onVerify={onVerify} onSave={onSave} />,
      );
      fireEvent.change(field(getByTestId), { target: { value: "good_key" } });
      fireEvent.click(getByText("Save"));
      await flushSubmit();
      expect(onVerify).toHaveBeenCalledTimes(1);
      expect(onVerify).toHaveBeenCalledWith("good_key");
      expect(onSave).toHaveBeenCalledTimes(1);
      expect(onSave).toHaveBeenCalledWith("good_key");
      expect(closeModal).toHaveBeenCalledTimes(1);
      expect(queryByTestId("sgdb-key-error")).toBeNull();
    });
  });

  describe("failure path (modal stays open, key not saved)", () => {
    it("shows the returned message and does NOT save when verification fails", async () => {
      const closeModal = vi.fn();
      const onVerify = verifyFail("API key rejected by SteamGridDB");
      const onSave = saveOk();
      const { getByTestId, getByText } = render(
        <SgdbApiKeyModal closeModal={closeModal} onVerify={onVerify} onSave={onSave} />,
      );
      fireEvent.change(field(getByTestId), { target: { value: "bad_key" } });
      fireEvent.click(getByText("Save"));
      await flushSubmit();
      expect(getByTestId("sgdb-key-error").textContent).toBe("API key rejected by SteamGridDB");
      expect(onSave).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
    });

    it("shows a generic error and stays open when verification rejects", async () => {
      const closeModal = vi.fn();
      const onVerify = vi.fn().mockRejectedValue(new Error("network"));
      const onSave = saveOk();
      const { getByTestId, getByText } = render(
        <SgdbApiKeyModal closeModal={closeModal} onVerify={onVerify} onSave={onSave} />,
      );
      fireEvent.change(field(getByTestId), { target: { value: "any_key" } });
      fireEvent.click(getByText("Save"));
      await flushSubmit();
      expect(getByTestId("sgdb-key-error").textContent).toBe(
        "Could not verify the key. Check your connection and try again.",
      );
      expect(onSave).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
    });

    it("shows a save-specific error (not the verify error) and stays open when the save rejects", async () => {
      const closeModal = vi.fn();
      const onVerify = verifyOk();
      const onSave = vi.fn().mockRejectedValue(new Error("disk full"));
      const { getByTestId, getByText } = render(
        <SgdbApiKeyModal closeModal={closeModal} onVerify={onVerify} onSave={onSave} />,
      );
      fireEvent.change(field(getByTestId), { target: { value: "good_key" } });
      fireEvent.click(getByText("Save"));
      await flushSubmit();
      expect(onSave).toHaveBeenCalledTimes(1);
      // The verify succeeded, so the message must not claim it couldn't be verified.
      expect(getByTestId("sgdb-key-error").textContent).toBe(
        "The key is valid, but saving it failed. Check your connection and try again.",
      );
      expect(closeModal).not.toHaveBeenCalled();
    });

    it("clears a stale error when the user edits the field", async () => {
      const onVerify = verifyFail("API key rejected by SteamGridDB");
      const { getByTestId, getByText, queryByTestId } = render(
        <SgdbApiKeyModal onVerify={onVerify} onSave={saveOk()} />,
      );
      fireEvent.change(field(getByTestId), { target: { value: "bad" } });
      fireEvent.click(getByText("Save"));
      await flushSubmit();
      expect(getByTestId("sgdb-key-error")).toBeTruthy();
      fireEvent.change(field(getByTestId), { target: { value: "bad2" } });
      expect(queryByTestId("sgdb-key-error")).toBeNull();
    });
  });

  describe("Enter key", () => {
    it("submits on Enter once the field is non-empty and closes on success, consuming the event", async () => {
      const closeModal = vi.fn();
      const onVerify = verifyOk();
      const onSave = saveOk();
      const { getByTestId } = render(<SgdbApiKeyModal closeModal={closeModal} onVerify={onVerify} onSave={onSave} />);
      fireEvent.change(field(getByTestId), { target: { value: "good_key" } });
      // fireEvent returns false when a handler called preventDefault — the Enter
      // is consumed so Steam's ModalRoot cannot fire its own default close.
      const notPrevented = fireEvent.keyDown(field(getByTestId), { key: "Enter" });
      expect(notPrevented).toBe(false);
      await flushSubmit();
      expect(onVerify).toHaveBeenCalledWith("good_key");
      expect(onSave).toHaveBeenCalledWith("good_key");
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("ignores Enter on an empty field (no verify, no close) but still consumes the event", () => {
      const closeModal = vi.fn();
      const onVerify = verifyOk();
      const { getByTestId } = render(<SgdbApiKeyModal closeModal={closeModal} onVerify={onVerify} onSave={saveOk()} />);
      const notPrevented = fireEvent.keyDown(field(getByTestId), { key: "Enter" });
      expect(notPrevented).toBe(false);
      expect(onVerify).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
    });
  });
});
