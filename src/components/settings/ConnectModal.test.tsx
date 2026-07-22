import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement, forwardRef } from "react";
import { ConnectModal } from "./ConnectModal";

// Local @decky/ui mock: ConfirmModal exposes its OK button (driving onOK) so
// the submit path is exercised; TextField forwards label + value + onChange +
// onKeyDown to a real <input> (mirroring the real component's typed contract —
// TextFieldProps extends HTMLAttributes<HTMLInputElement>) so each field can be
// typed into, receive key events, and be focused; DropdownItem renders one
// button per option (data-testid `mode-<data>`) that drives onChange so a test
// can flip the sign-in mode.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface ConfirmModalProps extends AnyProps {
  onOK?: () => void;
  strOKButtonText?: string;
}
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
  // Focusable forwards its ref to the wrapping div so the component's
  // querySelectorAll('input') focus-advance resolves against the boxes.
  Focusable: forwardRef<HTMLDivElement, AnyProps>((p, ref) =>
    createElement(
      "div",
      { ref, style: p.style, "data-testid": p["data-testid"] as string | undefined },
      p.children as never,
    ),
  ),
  ConfirmModal: (p: ConfirmModalProps) =>
    createElement(
      "div",
      {},
      p.children as never,
      createElement("button", { "data-testid": "ok-button", onClick: () => p.onOK?.() }, p.strOKButtonText ?? "OK"),
    ),
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

