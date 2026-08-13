import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, forwardRef } from "react";
import { ConnectModal } from "./ConnectModal";

// Local @decky/ui mock: ModalRoot passes its children through (the modal owns its
// own footer buttons); DialogButton renders a real <button> forwarding onClick +
// disabled + children so the Sign in / Cancel buttons are clickable and their
// disabled state is assertable (a disabled button swallows the click in happy-dom,
// mirroring the real gate); TextField forwards label + value + onChange +
// onKeyDown to a real <input> (mirroring the real component's typed contract —
// TextFieldProps extends HTMLAttributes<HTMLInputElement>) so each field can be
// typed into, receive key events, and be focused; DropdownItem renders one button
// per option (data-testid `mode-<data>`) that drives onChange so a test can flip
// the sign-in mode.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface TextFieldProps {
  label?: string;
  value?: string;
  bIsPassword?: boolean;
  onChange?: (e: { target: { value: string } }) => void;
  onKeyDown?: (e: unknown) => void;
}
interface DropdownOption {
  data: string;
  label: string;
}
interface DropdownItemProps {
  label?: string;
  rgOptions?: DropdownOption[];
  selectedOption?: string;
  onChange?: (option: DropdownOption) => void;
}

const textFields: TextFieldProps[] = [];

vi.mock("@decky/ui", () => ({
  // ModalRoot is the bare shell — it renders its children and nothing else (no
  // strTitle, no OK button), so the modal supplies its own footer.
  ModalRoot: (p: AnyProps) => createElement("div", { "data-testid": "modal-root" }, p.children as never),
  // Focusable forwards its ref to the wrapping div so the component's
  // querySelectorAll('input') focus-advance resolves against the boxes.
  Focusable: forwardRef<HTMLDivElement, AnyProps>((p, ref) =>
    createElement(
      "div",
      { ref, style: p.style, "data-testid": p["data-testid"] as string | undefined },
      p.children as never,
    ),
  ),
  DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children as never),
  TextField: (p: TextFieldProps) => {
    textFields.push(p);
    return createElement("input", {
      "data-testid": `field-${p.label ?? ""}`,
      "data-is-password": p.bIsPassword ? "true" : "false",
      value: p.value ?? "",
      onChange: (e: unknown) => p.onChange?.(e as { target: { value: string } }),
      onKeyDown: (e: unknown) => p.onKeyDown?.(e),
    });
  },
  DropdownItem: (p: DropdownItemProps) =>
    createElement(
      "div",
      { "data-testid": "mode-dropdown", "data-selected": p.selectedOption },
      (p.rgOptions ?? []).map((o) =>
        createElement(
          "button",
          { key: o.data, "data-testid": `mode-${o.data}`, onClick: () => p.onChange?.(o) },
          o.label,
        ),
      ),
    ),
}));

// Resolved-success default so a click that reaches submit() closes the modal.
const ok = (message = "Connected!") => vi.fn().mockResolvedValue({ success: true, message });
const fail = (message: string) => vi.fn().mockResolvedValue({ success: false, message });

// Flush the floating submit() promise (onClick fires `void submit()`), then let
// React apply the resulting state updates. Two microtask ticks cover the single
// `await onConnect(...)` plus its finally block.
async function flushSubmit() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

const signInButton = (getByText: (t: string) => HTMLElement): HTMLElement => getByText("Sign in");

