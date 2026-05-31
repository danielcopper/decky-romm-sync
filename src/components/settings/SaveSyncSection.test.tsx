import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { SaveSyncSection } from "./SaveSyncSection";
import { showModal } from "@decky/ui";
import type { SaveSyncSettings } from "../../types";

// Local re-mock: the Default Save Slot row is a Field + DialogButton again,
// so Field renders its `label` + `description` (the slot copy stays assertable
// via container.textContent / field-desc) and DialogButton renders its
// `children` ("Edit") forwarding `onClick`; ButtonItem stays for the
// layout="below" Reset / Sync All rows, forwarding `disabled` + `children`;
// ToggleField + DropdownItem expose their props so we can drive their
// controlled behavior without a real Steam UI.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface ToggleFieldProps {
  label?: unknown;
  description?: unknown;
  checked?: boolean;
  onChange?: (value: boolean) => void;
}
interface DropdownOption {
  data: unknown;
  label: string;
}
interface DropdownItemProps {
  label?: string;
  rgOptions?: DropdownOption[];
  selectedOption?: unknown;
  onChange?: (option: DropdownOption) => void;
}
const toggleCaptured: { items: ToggleFieldProps[] } = { items: [] };
const dropdownCaptured: { items: DropdownItemProps[] } = { items: [] };

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
  }: AnyProps & {
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
  DropdownItem: (p: DropdownItemProps) => {
    dropdownCaptured.items.push(p);
    return createElement("div", { "data-testid": "dropdown" }, p.label as never);
  },
  showModal: vi.fn(),
}));

interface TextInputProps {
  label: string;
  value: string;
  onSubmit: (value: string) => void;
}

function lastShownModalProps(): TextInputProps | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<TextInputProps> | undefined;
  return el?.props ?? null;
}

function makeSettings(overrides: Partial<SaveSyncSettings> = {}): SaveSyncSettings {
  return {
    save_sync_enabled: true,
    sync_before_launch: false,
    sync_after_exit: false,
    default_slot: "default",
    autocleanup_limit: 10,
    ...overrides,
  };
}

function defaultProps(overrides: Partial<React.ComponentProps<typeof SaveSyncSection>> = {}) {
  return {
    saveSyncSettings: makeSettings(),
    saveSyncToggleKey: 0,
    deviceInfo: null,
    syncing: false,
    syncStatus: "",
    onToggleSaveSync: vi.fn(),
    onSettingChange: vi.fn(),
    onDefaultSlotSubmit: vi.fn(),
    onResetDefaultSlot: vi.fn(),
    onSyncAll: vi.fn(),
    ...overrides,
  };
}

