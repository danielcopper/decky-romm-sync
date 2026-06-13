import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { ConnectionSection } from "./ConnectionSection";
import { showModal } from "@decky/ui";

// Local re-mock: the URL + RomM Account rows are Field + DialogButton again,
// so Field renders its `label` + `description` (those copy strings stay
// queryable via field-label/field-desc) and DialogButton renders its
// `children` ("Edit"/"Sign in") forwarding `onClick`; ButtonItem stays for the
// layout="below" Test Connection row, forwarding `disabled` + `description` +
// `children`; ToggleField forwards `checked` + a usable onChange that mirrors
// the global stub's (boolean) signature.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface ToggleFieldProps {
  label?: unknown;
  description?: unknown;
  checked?: boolean;
  onChange?: (value: boolean) => void;
}
const toggleCaptured: { items: ToggleFieldProps[] } = { items: [] };

vi.mock("@decky/ui", () => ({
  PanelSection: (p: AnyProps) => createElement("section", {}, p.children as never),
  PanelSectionRow: (p: AnyProps) => createElement("div", {}, p.children as never),
  Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
    createElement(
      "div",
      { "data-testid": "field" },
      createElement("span", { "data-testid": "field-label" }, p.label as never),
      createElement("span", { "data-testid": "field-desc" }, p.description as never),
      p.children as never,
    ),
  DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children as never),
  ButtonItem: ({
    children,
    onClick,
    disabled,
    description,
  }: AnyProps & {
    onClick?: () => void;
    disabled?: boolean;
    description?: unknown;
  }) => createElement("button", { onClick, disabled, "data-description": description as never }, children as never),
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

// Captured props off the modals opened via showModal. The URL Edit opens a
// TextInputModal (field='url'); the Sign in button opens a ConnectModal
// (onConnect callback).
interface UrlModalProps {
  label?: string;
  value?: string;
  field?: string;
  bIsPassword?: boolean;
  onSubmit?: (value: string) => void;
}
interface ConnectModalProps {
  onConnect?: (username: string, password: string) => void;
}

function lastShownModalProps<T>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

function defaultProps(overrides: Partial<React.ComponentProps<typeof ConnectionSection>> = {}) {
  return {
    url: "",
    hasToken: false,
    allowInsecureSsl: false,
    status: "",
    loading: false,
    onUrlChange: vi.fn(),
    onConnect: vi.fn(),
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
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps({ url: "http://romm.local" })} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("http://romm.local");
    });

    it("opens a TextInputModal with field='url' when Edit is clicked", () => {
      const onUrlChange = vi.fn();
      const { getByText } = render(<ConnectionSection {...defaultProps({ url: "http://romm.local", onUrlChange })} />);
      fireEvent.click(getByText("Edit"));
      const props = lastShownModalProps<UrlModalProps>();
      expect(props?.label).toBe("RomM URL");
      expect(props?.value).toBe("http://romm.local");
      expect(props?.field).toBe("url");
      expect(props?.bIsPassword).toBeUndefined();
      expect(props?.onSubmit).toBe(onUrlChange);
    });
  });

  describe("connection status indicator", () => {
    it("labels the account row 'RomM Account'", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps({ hasToken: true })} />);
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("RomM Account");
    });

    it("shows 'Signed in' description when hasToken is true", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps({ hasToken: true })} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("Signed in");
    });

    it("shows 'Not signed in' description when hasToken is false", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps({ hasToken: false })} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("Not signed in");
    });

    it("never renders the removed Username/Password fields", () => {
      const { container } = render(<ConnectionSection {...defaultProps({ hasToken: true })} />);
      expect(container.textContent).not.toContain("Username");
      expect(container.textContent).not.toContain("Password");
    });
  });

  describe("Sign in button", () => {
    it("renders a Sign in button", () => {
      const { getByText } = render(<ConnectionSection {...defaultProps()} />);
      expect(getByText("Sign in")).toBeTruthy();
    });

    it("opens a ConnectModal wired to onConnect when clicked", () => {
      const onConnect = vi.fn();
      const { getByText } = render(<ConnectionSection {...defaultProps({ onConnect })} />);
      fireEvent.click(getByText("Sign in"));
      const props = lastShownModalProps<ConnectModalProps>();
      expect(props?.onConnect).toBe(onConnect);
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

    it("trims a leading space so a padded https URL still shows the toggle", () => {
      // Regression: untrimmed startsWith("https") hid the toggle for "  https://...".
      render(<ConnectionSection {...defaultProps({ url: "  https://romm.local" })} />);
      expect(toggleCaptured.items).toHaveLength(1);
    });

    it("stays hidden for a padded http URL", () => {
      render(<ConnectionSection {...defaultProps({ url: "  http://romm.local" })} />);
      expect(toggleCaptured.items).toHaveLength(0);
    });

    it("reflects allowInsecureSsl in checked state", () => {
      render(<ConnectionSection {...defaultProps({ url: "https://romm.local", allowInsecureSsl: true })} />);
      expect(toggleCaptured.items[0]?.checked).toBe(true);
    });

    it("dispatches onAllowInsecureSslChange when toggled", () => {
      const onAllowInsecureSslChange = vi.fn();
      render(<ConnectionSection {...defaultProps({ url: "https://romm.local", onAllowInsecureSslChange })} />);
      toggleCaptured.items[0]?.onChange?.(true);
      expect(onAllowInsecureSslChange).toHaveBeenCalledWith(true);
    });
  });

  describe("Test Connection button", () => {
    it("fires onTestConnection when clicked", () => {
      const onTestConnection = vi.fn();
      const { getByText } = render(<ConnectionSection {...defaultProps({ hasToken: true, onTestConnection })} />);
      fireEvent.click(getByText("Test Connection"));
      expect(onTestConnection).toHaveBeenCalledTimes(1);
    });

    it("is disabled while loading", () => {
      const { getByText } = render(<ConnectionSection {...defaultProps({ hasToken: true, loading: true })} />);
      expect(getByText("Test Connection")).toBeDisabled();
    });

    it("is disabled and shows the sign-in hint when not signed in", () => {
      const { getByText } = render(<ConnectionSection {...defaultProps({ hasToken: false })} />);
      const button = getByText("Test Connection");
      expect(button).toBeDisabled();
      expect(button.getAttribute("data-description")).toBe("Sign in to RomM first to test the connection.");
    });

    it("is enabled with no hint once signed in", () => {
      const { getByText } = render(<ConnectionSection {...defaultProps({ hasToken: true })} />);
      const button = getByText("Test Connection");
      expect(button).not.toBeDisabled();
      // `description={undefined}` renders no data-description attribute.
      expect(button.getAttribute("data-description")).toBeNull();
    });
  });

  describe("status row", () => {
    it("renders the status Field when non-empty", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps({ status: "Connected ✓" })} />);
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Connected ✓");
      // URL row + RomM Account row + status row.
      expect(getAllByTestId("field")).toHaveLength(3);
    });

    it("omits the status Field when empty", () => {
      const { getAllByTestId } = render(<ConnectionSection {...defaultProps()} />);
      // URL + RomM Account are Field + DialogButton rows, so with no status the
      // only Fields are those two — the status row does not render.
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toEqual(["RomM URL", "RomM Account"]);
    });
  });
});
