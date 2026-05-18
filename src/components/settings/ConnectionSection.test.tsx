import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { ConnectionSection } from "./ConnectionSection";
import { showModal } from "@decky/ui";

// Local re-mock: ButtonItem must forward `disabled`; ToggleField must
// forward `checked` + a usable onChange that mirrors the global stub's
// (boolean) signature; Field renders both label + description so we can
// assert masked-password / "(not set)" copy.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface ToggleFieldProps {
  label?: unknown;
  description?: unknown;
  checked?: boolean;
  onChange?: (value: boolean) => void;
}
const toggleCaptured: { items: ToggleFieldProps[] } = { items: [] };

vi.mock("@decky/ui", () => ({
  PanelSection: (p: AnyProps) =>
    createElement("section", {}, p.children as never),
  PanelSectionRow: (p: AnyProps) =>
    createElement("div", {}, p.children as never),
  Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
    createElement(
      "div",
      { "data-testid": "field" },
      createElement("span", { "data-testid": "field-label" }, p.label as never),
      createElement("span", { "data-testid": "field-desc" }, p.description as never),
      p.children as never,
    ),
  DialogButton: ({ children, onClick }: AnyProps & { onClick?: () => void }) =>
    createElement("button", { onClick }, children as never),
  ButtonItem: ({ children, onClick, disabled }: AnyProps & {
    onClick?: () => void;
    disabled?: boolean;
  }) => createElement("button", { onClick, disabled }, children as never),
  ToggleField: (p: ToggleFieldProps) => {
    toggleCaptured.items.push(p);
    return createElement("input", {
      type: "checkbox",
      checked: p.checked ?? false,
      "data-testid": "toggle",
      onChange: (e: { target: { checked: boolean } }) => p.onChange?.(e.target.checked),
    });
  },
  showModal: vi.fn(),
}));

interface TextInputProps {
  label: string;
  value: string;
  field?: string;
  bIsPassword?: boolean;
  onSubmit: (value: string) => void;
}

function lastShownModalProps(): TextInputProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<TextInputProps> | undefined;
  return el?.props ?? null;
}

function defaultProps(overrides: Partial<React.ComponentProps<typeof ConnectionSection>> = {}) {
  return {
    url: "",
    username: "",
    password: "",
    allowInsecureSsl: false,
    status: "",
    loading: false,
    onUrlSubmit: vi.fn(),
    onUsernameSubmit: vi.fn(),
    onPasswordSubmit: vi.fn(),
    onAllowInsecureSslChange: vi.fn(),
    onTestConnection: vi.fn(),
    ...overrides,
  };
}