describe("SaveSyncSection", () => {
  beforeEach(() => {
    toggleCaptured.items = [];
    dropdownCaptured.items = [];
    vi.clearAllMocks();
  });

  describe("loading state", () => {
    it("renders 'Loading...' when saveSyncSettings is null", () => {
      const { getAllByTestId } = render(<SaveSyncSection {...defaultProps({ saveSyncSettings: null })} />);
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Loading...");
      // No toggle when loading.
      expect(toggleCaptured.items).toHaveLength(0);
    });
  });

  describe("master toggle", () => {
    it("renders the Enable Save Sync toggle reflecting saveSyncSettings.save_sync_enabled", () => {
      render(<SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ save_sync_enabled: false }) })} />);
      const masterToggle = toggleCaptured.items.find((t) => t.label === "Enable Save Sync");
      expect(masterToggle?.checked).toBe(false);
    });

    it("dispatches onToggleSaveSync when toggled", () => {
      const onToggleSaveSync = vi.fn();
      render(<SaveSyncSection {...defaultProps({ onToggleSaveSync })} />);
      const masterToggle = toggleCaptured.items.find((t) => t.label === "Enable Save Sync");
      masterToggle?.onChange?.(true);
      expect(onToggleSaveSync).toHaveBeenCalledWith(true);
    });

    it("renders the 'Save sync is disabled' field when disabled", () => {
      const { getAllByTestId } = render(
        <SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ save_sync_enabled: false }) })} />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Save sync is disabled");
    });

    it("hides sub-controls when save_sync_enabled is false", () => {
      render(<SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ save_sync_enabled: false }) })} />);
      // Only the master toggle present.
      expect(toggleCaptured.items).toHaveLength(1);
      // No dropdown, no Sync All button.
      expect(dropdownCaptured.items).toHaveLength(0);
    });

    it("treats save_sync_enabled defaulting to false when missing (?? false)", () => {
      // Cast through Partial to drop the required field intentionally.
      const partial = makeSettings();
      delete (partial as { save_sync_enabled?: boolean }).save_sync_enabled;
      render(<SaveSyncSection {...defaultProps({ saveSyncSettings: partial })} />);
      const masterToggle = toggleCaptured.items.find((t) => t.label === "Enable Save Sync");
      expect(masterToggle?.checked).toBe(false);
    });
  });

  describe("device info field", () => {
    it("is hidden when deviceInfo is null", () => {
      const { container } = render(<SaveSyncSection {...defaultProps()} />);
      expect(container.textContent).not.toContain("Registered as");
    });

    it("renders the device name when deviceInfo is provided", () => {
      const { container } = render(
        <SaveSyncSection {...defaultProps({ deviceInfo: { device_id: "d1", device_name: "Steam Deck" } })} />,
      );
      expect(container.textContent).toContain('Registered as "Steam Deck"');
    });
  });

  describe("sync_before_launch / sync_after_exit toggles", () => {
    it("reflect their values from saveSyncSettings", () => {
      render(
        <SaveSyncSection
          {...defaultProps({
            saveSyncSettings: makeSettings({
              sync_before_launch: true,
              sync_after_exit: false,
            }),
          })}
        />,
      );
      const before = toggleCaptured.items.find((t) => t.label === "Sync before launch");
      const after = toggleCaptured.items.find((t) => t.label === "Sync after exit");
      expect(before?.checked).toBe(true);
      expect(after?.checked).toBe(false);
    });

    it("dispatches partial settings on sync_before_launch change", () => {
      const onSettingChange = vi.fn();
      render(<SaveSyncSection {...defaultProps({ onSettingChange })} />);
      const before = toggleCaptured.items.find((t) => t.label === "Sync before launch");
      before?.onChange?.(true);
      expect(onSettingChange).toHaveBeenCalledWith({ sync_before_launch: true });
    });

    it("dispatches partial settings on sync_after_exit change", () => {
      const onSettingChange = vi.fn();
      render(<SaveSyncSection {...defaultProps({ onSettingChange })} />);
      const after = toggleCaptured.items.find((t) => t.label === "Sync after exit");
      after?.onChange?.(true);
      expect(onSettingChange).toHaveBeenCalledWith({ sync_after_exit: true });
    });
  });

  describe("default slot field", () => {
    it("shows the current default_slot value in the description", () => {
      const { container } = render(
        <SaveSyncSection
          {...defaultProps({
            saveSyncSettings: makeSettings({ default_slot: "speedrun" }),
          })}
        />,
      );
      expect(container.textContent).toContain("speedrun");
    });

    it("shows '(no slot)' when default_slot is empty", () => {
      const { container } = render(
        <SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ default_slot: "" }) })} />,
      );
      expect(container.textContent).toContain("(no slot)");
    });

    it("opens a TextInputModal on Edit with the current default_slot", () => {
      const onDefaultSlotSubmit = vi.fn();
      const { getByText } = render(
        <SaveSyncSection
          {...defaultProps({
            saveSyncSettings: makeSettings({ default_slot: "main" }),
            onDefaultSlotSubmit,
          })}
        />,
      );
      fireEvent.click(getByText("Edit"));
      const props = lastShownModalProps();
      expect(props?.label).toBe("Default Save Slot");
      expect(props?.value).toBe("main");
      expect(props?.onSubmit).toBe(onDefaultSlotSubmit);
    });

    it("passes an empty string to the modal when default_slot is null", () => {
      const { getByText } = render(
        <SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ default_slot: null }) })} />,
      );
      fireEvent.click(getByText("Edit"));
      expect(lastShownModalProps()?.value).toBe("");
    });
  });

  describe("Reset to default button", () => {
    it("is hidden when default_slot === 'default'", () => {
      const { container } = render(<SaveSyncSection {...defaultProps()} />);
      expect(container.textContent).not.toContain("Reset to default");
    });

    it("is rendered and fires onResetDefaultSlot when default_slot differs", () => {
      const onResetDefaultSlot = vi.fn();
      const { getByText } = render(
        <SaveSyncSection
          {...defaultProps({
            saveSyncSettings: makeSettings({ default_slot: "speedrun" }),
            onResetDefaultSlot,
          })}
        />,
      );
      fireEvent.click(getByText("Reset to default"));
      expect(onResetDefaultSlot).toHaveBeenCalledTimes(1);
    });

    it("is rendered when default_slot is null (null !== 'default')", () => {
      const { getByText } = render(
        <SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ default_slot: null }) })} />,
      );
      expect(getByText("Reset to default")).toBeInTheDocument();
    });
  });

  describe("legacy-mode warning", () => {
    it("is hidden for a normal slot like 'default'", () => {
      const { container } = render(<SaveSyncSection {...defaultProps()} />);
      expect(container.textContent).not.toContain("Legacy mode");
    });

    it("renders when default_slot is null", () => {
      const { container } = render(
        <SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ default_slot: null }) })} />,
      );
      expect(container.textContent).toContain("Legacy mode (no slot)");
    });

    it("renders when default_slot is the empty string", () => {
      const { container } = render(
        <SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ default_slot: "" }) })} />,
      );
      expect(container.textContent).toContain("Legacy mode (no slot)");
    });
  });

  describe("history limit dropdown", () => {
    it("renders the four canonical limit options", () => {
      render(<SaveSyncSection {...defaultProps()} />);
      const dd = dropdownCaptured.items.find((d) => d.label === "Save History Limit");
      expect(dd?.rgOptions?.map((o) => o.data)).toEqual([5, 10, 20, 50]);
    });

    it("reflects autocleanup_limit as selectedOption", () => {
      render(<SaveSyncSection {...defaultProps({ saveSyncSettings: makeSettings({ autocleanup_limit: 20 }) })} />);
      const dd = dropdownCaptured.items.find((d) => d.label === "Save History Limit");
      expect(dd?.selectedOption).toBe(20);
    });

    it("dispatches partial settings with autocleanup_limit on change", () => {
      const onSettingChange = vi.fn();
      render(<SaveSyncSection {...defaultProps({ onSettingChange })} />);
      const dd = dropdownCaptured.items.find((d) => d.label === "Save History Limit");
      dd?.onChange?.({ data: 50, label: "50" });
      expect(onSettingChange).toHaveBeenCalledWith({ autocleanup_limit: 50 });
    });
  });

  describe("Sync All button", () => {
    it("fires onSyncAll when clicked", () => {
      const onSyncAll = vi.fn();
      const { getByText } = render(<SaveSyncSection {...defaultProps({ onSyncAll })} />);
      fireEvent.click(getByText("Sync All Saves Now"));
      expect(onSyncAll).toHaveBeenCalledTimes(1);
    });

    it("renders 'Syncing...' and is disabled while syncing=true", () => {
      const { getByText } = render(<SaveSyncSection {...defaultProps({ syncing: true })} />);
      const btn = getByText("Syncing...");
      expect(btn).toBeDisabled();
    });
  });

  describe("syncStatus row", () => {
    it("renders the status Field when non-empty", () => {
      const { getAllByTestId } = render(<SaveSyncSection {...defaultProps({ syncStatus: "Synced 3 files ✓" })} />);
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Synced 3 files ✓");
    });

    it("omits the status Field when empty", () => {
      const { queryAllByTestId } = render(<SaveSyncSection {...defaultProps()} />);
      const labels = queryAllByTestId("field-label").map((el) => el.textContent);
      // Catches the regression where the `{syncStatus && ...}` guard is
      // dropped and the Field renders with an empty label. With the default
      // props the only Field is the Default Save Slot row (labelled
      // "Default Save Slot") — no device/legacy/status Field renders, so
      // there is no empty-label leak.
      expect(labels.filter((l) => l === "")).toHaveLength(0);
      // Sanity-check the status row truly is absent (and the slot row present).
      expect(labels).toEqual(["Default Save Slot"]);
    });
  });
});
