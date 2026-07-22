import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { SteamGridDBSection } from "./SteamGridDBSection";
import { showModal } from "@decky/ui";

// Local re-mock — the API Key row is a Field + DialogButton, so Field renders
// its `label` + `description` (masked "••••" vs. "Not configured" assertable via
// field-desc) and DialogButton renders its `children` ("Edit") forwarding
// `onClick` so the modal-open wiring stays drivable.
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
  DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
    createElement("button", { onClick, disabled }, children as never),
  showModal: vi.fn(),
}));

// The Edit button opens SgdbApiKeyModal; capture the props it was constructed
// with so the verify/save wiring is assertable without rendering the modal.
interface SgdbModalProps {
  onVerify?: (key: string) => Promise<{ success: boolean; message: string }>;
  onSave?: (key: string) => Promise<void>;
}

function lastShownModalProps(): SgdbModalProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<SgdbModalProps> | undefined;
  return el?.props ?? null;
}

function defaultProps(overrides: Partial<React.ComponentProps<typeof SteamGridDBSection>> = {}) {
  return {
    sgdbApiKey: "",
    onVerifyKey: vi.fn(async () => ({ success: true, message: "" })),
    onSaveKey: vi.fn(async () => {}),
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
  });

  describe("Edit opens the inline-validating modal", () => {
    it("opens SgdbApiKeyModal wired to onVerifyKey + onSaveKey when Edit is clicked", () => {
      const onVerifyKey = vi.fn(async () => ({ success: true, message: "" }));
      const onSaveKey = vi.fn(async () => {});
      const { getByText } = render(
        <SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored", onVerifyKey, onSaveKey })} />,
      );
      fireEvent.click(getByText("Edit"));
      const props = lastShownModalProps();
      expect(props).not.toBeNull();
      // The modal owns the verify-then-save orchestration; the section only
      // threads the parent's handlers into it.
      expect(props?.onVerify).toBe(onVerifyKey);
      expect(props?.onSave).toBe(onSaveKey);
    });

    it("does not open a modal until Edit is clicked", () => {
      render(<SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored" })} />);
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
    });
  });

  describe("removed affordances", () => {
    it("no longer renders a Verify Key button", () => {
      const { queryByText } = render(<SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored" })} />);
      expect(queryByText("Verify Key")).toBeNull();
      expect(queryByText("Verifying...")).toBeNull();
    });

    it("renders exactly one Field (the API Key row) — no status line of its own", () => {
      const { getAllByTestId } = render(<SteamGridDBSection {...defaultProps({ sgdbApiKey: "stored" })} />);
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toEqual(["API Key"]);
    });
  });
});
