import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { ConnectModal } from "./ConnectModal";

// Local @decky/ui mock: ConfirmModal exposes its OK button (driving onOK) so
// the submit path is exercised; TextField forwards label + value + onChange so
// each field can be typed into and identified by its label; DropdownItem
// renders one button per option (data-testid `mode-<data>`) that drives onChange
// so a test can flip the sign-in mode.
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

  describe("credentials mode (default)", () => {
    it("renders a username and an obscured password field, both empty (write-only)", () => {
      const { getByTestId } = render(<ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} />);
      const user = getByTestId("field-Username") as HTMLInputElement;
      const pass = getByTestId("field-Password") as HTMLInputElement;
      expect(user.value).toBe("");
      expect(pass.value).toBe("");
      expect(pass.getAttribute("data-is-password")).toBe("true");
    });

    it("calls onConnect with the entered username + password on Sign in", () => {
      const onConnect = vi.fn();
      const onConnectToken = vi.fn();
      const { getByTestId } = render(<ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} />);

      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
      fireEvent.click(getByTestId("ok-button"));

      expect(onConnect).toHaveBeenCalledTimes(1);
      expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
      expect(onConnectToken).not.toHaveBeenCalled();
    });

    it("passes empty strings to onConnect when nothing is entered", () => {
      const onConnect = vi.fn();
      const { getByTestId } = render(<ConnectModal onConnect={onConnect} onConnectToken={vi.fn()} />);
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnect).toHaveBeenCalledWith("", "");
    });

    it("does not render the API token field until token mode is selected", () => {
      const { queryByTestId } = render(<ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} />);
      expect(queryByTestId("field-API Token")).toBeNull();
    });
  });

  describe("token mode", () => {
    it("shows an obscured API token field and hides the credential fields after switching", () => {
      const { getByTestId, queryByTestId } = render(<ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} />);
      fireEvent.click(getByTestId("mode-token"));
      const token = getByTestId("field-API Token") as HTMLInputElement;
      expect(token.getAttribute("data-is-password")).toBe("true");
      expect(queryByTestId("field-Username")).toBeNull();
      expect(queryByTestId("field-Password")).toBeNull();
    });

    it("calls onConnectToken with the entered token on Sign in", () => {
      const onConnect = vi.fn();
      const onConnectToken = vi.fn();
      const { getByTestId } = render(<ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} />);
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.change(getByTestId("field-API Token"), { target: { value: "rmm_pasted" } });
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnectToken).toHaveBeenCalledTimes(1);
      expect(onConnectToken).toHaveBeenCalledWith("rmm_pasted");
      expect(onConnect).not.toHaveBeenCalled();
    });

    it("switches back to credentials mode and calls onConnect again", () => {
      const onConnect = vi.fn();
      const onConnectToken = vi.fn();
      const { getByTestId } = render(<ConnectModal onConnect={onConnect} onConnectToken={onConnectToken} />);
      fireEvent.click(getByTestId("mode-token"));
      fireEvent.click(getByTestId("mode-credentials"));
      fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
      fireEvent.click(getByTestId("ok-button"));
      expect(onConnect).toHaveBeenCalledWith("daniel", "");
      expect(onConnectToken).not.toHaveBeenCalled();
    });
  });

  it("uses 'Sign in' as the OK button label", () => {
    const { getByTestId } = render(<ConnectModal onConnect={vi.fn()} onConnectToken={vi.fn()} />);
    expect(getByTestId("ok-button").textContent).toBe("Sign in");
  });
});
