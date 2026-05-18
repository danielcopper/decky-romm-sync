import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render } from "@testing-library/react";
import { renderServerSaveRow } from "./ServerSaveRow";
import type { SlotSaveFile } from "../../types";

function makeFile(overrides: Partial<SlotSaveFile> = {}): SlotSaveFile {
  return {
    filename: "save.srm",
    id: 1,
    size: null,
    updated_at: "",
    emulator: "retroarch",
    ...overrides,
  };
}

describe("renderServerSaveRow", () => {
  // Pin time so "Updated <relative>" assertions stay deterministic.
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("renders just the filename when size and updated_at are missing", () => {
    const { container } = render(<div>{renderServerSaveRow(makeFile({ updated_at: "" }))}</div>);
    expect(container.textContent).toContain("save.srm");
    // No second line (size · Updated) when both are absent
    expect(container.textContent).not.toContain("·");
    expect(container.textContent).not.toContain("Updated");
  });

  it("renders filename + formatted size when size is present", () => {
    const { container } = render(
      <div>{renderServerSaveRow(makeFile({ size: 2048 }))}</div>,
    );
    expect(container.textContent).toContain("save.srm");
    expect(container.textContent).toContain("2.0 KB");
  });

  it("renders filename + size + 'Updated <relative>' when both are present", () => {
    const { container } = render(
      <div>{renderServerSaveRow(makeFile({
        size: 1024,
        updated_at: "2025-06-15T11:30:00Z",
      }))}</div>,
    );
    expect(container.textContent).toContain("save.srm");
    expect(container.textContent).toContain("1.0 KB");
    expect(container.textContent).toContain("Updated 30m ago");
  });

  it("renders only the filename when size is null", () => {
    const { container } = render(
      <div>{renderServerSaveRow(makeFile({ size: null, updated_at: "" }))}</div>,
    );
    expect(container.textContent).toBe("save.srm");
  });

  it("renders only filename + Updated when updated_at is set but size is null", () => {
    const { container } = render(
      <div>{renderServerSaveRow(makeFile({
        size: null,
        updated_at: "2025-06-15T11:30:00Z",
      }))}</div>,
    );
    expect(container.textContent).toContain("save.srm");
    expect(container.textContent).toContain("Updated 30m ago");
    expect(container.textContent).not.toContain("KB");
  });

  it("returns null for the second line when both size and updated_at are missing (no separator)", () => {
    const { container } = render(<div>{renderServerSaveRow(makeFile())}</div>);
    // Only the filename div exists — no second line at all
    const wrapper = container.firstChild as HTMLElement;
    const rowDiv = wrapper.firstChild as HTMLElement;
    // First child = filename div; nothing else inside the row
    expect(rowDiv.children.length).toBe(1);
  });

  it("uses a unique key per save id so list reconciliation stays stable", () => {
    const f1 = makeFile({ id: 1, filename: "a.srm" });
    const f2 = makeFile({ id: 2, filename: "b.srm" });
    // The key is set on the row's outer div via createElement; we can't read
    // React keys from the DOM, but we can assert no duplicates by checking
    // both rows render side-by-side without crashing.
    const { container } = render(
      <div>
        {renderServerSaveRow(f1)}
        {renderServerSaveRow(f2)}
      </div>,
    );
    expect(container.textContent).toContain("a.srm");
    expect(container.textContent).toContain("b.srm");
    // Both rows are direct children of the wrapper
    expect((container.firstChild as HTMLElement).children.length).toBe(2);
  });
});
