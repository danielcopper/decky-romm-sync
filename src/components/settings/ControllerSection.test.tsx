import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { ControllerSection } from "./ControllerSection";
import type { RetroArchInputCheck } from "../../types";

// Local re-mock: ButtonItem must forward `disabled`, DropdownItem must
// capture rgOptions + onChange so we can drive the dropdown without a real
// Steam UI.
type AnyProps = Record<string, unknown> & { children?: unknown };
interface DropdownOption { data: unknown; label: string }
interface DropdownItemProps {
  label?: string;
  rgOptions?: DropdownOption[];
  selectedOption?: unknown;
  onChange?: (option: DropdownOption) => void;
}
const dropdownCaptured: { items: DropdownItemProps[] } = { items: [] };

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
    ),
  ButtonItem: ({ children, onClick, disabled }: AnyProps & {
    onClick?: () => void;
    disabled?: boolean;
  }) => createElement("button", { onClick, disabled }, children as never),
  DropdownItem: (p: DropdownItemProps) => {
    dropdownCaptured.items.push(p);
    return createElement("div", { "data-testid": "dropdown" }, p.label as never);
  },
}));

function defaultProps(overrides: Partial<React.ComponentProps<typeof ControllerSection>> = {}) {
  return {
    steamInputMode: "default",
    steamInputStatus: "",
    retroarchWarning: null,
    retroarchFixStatus: "",
    loading: false,
    onModeChange: vi.fn(),
    onApplyMode: vi.fn(),
    onFixInputDriver: vi.fn(),
    ...overrides,
  };
}

describe("ControllerSection", () => {
  beforeEach(() => {
    dropdownCaptured.items = [];
    vi.clearAllMocks();
  });

  describe("mode dropdown", () => {
    it("renders the three Steam Input mode options", () => {
      render(<ControllerSection {...defaultProps()} />);
      expect(dropdownCaptured.items).toHaveLength(1);
      const opts = dropdownCaptured.items[0]?.rgOptions ?? [];
      expect(opts.map((o) => o.data)).toEqual(["default", "force_on", "force_off"]);
    });

    it("forwards steamInputMode as selectedOption", () => {
      render(<ControllerSection {...defaultProps({ steamInputMode: "force_on" })} />);
      expect(dropdownCaptured.items[0]?.selectedOption).toBe("force_on");
    });

    it("dispatches onModeChange with option.data on dropdown change", () => {
      const onModeChange = vi.fn();
      render(<ControllerSection {...defaultProps({ onModeChange })} />);
      dropdownCaptured.items[0]?.onChange?.({ data: "force_off", label: "Force Off" });
      expect(onModeChange).toHaveBeenCalledWith("force_off");
    });
  });

  describe("apply button", () => {
    it("fires onApplyMode when clicked", () => {
      const onApplyMode = vi.fn();
      const { getByText } = render(
        <ControllerSection {...defaultProps({ onApplyMode })} />,
      );
      fireEvent.click(getByText("Apply to All Shortcuts"));
      expect(onApplyMode).toHaveBeenCalledTimes(1);
    });

    it("is disabled while loading=true", () => {
      const { getByText } = render(<ControllerSection {...defaultProps({ loading: true })} />);
      expect(getByText("Apply to All Shortcuts")).toBeDisabled();
    });

    it("is enabled when loading=false", () => {
      const { getByText } = render(<ControllerSection {...defaultProps()} />);
      expect(getByText("Apply to All Shortcuts")).not.toBeDisabled();
    });
  });

  describe("steamInputStatus field", () => {
    it("renders the status Field when non-empty", () => {
      const { getAllByTestId } = render(
        <ControllerSection {...defaultProps({ steamInputStatus: "Applied to 12 shortcuts" })} />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Applied to 12 shortcuts");
    });

    it("omits the status Field when empty", () => {
      const { queryAllByTestId } = render(<ControllerSection {...defaultProps()} />);
      expect(queryAllByTestId("field")).toHaveLength(0);
    });
  });

  describe("RetroArch warning block", () => {
    function warning(overrides: Partial<RetroArchInputCheck> = {}): RetroArchInputCheck {
      return {
        warning: true,
        current: "udev",
        config_path: "/home/deck/.config/retroarch/retroarch.cfg",
        ...overrides,
      };
    }

    it("is hidden when retroarchWarning is null", () => {
      const { container } = render(<ControllerSection {...defaultProps()} />);
      expect(container.textContent).not.toContain("RetroArch input_driver");
      expect(container.textContent).not.toContain("Fix input_driver");
    });

    it("is hidden when retroarchWarning.warning is false", () => {
      const { container } = render(
        <ControllerSection
          {...defaultProps({ retroarchWarning: warning({ warning: false }) })}
        />,
      );
      expect(container.textContent).not.toContain("RetroArch input_driver");
    });

    it("renders the current input_driver in the warning label", () => {
      const { container } = render(
        <ControllerSection
          {...defaultProps({ retroarchWarning: warning({ current: "udev" }) })}
        />,
      );
      expect(container.textContent).toContain('RetroArch input_driver: "udev"');
    });

    it("fires onFixInputDriver when the fix button is clicked", () => {
      const onFixInputDriver = vi.fn();
      const { getByText } = render(
        <ControllerSection
          {...defaultProps({ retroarchWarning: warning(), onFixInputDriver })}
        />,
      );
      fireEvent.click(getByText("Fix input_driver to sdl2"));
      expect(onFixInputDriver).toHaveBeenCalledTimes(1);
    });

    it("renders the fix-status Field when retroarchFixStatus is non-empty", () => {
      const { getAllByTestId } = render(
        <ControllerSection
          {...defaultProps({
            retroarchWarning: warning(),
            retroarchFixStatus: "Updated retroarch.cfg",
          })}
        />,
      );
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toContain("Updated retroarch.cfg");
    });

    it("omits the fix-status Field when retroarchFixStatus is empty", () => {
      const { getAllByTestId } = render(
        <ControllerSection {...defaultProps({ retroarchWarning: warning() })} />,
      );
      // Only the warning-label Field, no fix-status Field.
      const labels = getAllByTestId("field-label").map((el) => el.textContent);
      expect(labels).toHaveLength(1);
      expect(labels[0]).toContain("RetroArch input_driver");
    });
  });
});
