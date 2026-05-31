import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { SteamGridDBSection } from "./SteamGridDBSection";
import { showModal } from "@decky/ui";

// Local re-mock — we need:
//  - DialogButton to forward `disabled` so we can assert the Verify state.
//  - ButtonItem to forward `onClick` + `disabled` (not in global stub).
//  - Field to render the `description` text so masked-key vs. "Not configured"
//    can be asserted via container.textContent.
type AnyProps = Record<string, unknown> & { children?: unknown };
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
  DialogButton: ({
    children,
    onClick,
    disabled,
  }: AnyProps & {
    onClick?: () => void;
    disabled?: boolean;
  }) => createElement("button", { onClick, disabled, "data-role": "dialog" }, children as never),
  ButtonItem: ({
    children,
    onClick,
    disabled,
  }: AnyProps & {
    onClick?: () => void;
    disabled?: boolean;
  }) => createElement("button", { onClick, disabled, "data-role": "item" }, children as never),
  showModal: vi.fn(),
}));

interface TextInputProps {
  label: string;
  value: string;
  bIsPassword?: boolean;
  onSubmit: (value: string) => void;
}

function lastShownModalProps(): TextInputProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<TextInputProps> | undefined;
  return el?.props ?? null;
}

function defaultProps(overrides: Partial<React.ComponentProps<typeof SteamGridDBSection>> = {}) {
  return {
    sgdbApiKey: "",
    sgdbStatus: "",
    sgdbVerifying: false,
    onSubmitKey: vi.fn(),
    onVerifyKey: vi.fn(),
    ...overrides,
  };
}

describe("SteamGridDBSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("api key field", () => {
    it("renders masked '••••' description when a key is set", () => {
      const { getAllByTestId } = render(<SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored" })} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("••••");
    });

    it("renders 'Not configured' description when the key is empty", () => {
      const { getAllByTestId } = render(<SteamGridDBSection {...defaultProps()} />);
      const descs = getAllByTestId("field-desc").map((el) => el.textContent);
      expect(descs).toContain("Not configured");
    });

    it("opens a TextInputModal when Edit is clicked, prefilled empty and password-typed", () => {
      const onSubmitKey = vi.fn();
      const { getByText } = render(<SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored", onSubmitKey })} />);
      fireEvent.click(getByText("Edit"));
      const props = lastShownModalProps();
      expect(props).not.toBeNull();
      expect(props?.label).toBe("SteamGridDB API Key");
      expect(props?.value).toBe("");
      expect(props?.bIsPassword).toBe(true);
      expect(props?.onSubmit).toBe(onSubmitKey);
    });
  });

  describe("verify button", () => {
    it("is disabled while verifying", () => {
      const { getByText } = render(
        <SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored", sgdbVerifying: true })} />,
      );
      const btn = getByText("Verifying...");
      expect(btn).toBeDisabled();
    });

    it("is disabled when no key is configured (even if not verifying)", () => {
      const { getByText } = render(<SteamGridDBSection {...defaultProps()} />);
      const btn = getByText("Verify Key");
      expect(btn).toBeDisabled();
    });

    it("is enabled when a key is set and not verifying", () => {
      const { getByText } = render(<SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored" })} />);
      const btn = getByText("Verify Key");
      expect(btn).not.toBeDisabled();
    });

    it("fires onVerifyKey when clicked", () => {
      const onVerifyKey = vi.fn();
      const { getByText } = render(<SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored", onVerifyKey })} />);
      fireEvent.click(getByText("Verify Key"));
      expect(onVerifyKey).toHaveBeenCalledTimes(1);
    });

    it("renders 'Verifying...' label while sgdbVerifying is true", () => {
      const { getByText } = render(
        <SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored", sgdbVerifying: true })} />,
      );
      expect(getByText("Verifying...")).toBeInTheDocument();
    });
  });

  describe("status row", () => {
    it("renders a status Field when sgdbStatus is non-empty", () => {
      const { getAllByTestId } = render(<SteamGridDBSection {...defaultProps({ sgdbStatus: "Valid key ✓" })} />);
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Valid key ✓");
    });

    it("omits the status row when sgdbStatus is empty", () => {
      const { getAllByTestId } = render(<SteamGridDBSection {...defaultProps()} />);
      // Only the "API Key" Field renders when sgdbStatus is empty (Verify is
      // a ButtonItem, not a Field). Confirm no accidental empty-label leak.
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels.filter((l) => l === "")).toHaveLength(0);
    });
  });
});