describe("ConnectModal", () => {
  beforeEach(() => {
    textFields.length = 0;
    vi.clearAllMocks();
  });

  it("lists the sign-in methods in order: pairing, token, credentials", () => {
    const { getByTestId } = render(
      <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
    );
    const options = Array.from(getByTestId("mode-dropdown").querySelectorAll("button")).map((b) => b.textContent);
    expect(options).toEqual(["Pairing code", "API token", "Username & password"]);
  });

  it("opens in pairing mode by default", () => {
    const { getByTestId, queryByTestId } = render(
      <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
    );
    // No dropdown interaction — the pairing-code boxes are the ones shown on first render.
    expect(getByTestId("mode-dropdown").getAttribute("data-selected")).toBe("pairing");
    expect(getByTestId("pairing-code-row").querySelectorAll("input")).toHaveLength(8);
    expect(queryByTestId("field-Username")).toBeNull();
    expect(queryByTestId("field-API Token")).toBeNull();
  });

  describe("credentials mode", () => {
    it("renders a username and an obscured password field, both empty (write-only)", () => {
      const { getByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      const user = getByTestId("field-Username") as HTMLInputElement;
      const pass = getByTestId("field-Password") as HTMLInputElement;
      expect(user.value).toBe("");
      expect(pass.value).toBe("");
      expect(pass.getAttribute("data-is-password")).toBe("true");
    });

    it("calls onConnect with the entered username + password on Sign in", () => {
      const onConnect = vi.fn();
      const onConnectToken = vi.fn();
      const onConnectPairing = vi.fn();
      const { getByTestId } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={onConnectPairing} />,
      );

      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.click(getByTestId("ok-button"));

      expect(onConnect).toHaveBeenCalledTimes(1);
      expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
      expect(onConnectToken).not.toHaveBeenCalled();
    });

    it("submits on Enter in the username field (on-screen keyboard) and closes", () => {
      const onConnect = vi.fn();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={onConnect}
          onConnectToken={vi.fn()}
          onConnectPairing={vi.fn()}
        />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.keyDown(getByTestId("field-Username"), { key: "Enter" });
      expect(onConnect).toHaveBeenCalledTimes(1);
      expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
      // ConfirmModal's OK auto-closes; the manual Enter path must close itself.
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("submits on Enter in the password field", () => {
      const onConnect = vi.fn();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={onConnect}
          onConnectToken={vi.fn()}
          onConnectPairing={vi.fn()}
        />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.keyDown(getByTestId("field-Password"), { key: "Enter" });
      expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("passes empty strings to onConnect when nothing is entered", () => {
      const onConnect = vi.fn();
      const { getByTestId } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnect).toHaveBeenCalledWith("", "");
    });

    it("does not render the API token field until token mode is selected", () => {
      const { getByTestId, queryByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-credentials"));
      expect(queryByTestId("field-API Token")).toBeNull();
    });
  });

  describe("token mode", () => {
    it("shows an obscured API token field and hides the credential fields after switching", () => {
      const { getByTestId, queryByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      const token = getByTestId("field-API Token") as HTMLInputElement;
      expect(token.getAttribute("data-is-password")).toBe("true");
      expect(queryByTestId("field-Username")).toBeNull();
      expect(queryByTestId("field-Password")).toBeNull();
    });

    it("calls onConnectToken with the entered token on Sign in", () => {
      const onConnect = vi.fn();
      const onConnectToken = vi.fn();
      const onConnectPairing = vi.fn();
      const { getByTestId } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={onConnectPairing} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnectToken).toHaveBeenCalledTimes(1);
      expect(onConnectToken).toHaveBeenCalledWith("rmm_pasted");
      expect(onConnect).not.toHaveBeenCalled();
    });

    it("submits the token on Enter in the API token field (on-screen keyboard) and closes", () => {
      const onConnectToken = vi.fn();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={vi.fn()}
          onConnectToken={onConnectToken}
          onConnectPairing={vi.fn()}
        />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.keyDown(getByTestId("field-API Token"), { key: "Enter" });
      expect(onConnectToken).toHaveBeenCalledTimes(1);
      expect(onConnectToken).toHaveBeenCalledWith("rmm_pasted");
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("switches back to credentials mode and calls onConnect again", () => {
      const onConnect = vi.fn();
      const onConnectToken = vi.fn();
      const onConnectPairing = vi.fn();
      const { getByTestId } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={onConnectPairing} />,
      );
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnect).toHaveBeenCalledWith("daniel", "");
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
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
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
      const { getByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "a" } });
      expect(box(getByTestId, 0).value).toBe("A");
      // Auto-advance: the character hands the caret to the next box.
      expect(document.activeElement).toBe(box(getByTestId, 1));
    });

    it("ignores a non-alphanumeric character, leaving the box empty and focus put", () => {
      const { getByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "!" } });
      expect(box(getByTestId, 0).value).toBe("");
      // Nothing entered, so focus does not advance.
      expect(document.activeElement).not.toBe(box(getByTestId, 1));
    });

    it("distributes a two-character entry across two boxes (never crams two into one)", () => {
      const { getByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "zq" } });
      expect(box(getByTestId, 0).value).toBe("Z");
      expect(box(getByTestId, 1).value).toBe("Q");
      expect(box(getByTestId, 2).value).toBe("");
    });

    it("returns focus to the previous box when Backspace is pressed on an empty box", () => {
      const { getByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
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
      const { getByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: pasted } });
      expect(codeBoxes(getByTestId).map((b) => b.value)).toEqual([...expected]);
    });

    it("calls onConnectPairing with the concatenated eight characters on Sign in and leaks to no other handler", () => {
      const onConnect = vi.fn();
      const onConnectToken = vi.fn();
      const onConnectPairing = vi.fn();
      const { getByTestId } = render(
        <ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} onConnectPairing={onConnectPairing} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.change(box(getByTestId, 0), { target: { value: "abcdefgh" } });
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnectPairing).toHaveBeenCalledTimes(1);
      // Bare eight characters in order — the backend normalizes, so no hyphen is sent.
      expect(onConnectPairing).toHaveBeenCalledWith("ABCDEFGH");
      expect(onConnect).not.toHaveBeenCalled();
      expect(onConnectToken).not.toHaveBeenCalled();
    });

    it("submits the pairing code on Enter once all eight boxes are filled and closes", () => {
      const onConnectPairing = vi.fn();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={vi.fn()}
          onConnectToken={vi.fn()}
          onConnectPairing={onConnectPairing}
        />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      // A full paste fills all eight boxes.
      fireEvent.change(box(getByTestId, 0), { target: { value: "abcdefgh" } });
      fireEvent.keyDown(box(getByTestId, 7), { key: "Enter" });
      expect(onConnectPairing).toHaveBeenCalledTimes(1);
      expect(onConnectPairing).toHaveBeenCalledWith("ABCDEFGH");
      expect(closeModal).toHaveBeenCalledTimes(1);
    });

    it("ignores Enter while the pairing code is incomplete (no submit, no close)", () => {
      const onConnectPairing = vi.fn();
      const closeModal = vi.fn();
      const { getByTestId } = render(
        <ConnectModal
          closeModal={closeModal}
          onConnect={vi.fn()}
          onConnectToken={vi.fn()}
          onConnectPairing={onConnectPairing}
        />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      // Only the first four boxes are filled — the code is incomplete.
      fireEvent.change(box(getByTestId, 0), { target: { value: "abcd" } });
      fireEvent.keyDown(box(getByTestId, 3), { key: "Enter" });
      expect(onConnectPairing).not.toHaveBeenCalled();
      expect(closeModal).not.toHaveBeenCalled();
    });

    it("switches from pairing back to token mode and routes to onConnectToken only", () => {
      const onConnectToken = vi.fn();
      const onConnectPairing = vi.fn();
      const { getByTestId } = render(
        <ConnectModal onConnect={vi.fn()} onConnectToken={onConnectToken} onConnectPairing={onConnectPairing} />,
      );
      fireEvent.click(getByTestId("mode-pairing"));
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnectToken).toHaveBeenCalledWith("rmm_pasted");
      expect(onConnectPairing).not.toHaveBeenCalled();
    });
  });

  it("uses 'Sign in' as the OK button label", () => {
    const { getByTestId } = render(
      <ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} onConnectPairing={vi.fn()} />,
    );
    expect(getByTestId("ok-button").textContent).toBe("Sign in");
  });
});
