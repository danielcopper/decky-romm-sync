import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { createElement } from "react";
import { AdvancedSection } from "./AdvancedSection";

// DropdownItem isn't in the global @decky/ui stub. Capture rgOptions +
// selectedOption + onChange so we can drive the onChange callback and assert
// the wiring without rendering a real Steam Dropdown.
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
const captured: { items: DropdownItemProps[] } = { items: [] };

vi.mock("@decky/ui", () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const passthrough = (tag: string) => (p: AnyProps) => createElement(tag, {}, p.children as never);
  return {
    PanelSection: passthrough("section"),
    PanelSectionRow: passthrough("div"),
    DropdownItem: (p: DropdownItemProps) => {
      captured.items.push(p);
      return createElement("div", { "data-testid": "dropdown" }, p.label as never);
    },
  };
});

describe("AdvancedSection", () => {
  beforeEach(() => {
    captured.items = [];
  });

  it("renders the log-level dropdown with the four canonical options", () => {
    render(<AdvancedSection logLevel="info" onLogLevelChange={vi.fn()} />);
    expect(captured.items).toHaveLength(1);
    const item = captured.items[0];
    expect(item?.label).toBe("Log Level");
    expect(item?.rgOptions?.map((o) => o.data)).toEqual(["error", "warn", "info", "debug"]);
  });

  it("forwards the current logLevel as selectedOption", () => {
    render(<AdvancedSection logLevel="debug" onLogLevelChange={vi.fn()} />);
    expect(captured.items[0]?.selectedOption).toBe("debug");
  });

  it("dispatches onLogLevelChange with option.data when the dropdown fires", () => {
    const onChange = vi.fn();
    render(<AdvancedSection logLevel="info" onLogLevelChange={onChange} />);
    captured.items[0]?.onChange?.({ data: "warn", label: "Warn" });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("warn");
  });

  it("passes string values straight through (no transformation)", () => {
    const onChange = vi.fn();
    render(<AdvancedSection logLevel="error" onLogLevelChange={onChange} />);
    captured.items[0]?.onChange?.({ data: "error", label: "Error" });
    expect(onChange).toHaveBeenCalledWith("error");
  });
});
