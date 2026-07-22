import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { ValidatingModalShell } from "./ValidatingModalShell";

// Local @decky/ui mock (mirrors ConnectModal.test.tsx): ModalRoot passes its
// children through (the shell owns its own footer buttons); DialogButton renders
// a real <button> forwarding onClick + disabled + children so the primary and
// Cancel buttons are clickable and their disabled state is assertable (a disabled
// button swallows the click in happy-dom, mirroring the real gate).
type AnyProps = Record<string, unknown> & { children?: unknown };

vi.mock("@decky/ui", () => ({
  ModalRoot: (p: AnyProps) => createElement("div", { "data-testid": "modal-root" }, p.children as never),
  DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children as never),
}));

// A minimal, always-present body so children rendering is observable and distinct
// from the title.
const child = () => createElement("div", { "data-testid": "shell-child" }, "field content");

describe("ValidatingModalShell", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the title, the children, and the primary + Cancel buttons", () => {
    const { getByText, getByTestId } = render(
      <ValidatingModalShell
        title="My Title"
        error={null}
        errorTestId="shell-error"
        submitLabel="Save"
        submitDisabled={false}
        onSubmit={vi.fn()}
      >
        {child()}
      </ValidatingModalShell>,
    );
    expect(getByText("My Title")).toBeTruthy();
    expect(getByTestId("shell-child").textContent).toBe("field content");
    expect(getByText("Save")).toBeTruthy();
    expect(getByText("Cancel")).toBeTruthy();
  });

  it("hides the error line when error is null", () => {
    const { queryByTestId } = render(
      <ValidatingModalShell
        title="My Title"
        error={null}
        errorTestId="shell-error"
        submitLabel="Save"
        submitDisabled={false}
        onSubmit={vi.fn()}
      >
        {child()}
      </ValidatingModalShell>,
    );
    expect(queryByTestId("shell-error")).toBeNull();
  });

  it("shows the error under the given test id when error is non-null", () => {
    const { getByTestId } = render(
      <ValidatingModalShell
        title="My Title"
        error="Something went wrong"
        errorTestId="shell-error"
        submitLabel="Save"
        submitDisabled={false}
        onSubmit={vi.fn()}
      >
        {child()}
      </ValidatingModalShell>,
    );
    expect(getByTestId("shell-error").textContent).toBe("Something went wrong");
  });

  it("renders the given submit label on the primary button", () => {
    const { getByText, queryByText } = render(
      <ValidatingModalShell
        title="My Title"
        error={null}
        errorTestId="shell-error"
        submitLabel="Verifying…"
        submitDisabled={true}
        onSubmit={vi.fn()}
      >
        {child()}
      </ValidatingModalShell>,
    );
    expect(getByText("Verifying…")).toBeTruthy();
    expect(queryByText("Save")).toBeNull();
  });

  it("calls onSubmit when the enabled primary button is clicked", () => {
    const onSubmit = vi.fn();
    const { getByText } = render(
      <ValidatingModalShell
        title="My Title"
        error={null}
        errorTestId="shell-error"
        submitLabel="Save"
        submitDisabled={false}
        onSubmit={onSubmit}
      >
        {child()}
      </ValidatingModalShell>,
    );
    fireEvent.click(getByText("Save"));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("disables the primary button and swallows the click when submitDisabled is true", () => {
    const onSubmit = vi.fn();
    const { getByText } = render(
      <ValidatingModalShell
        title="My Title"
        error={null}
        errorTestId="shell-error"
        submitLabel="Save"
        submitDisabled={true}
        onSubmit={onSubmit}
      >
        {child()}
      </ValidatingModalShell>,
    );
    expect(getByText("Save")).toBeDisabled();
    fireEvent.click(getByText("Save"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("calls closeModal when Cancel is clicked", () => {
    const closeModal = vi.fn();
    const { getByText } = render(
      <ValidatingModalShell
        closeModal={closeModal}
        title="My Title"
        error={null}
        errorTestId="shell-error"
        submitLabel="Save"
        submitDisabled={false}
        onSubmit={vi.fn()}
      >
        {child()}
      </ValidatingModalShell>,
    );
    fireEvent.click(getByText("Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
  });

  it("does not throw when Cancel is clicked with no closeModal provided", () => {
    const { getByText } = render(
      <ValidatingModalShell
        title="My Title"
        error={null}
        errorTestId="shell-error"
        submitLabel="Save"
        submitDisabled={false}
        onSubmit={vi.fn()}
      >
        {child()}
      </ValidatingModalShell>,
    );
    expect(() => fireEvent.click(getByText("Cancel"))).not.toThrow();
  });
});
