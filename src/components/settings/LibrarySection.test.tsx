import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { LibrarySection, buildRegionOptions, DEFAULT_REGION_LABEL, AUTO_REGION } from "./LibrarySection";

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
    ToggleField: (p: AnyProps & { checked?: boolean; onChange?: (v: boolean) => void; label?: unknown }) =>
      createElement(
        "div",
        {
          "data-testid": "toggle",
          "data-label": typeof p.label === "string" ? p.label : undefined,
        },
        createElement("input", {
          type: "checkbox",
          "data-testid": "toggle-input",
          checked: p.checked ?? false,
          onChange: (e: { target: { checked: boolean } }) => p.onChange?.(e.target.checked),
        }),
      ),
  };
});

describe("buildRegionOptions", () => {
  it("lists the Default sentinel + fixed anchors first, in build-time order", () => {
    const opts = buildRegionOptions([], AUTO_REGION);
    expect(opts.map((o) => o.data)).toEqual([AUTO_REGION, "World", "USA", "Europe", "Japan"]);
    expect(opts[0]?.label).toBe(DEFAULT_REGION_LABEL);
    expect(opts[0]?.label).not.toMatch(/auto/i);
  });

  it("appends distinct library regions after the anchors, sorted, de-duped against anchors", () => {
    const opts = buildRegionOptions(["Korea", "Brazil", "USA", "Korea"], AUTO_REGION);
    // "USA" is an anchor → not duplicated; "Korea"/"Brazil" appended sorted.
    expect(opts.map((o) => o.data)).toEqual([AUTO_REGION, "World", "USA", "Europe", "Japan", "Brazil", "Korea"]);
  });

  it("empty library → anchors only", () => {
    expect(buildRegionOptions([], AUTO_REGION).map((o) => o.data)).toEqual([
      AUTO_REGION,
      "World",
      "USA",
      "Europe",
      "Japan",
    ]);
  });

  it("always includes the current selection even if absent from anchors + library", () => {
    const opts = buildRegionOptions([], "Germany");
    expect(opts.map((o) => o.data)).toContain("Germany");
  });

  it("ignores empty/blank library region strings", () => {
    const opts = buildRegionOptions(["", "Brazil"], AUTO_REGION);
    expect(opts.map((o) => o.data)).toEqual([AUTO_REGION, "World", "USA", "Europe", "Japan", "Brazil"]);
  });
});

describe("LibrarySection", () => {
  beforeEach(() => {
    captured.items = [];
  });

  it("renders the preferred-region dropdown with anchors + library regions", () => {
    render(
      <LibrarySection
        preferredRegion={AUTO_REGION}
        libraryRegions={["Korea"]}
        onPreferredRegionChange={vi.fn()}
        platformGroups={false}
        onPlatformGroupsChange={vi.fn()}
      />,
    );
    expect(captured.items).toHaveLength(1);
    const item = captured.items[0];
    expect(item?.label).toBe("Preferred region");
    expect(item?.rgOptions?.map((o) => o.data)).toEqual([AUTO_REGION, "World", "USA", "Europe", "Japan", "Korea"]);
  });

  it("forwards the current preferredRegion as selectedOption", () => {
    render(
      <LibrarySection
        preferredRegion="Japan"
        libraryRegions={[]}
        onPreferredRegionChange={vi.fn()}
        platformGroups={false}
        onPlatformGroupsChange={vi.fn()}
      />,
    );
    expect(captured.items[0]?.selectedOption).toBe("Japan");
  });

  it("dispatches onPreferredRegionChange with option.data when the dropdown fires", () => {
    const onChange = vi.fn();
    render(
      <LibrarySection
        preferredRegion={AUTO_REGION}
        libraryRegions={[]}
        onPreferredRegionChange={onChange}
        platformGroups={false}
        onPlatformGroupsChange={vi.fn()}
      />,
    );
    captured.items[0]?.onChange?.({ data: "Japan", label: "Japan" });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("Japan");
  });

  it("passes the auto sentinel straight through", () => {
    const onChange = vi.fn();
    render(
      <LibrarySection
        preferredRegion="Europe"
        libraryRegions={[]}
        onPreferredRegionChange={onChange}
        platformGroups={false}
        onPlatformGroupsChange={vi.fn()}
      />,
    );
    captured.items[0]?.onChange?.({ data: AUTO_REGION, label: DEFAULT_REGION_LABEL });
    expect(onChange).toHaveBeenCalledWith(AUTO_REGION);
  });

  it("renders the platform-groups toggle reflecting the current value", () => {
    const { container } = render(
      <LibrarySection
        preferredRegion={AUTO_REGION}
        libraryRegions={[]}
        onPreferredRegionChange={vi.fn()}
        platformGroups={true}
        onPlatformGroupsChange={vi.fn()}
      />,
    );
    const toggle = container.querySelector<HTMLInputElement>(
      '[data-label="Show collection games in platform groups"] input',
    );
    expect(toggle).not.toBeNull();
    expect(toggle?.checked).toBe(true);
  });

  it("dispatches onPlatformGroupsChange when the platform-groups toggle fires", () => {
    const onChange = vi.fn();
    const { container } = render(
      <LibrarySection
        preferredRegion={AUTO_REGION}
        libraryRegions={[]}
        onPreferredRegionChange={vi.fn()}
        platformGroups={false}
        onPlatformGroupsChange={onChange}
      />,
    );
    const toggle = container.querySelector<HTMLInputElement>(
      '[data-label="Show collection games in platform groups"] input',
    )!;
    fireEvent.click(toggle);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(true);
  });
});