describe("ConnectionSection", () => {
  beforeEach(() => {
    toggleCaptured.items = [];
    vi.clearAllMocks();
  });

  describe("URL field", () => {
    it("shows '(not set)' description when url is empty", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps()} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("(not set)");
    });

    it("shows the configured URL in the description", () => {
      const { getAllByTestId } = render(
        <ConnectionSection {...defaultProps({ url: "http://romm.local" })} />,
      );
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("http://romm.local");
    });

    it("opens a TextInputModal with field='url' when Edit is clicked", () => {
      const onUrlSubmit = vi.fn();
      const { getAllByText } = render(
        <ConnectionSection {...defaultProps({ url: "http://romm.local", onUrlSubmit })} />,
      );
      // Three Edit buttons (URL, username, password). URL is first.
      fireEvent.click(getAllByText("Edit")[0]!);
      const props = lastShownModalProps();
      expect(props?.label).toBe("RomM URL");
      expect(props?.value).toBe("http://romm.local");
      expect(props?.field).toBe("url");
      expect(props?.bIsPassword).toBeUndefined();
      expect(props?.onSubmit).toBe(onUrlSubmit);
    });
  });

  describe("username field", () => {
    it("shows '(not set)' when username is empty", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps()} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs.filter((d) => d === "(not set)").length).toBeGreaterThanOrEqual(2);
    });

    it("shows the username when set", () => {
      const { getAllByTestId } = render(
        <ConnectionSection {...defaultProps({ username: "daniel" })} />,
      );
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("daniel");
    });

    it("opens a TextInputModal with field='username' on Edit", () => {
      const onUsernameSubmit = vi.fn();
      const { getAllByText } = render(
        <ConnectionSection {...defaultProps({ username: "daniel", onUsernameSubmit })} />,
      );
      fireEvent.click(getAllByText("Edit")[1]!);
      const props = lastShownModalProps();
      expect(props?.label).toBe("Username");
      expect(props?.value).toBe("daniel");
      expect(props?.field).toBe("username");
      expect(props?.onSubmit).toBe(onUsernameSubmit);
    });
  });

  describe("shared-account warning", () => {
    it("is hidden for a personal username", () => {
      const { container } = render(
        <ConnectionSection {...defaultProps({ username: "daniel" })} />,
      );
      expect(container.textContent).not.toContain("Shared account detected");
    });

    it("renders for a known shared-account username", () => {
      const { container } = render(
        <ConnectionSection {...defaultProps({ username: "admin" })} />,
      );
      expect(container.textContent).toContain("Shared account detected");
      expect(container.textContent).toContain('"admin"');
    });
  });

  describe("password field", () => {
    it("shows '(not set)' when password is empty", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps()} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs.filter((d) => d === "(not set)").length).toBe(3);
    });

    it("shows '••••' when a password is set", () => {
      const { getAllByTestId } = render(
        <ConnectionSection {...defaultProps({ password: "stored" })} />,
      );
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("••••");
    });

    it("opens a TextInputModal with field='password' and bIsPassword=true on Edit", () => {
      const onPasswordSubmit = vi.fn();
      const { getAllByText } = render(
        <ConnectionSection {...defaultProps({ password: "stored", onPasswordSubmit })} />,
      );
      fireEvent.click(getAllByText("Edit")[2]!);
      const props = lastShownModalProps();
      expect(props?.label).toBe("Password");
      expect(props?.value).toBe("");
      expect(props?.field).toBe("password");
      expect(props?.bIsPassword).toBe(true);
      expect(props?.onSubmit).toBe(onPasswordSubmit);
    });
  });

  describe("HTTPS SSL toggle", () => {
    it("is hidden when url is http://...", () => {
      render(<ConnectionSection {...defaultProps({ url: "http://romm.local" })} />);
      expect(toggleCaptured.items).toHaveLength(0);
    });

    it("is hidden when url is empty", () => {
      render(<ConnectionSection {...defaultProps()} />);
      expect(toggleCaptured.items).toHaveLength(0);
    });

    it("is visible when url is https://...", () => {
      render(<ConnectionSection {...defaultProps({ url: "https://romm.local" })} />);
      expect(toggleCaptured.items).toHaveLength(1);
      expect(toggleCaptured.items[0]?.label).toBe("Allow Insecure SSL");
    });

    it("treats case-insensitive https prefix", () => {
      render(<ConnectionSection {...defaultProps({ url: "HTTPS://romm.local" })} />);
      expect(toggleCaptured.items).toHaveLength(1);
    });

    it("reflects allowInsecureSsl in checked state", () => {
      render(
        <ConnectionSection
          {...defaultProps({ url: "https://romm.local", allowInsecureSsl: true })}
        />,
      );
      expect(toggleCaptured.items[0]?.checked).toBe(true);
    });

    it("dispatches onAllowInsecureSslChange when toggled", () => {
      const onAllowInsecureSslChange = vi.fn();
      render(
        <ConnectionSection
          {...defaultProps({ url: "https://romm.local", onAllowInsecureSslChange })}
        />,
      );
      toggleCaptured.items[0]?.onChange?.(true);
      expect(onAllowInsecureSslChange).toHaveBeenCalledWith(true);
    });
  });

  describe("Test Connection button", () => {
    it("fires onTestConnection when clicked", () => {
      const onTestConnection = vi.fn();
      const { getByText } = render(
        <ConnectionSection {...defaultProps({ onTestConnection })} />,
      );
      fireEvent.click(getByText("Test Connection"));
      expect(onTestConnection).toHaveBeenCalledTimes(1);
    });

    it("is disabled while loading", () => {
      const { getByText } = render(<ConnectionSection {...defaultProps({ loading: true })} />);
      expect(getByText("Test Connection")).toBeDisabled();
    });
  });

  describe("status row", () => {
    it("renders the status Field when non-empty", () => {
      const { getAllByTestId } = render(
        <ConnectionSection {...defaultProps({ status: "Connected ✓" })} />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Connected ✓");
    });

    it("omits the status Field when empty", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps()} />);
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      // No status label among the field labels (3 base fields: URL, Username, Password).
      expect(labels).toEqual(["RomM URL", "Username", "Password"]);
    });
  });
});
