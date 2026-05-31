import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { ConnectModal } from "./ConnectModal";

// Local @decky/ui mock: ConfirmModal exposes its OK button (driving onOK) so
// the submit path is exercised; TextField forwards label + value + onChange so
// each field can be typed into and identified by its label.
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
}));

describe("ConnectModal", () => {
  beforeEach(() => {
    textFields.length = 0;
    vi.clearAllMocks();
  });

  it("renders a username and an obscured password field, both empty (write-only)", () => {
    const { getByTestId } = render(<ConnectModal onConnect={vi.fn()} />);
    const user = getByTestId("field-Username") as HTMLInputElement;
    const pass = getByTestId("field-Password") as HTMLInputElement;
    expect(user.value).toBe("");
    expect(pass.value).toBe("");
    expect(pass.getAttribute("data-is-password")).toBe("true");
  });

  it("calls onConnect with the entered username + password on Connect", () => {
    const onConnect = vi.fn();
    const { getByTestId } = render(<ConnectModal onConnect={onConnect} />);

    fireEvent.change(getByTestId("field-Username"), { target: { value: "daniel" } });
    fireEvent.change(getByTestId("field-Password"), { target: { value: "hunter2" } });
    fireEvent.click(getByTestId("ok-button"));

    expect(onConnect).toHaveBeenCalledTimes(1);
    expect(onConnect).toHaveBeenCalledWith("daniel", "hunter2");
  });

  it("passes empty strings to onConnect when nothing is entered", () => {
    const onConnect = vi.fn();
    const { getByTestId } = render(<ConnectModal onConnect={onConnect} />);
    fireEvent.click(getByTestId("ok-button"));
    expect(onConnect).toHaveBeenCalledWith("", "");
  });

  it("uses 'Connect' as the OK button label", () => {
    const { getByTestId } = render(<ConnectModal onConnect={vi.fn()} />);
    expect(getByTestId("ok-button").textContent).toBe("Connect");
  });
});