describe("ConnectModal", () => {
  beforeEach(() => {
    textFields.length = 0;
    vi.clearAllMocks();
  });

  it("lists the sign-in methods in order: pairing, token, credentials", () => {
    const { getByTestId } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
    const options = Array.from(getByTestId("mode-dropdown").querySelectorAll("button")).map((b) => b.textContent);
    expect(options).toEqual(["Pairing code", "API token", "Username & password"]);
  });

  it("opens in pairing mode by default", () => {
    const { getByTestId, queryByTestId } = render(
      <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
    );
    // No dropdown interaction — the pairing-code boxes are the ones shown on first render.
    expect(getByTestId("mode-dropdown").getAttribute("data-selected")).toBe("pairing");
    expect(getByTestId("pairing-code-row").querySelectorAll("input")).toHaveLength(8);
    expect(queryByTestId("field-Username")).toBeNull();
    expect(queryByTestId("field-API Token")).toBeNull();
  });

  it("renders a Sign in and a Cancel footer button; Cancel closes the modal", () => {
    const closeModal = vi.fn();
    const { getByText } = render(
      <ConnectModal closeModal={closeModal} onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
    );
    expect(getByText("Sign in")).toBeTruthy();
    fireEvent.click(getByText("Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
  });

  describe("Sign in gating (fields required before submit)", () => {
    it("disables Sign in in pairing mode until all eight boxes are filled", () => {
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      const inputs = () => getByTestId("pairing-code-row").querySelectorAll("input");
      expect(signInButton(getByText)).toBeDisabled();
      // A partial code (first four boxes) is still not enough.
      fireEvent.change(inputs()[0]!, { target: { value: "abcd" } });
      expect(signInButton(getByText)).toBeDisabled();
      // Fill the remaining four (the paste left focus on box 4) — now enabled.
      fireEvent.change(inputs()[4]!, { target: { value: "efgh" } });
      expect(signInButton(getByText)).not.toBeDisabled();
    });

    it("disables Sign in in token mode until the token is non-empty", () => {
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      expect(signInButton(getByText)).toBeDisabled();
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      expect(signInButton(getByText)).not.toBeDisabled();
    });

    it("disables Sign in in credentials mode until both username and password are filled", () => {
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      expect(signInButton(getByText)).toBeDisabled();
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      // Username alone is not enough.
      expect(signInButton(getByText)).toBeDisabled();
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      expect(signInButton(getByText)).not.toBeDisabled();
    });

    it("keeps Sign in disabled for a whitespace-only username or token, and a blank password", () => {
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      // Whitespace-only token — trimmed to empty, stays disabled.
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "   " } });
      expect(signInButton(getByText)).toBeDisabled();
      // Whitespace-only username + a real password — username trims to empty.
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "   " } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      expect(signInButton(getByText)).toBeDisabled();
      // A real username but a blank password — password blank stays disabled.
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "" } });
      expect(signInButton(getByText)).toBeDisabled();
    });

    it("allows a password made only of spaces (password is not trimmed)", () => {
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "   " } });
      expect(signInButton(getByText)).not.toBeDisabled();
    });

    it("does not call onConnect when Sign in is clicked with empty fields (disabled swallows the click)", () => {
      const onConnect = ok();
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.click(signInButton(getByText));
      expect(onConnect).not.toHaveBeenCalled();
    });
  });

  describe("credentials mode", () => {
    it("renders a username and an obscured password field, both empty (write-only)", () => {
      const { getByTestId } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
      fireEvent.click(getByTestId("mode-credentials"));
      const user = getByTestId("field-Username") as HTMLInputElement;
      const pass = getByTestId("field-Password") as HTMLInputElement;
      expect(user.value).toBe("");
      expect(pass.value).toBe("");
      expect(pass.getAttribute("data-is-password")).toBe("true");
    });

    it("calls onConnect with the entered username + password on Sign in", async () => {
      const onConnect = ok();
      const onConnectToken = ok();
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={ok()} />,
      );

      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();

      expect(onConnect).toHaveBeenCalledTimes(1);
      expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
      expect(onConnectToken).not.toHaveBeenCalled();
    });

    it("advances to the password field on Enter in the username field and does NOT submit", () => {
      const onConnect = ok();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal closeModal={closeModal} onConnect={onConnect} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      // Both fields filled, so a wrongful submit WOULD be possible — Enter on the
      // first field must still advance, never fire the action.
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.keyDown(getByTestId("field-Username"), { key: "Enter" });
      expect(onConnect).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
      // Focus moved to the password field.
      expect(document.activeElement).toBe(getByTestId("field-Password"));
    });

    it("submits on Enter in the password field once both fields are filled and closes on success", async () => {
      const onConnect = ok();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal closeModal={closeModal} onConnect={onConnect} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.keyDown(getByTestId("field-Password"), { key: "Enter" });
      await flushSubmit();
      expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("ignores Enter in the password field while the fields are incomplete", () => {
      const onConnect = ok();
      const { getByTestId } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      // Password entered but username still blank — the mode is not submittable.
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.keyDown(getByTestId("field-Password"), { key: "Enter" });
      expect(onConnect).not.toHaveBeenCalled();
    });

    it("does not render the API token field until token mode is selected", () => {
      const { getByTestId, queryByTestId } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      expect(queryByTestId("field-API Token")).toBeNull();
    });
  });

  describe("token mode", () => {
    it("shows an obscured API token field and hides the credential fields after switching", () => {
      const { getByTestId, queryByTestId } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      const token = getByTestId("field-API Token") as HTMLInputElement;
      expect(token.getAttribute("data-is-password")).toBe("true");
      expect(queryByTestId("field-Username")).toBeNull();
      expect(queryByTestId("field-Password")).toBeNull();
    });

    it("calls onConnectToken with the entered token on Sign in", async () => {
      const onConnect = ok();
      const onConnectToken = ok();
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(onConnectToken).toHaveBeenCalledTimes(1);
      expect(onConnectToken).toHaveBeenCalledWith("rmm_pasted");
      expect(onConnect).not.toHaveBeenCalled();
    });

    it("submits the token on Enter in the API token field and closes on success", async () => {
      const onConnectToken = ok();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={ok()}
          onConnectToken={onConnectToken}
          onConnectPairing={ok()}
        />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.keyDown(getByTestId("field-API Token"), { key: "Enter" });
      await flushSubmit();
      expect(onConnectToken).toHaveBeenCalledTimes(1);
      expect(onConnectToken).toHaveBeenCalledWith("rmm_pasted");
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("ignores Enter on an empty token field and consumes the event so the modal cannot close", () => {
      const onConnectToken = ok();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={ok()}
          onConnectToken={onConnectToken}
          onConnectPairing={ok()}
        />,
      );
      fireEvent.click(getByTestId("mode-token"));
      // fireEvent returns false when a handler called preventDefault. The handler
      // must consume the Enter so Steam's ModalRoot cannot fire its own default
      // confirm/close on the empty field — the Deck regression. The real close
      // can't be reproduced in happy-dom, so a consumed event is the observable
      // proxy that the fix is in place, alongside the no-submit/no-close effects.
      const notPrevented = fireEvent.keyDown(getByTestId("field-API Token"), { key: "Enter" });
      expect(notPrevented).toBe(false);
      expect(onConnectToken).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
    });

    it("switches back to credentials mode and calls onConnect again", async () => {
      const onConnect = ok();
      const onConnectToken = ok();
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
      expect(onConnectToken).not.toHaveBeenCalled();
    });
  });

  describe("pairing mode", () => {
    // The eight single-character boxes have no per-box label, so the test grabs
    // each underlying input the same way the component moves focus between them:
    // querySelectorAll('input') on the row wrapper, in DOM order. The @decky/ui
    // TextField exposes no ref to its input.
    const codeBoxes = (getByTestId: (id: string) => HTMLElement): HTMLInputElement[] =>
      Array.from(getByTestId("pairing-code-row").querySelectorAll("input"));
    const box = (getByTestId: (id: string) => HTMLElement, index: number): HTMLInputElement => {
      const input = codeBoxes(getByTestId)[index];
      if (!input) throw new Error(`no pairing box at index ${index}`);
      return input;
    };

    it("renders exactly eight single-character (non-obscured) code boxes and hides the other modes' fields", () => {
      const { getByTestId, queryByTestId } = render(
        <ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      const boxes = codeBoxes(getByTestId);
      expect(boxes).toHaveLength(8);
      // The code is short-lived and single-use — typability beats obscuring it.
      for (const box of boxes) {
        expect(box.getAttribute("data-is-password")).toBe("false");
      }
      expect(queryByTestId("field-Username")).toBeNull();
      expect(queryByTestId("field-Password")).toBeNull();
      expect(queryByTestId("field-API Token")).toBeNull();
    });

    it("uppercases a typed character and advances focus to the next box", () => {
      const { getByTestId } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "a" } });
      expect(box(getByTestId, 0).value).toBe("A");
      // Auto-advance: the character hands the caret to the next box.
      expect(document.activeElement).toBe(box(getByTestId, 1));
    });

    it("ignores a non-alphanumeric character, leaving the box empty and focus put", () => {
      const { getByTestId } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "!" } });
      expect(box(getByTestId, 0).value).toBe("");
      // Nothing entered, so focus does not advance.
      expect(document.activeElement).not.toBe(box(getByTestId, 1));
    });

    it("distributes a two-character entry across two boxes (never crams two into one)", () => {
      const { getByTestId } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "zq" } });
      expect(box(getByTestId, 0).value).toBe("Z");
      expect(box(getByTestId, 1).value).toBe("Q");
      expect(box(getByTestId, 2).value).toBe("");
    });

    it("returns focus to the previous box when Backspace is pressed on an empty box", () => {
      const { getByTestId } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
      fireEvent.click(getByTestId("mode-pairing"));
      // Fill the first box (auto-advances to the second, which stays empty).
      fireEvent.change(box(getByTestId, 0), { target: { value: "a" } });
      expect(document.activeElement).toBe(box(getByTestId, 1));
      // Backspace on the empty second box steps back to the first.
      fireEvent.keyDown(box(getByTestId, 1), { key: "Backspace" });
      expect(document.activeElement).toBe(box(getByTestId, 0));
    });

    it.each([
      ["a plain eight-character paste", "abcdefgh", ["A", "B", "C", "D", "E", "F", "G", "H"]],
      ["a hyphenated eight-character paste", "abcd-efgh", ["A", "B", "C", "D", "E", "F", "G", "H"]],
      ["a mixed-case paste (uppercased as it fills)", "aB2d-Ef4h", ["A", "B", "2", "D", "E", "F", "4", "H"]],
    ] as const)("splits %s across all eight boxes", (_name, pasted, expected) => {
      const { getByTestId } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: pasted } });
      expect(codeBoxes(getByTestId).map((b) => b.value)).toEqual([...expected]);
    });

    it("calls onConnectPairing with the concatenated eight characters on Sign in and leaks to no other handler", async () => {
      const onConnect = ok();
      const onConnectToken = ok();
      const onConnectPairing = ok();
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={onConnectPairing} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "abcdefgh" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(onConnectPairing).toHaveBeenCalledTimes(1);
      // Bare eight characters in order — the backend normalizes, so no hyphen is sent.
      expect(onConnectPairing).toHaveBeenCalledWith("ABCDEFGH");
      expect(onConnect).not.toHaveBeenCalled();
      expect(onConnectToken).not.toHaveBeenCalled();
    });

    it("submits the pairing code on Enter once all eight boxes are filled and closes on success", async () => {
      const onConnectPairing = ok();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={ok()}
          onConnectToken={ok()}
          onConnectPairing={onConnectPairing}
        />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      // A full paste fills all eight boxes.
      fireEvent.change(box(getByTestId, 0), { target: { value: "abcdefgh" } });
      fireEvent.keyDown(box(getByTestId, 7), { key: "Enter" });
      await flushSubmit();
      expect(onConnectPairing).toHaveBeenCalledTimes(1);
      expect(onConnectPairing).toHaveBeenCalledWith("ABCDEFGH");
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("ignores Enter while the pairing code is incomplete (no submit, no close)", async () => {
      const onConnectPairing = ok();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={ok()}
          onConnectToken={ok()}
          onConnectPairing={onConnectPairing}
        />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      // Only the first four boxes are filled — the code is incomplete.
      fireEvent.change(box(getByTestId, 0), { target: { value: "abcd" } });
      fireEvent.keyDown(box(getByTestId, 3), { key: "Enter" });
      await flushSubmit();
      expect(onConnectPairing).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
    });

    it("switches from pairing back to token mode and routes to onConnectToken only", async () => {
      const onConnectToken = ok();
      const onConnectPairing = ok();
      const { getByTestId, getByText } = render(
        <ConnectModal onConnect={ok()} onConnectToken={onConnectToken} onConnectPairing={onConnectPairing} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(onConnectToken).toHaveBeenCalledWith("rmm_pasted");
      expect(onConnectPairing).not.toHaveBeenCalled();
    });
  });

  describe("success vs failure (modal stays open on failure)", () => {
    it("closes the modal and shows no error when the connect handler resolves success", async () => {
      const closeModal = vi.fn();
      const { getByTestId, getByText, queryByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={ok()}
          onConnectToken={ok("Signed in!")}
          onConnectPairing={ok()}
        />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(closeModal).toHaveBeenCalledTimes(1);
      expect(queryByTestId("signin-error")).toBeNull();
    });

    it("stays open and shows the returned message when the connect handler resolves failure", async () => {
      const closeModal = vi.fn();
      const onConnectToken = fail("The API token is missing required permissions (scopes).");
      const { getByTestId, getByText } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={ok()}
          onConnectToken={onConnectToken}
          onConnectPairing={ok()}
        />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_readonly" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(closeModal).not.toHaveBeenCalled();
      expect(getByTestId("signin-error").textContent).toBe("The API token is missing required permissions (scopes).");
    });

    it("shows a generic error and stays open when the connect handler rejects", async () => {
      const closeModal = vi.fn();
      const onConnectToken = vi.fn().mockRejectedValue(new Error("network"));
      const { getByTestId, getByText } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={ok()}
          onConnectToken={onConnectToken}
          onConnectPairing={ok()}
        />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(closeModal).not.toHaveBeenCalled();
      expect(getByTestId("signin-error").textContent).toBe("Sign-in failed. Check your connection and try again.");
    });

    // Decky's callable() stays pending forever against a downed backend, so the
    // never-settling handler here is the real shape of that failure, not a
    // contrived one.
    it("shows a backend-never-answered error and re-enables Sign in when the call never settles", async () => {
      vi.useFakeTimers();
      try {
        const closeModal = vi.fn();
        const onConnectToken = vi.fn().mockReturnValue(new Promise(() => {}));
        const { getByTestId, getByText } = render(
          <ConnectModal
            closeModal={closeModal}
            onConnect={ok()}
            onConnectToken={onConnectToken}
            onConnectPairing={ok()}
          />,
        );
        fireEvent.click(getByTestId("mode-token"));
        fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
        fireEvent.click(signInButton(getByText));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(60_000);
        });
        expect(closeModal).not.toHaveBeenCalled();
        expect(getByTestId("signin-error").textContent).toBe(
          "The plugin backend never answered. Reload Decky or restart Steam, then try again.",
        );
        // Back out of the in-flight state so the deadline is an exit, not a
        // second dead end.
        expect(signInButton(getByText)).toBeTruthy();
      } finally {
        vi.useRealTimers();
      }
    });

    it("still closes on a slow sign-in that answers before the deadline", async () => {
      vi.useFakeTimers();
      try {
        const closeModal = vi.fn();
        const onConnectToken = vi.fn().mockReturnValue(
          new Promise((resolve) => {
            setTimeout(() => resolve({ success: true, message: "Connected!" }), 45_000);
          }),
        );
        const { getByTestId, getByText, queryByTestId } = render(
          <ConnectModal
            closeModal={closeModal}
            onConnect={ok()}
            onConnectToken={onConnectToken}
            onConnectPairing={ok()}
          />,
        );
        fireEvent.click(getByTestId("mode-token"));
        fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
        fireEvent.click(signInButton(getByText));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(45_000);
        });
        expect(closeModal).toHaveBeenCalled();
        expect(queryByTestId("signin-error")).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it("clears a stale error when the user edits a field", async () => {
      const onConnectToken = fail("Pairing code is invalid or has expired.");
      const { getByTestId, getByText, queryByTestId } = render(
        <ConnectModal onConnect={ok()} onConnectToken={onConnectToken} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "bad" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(getByTestId("signin-error")).toBeTruthy();
      // Editing the field clears the stale error.
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "bad2" } });
      expect(queryByTestId("signin-error")).toBeNull();
    });

    it("clears a stale error when the user switches mode", async () => {
      const onConnectToken = fail("bad token");
      const { getByTestId, getByText, queryByTestId } = render(
        <ConnectModal onConnect={ok()} onConnectToken={onConnectToken} onConnectPairing={ok()} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "bad" } });
      fireEvent.click(signInButton(getByText));
      await flushSubmit();
      expect(getByTestId("signin-error")).toBeTruthy();
      fireEvent.click(getByTestId("mode-credentials"));
      expect(queryByTestId("signin-error")).toBeNull();
    });
  });

  it("labels the primary footer button 'Sign in'", () => {
    const { getByText } = render(<ConnectModal onConnect={ok()} onConnectToken={ok()} onConnectPairing={ok()} />);
    expect(signInButton(getByText).textContent).toBe("Sign in");
  });
});
