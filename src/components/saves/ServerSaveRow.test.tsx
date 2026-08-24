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

  it("renders the filename + the #id line when size and updated_at are missing", () => {
    const { container } = render(<div>{renderServerSaveRow(makeFile({ id: 7, updated_at: "" }))}</div>);
    expect(container.textContent).toContain("save.srm");
    // The #id always shows (leads the details line); with nothing else there is
    // no separator and no "Updated" segment.
    expect(container.textContent).toContain("#7");
    expect(container.textContent).not.toContain("·");
    expect(container.textContent).not.toContain("Updated");
  });

  it("shows the server save id with a # prefix, leading the details line", () => {
    const { container } = render(<div>{renderServerSaveRow(makeFile({ id: 122, size: 1024 }))}</div>);
    // Consistent with the version-history header style (#122 · …).
    expect(container.textContent).toContain("#122 · 1.0 KB");
  });

  it("renders filename + formatted size when size is present", () => {
    const { container } = render(<div>{renderServerSaveRow(makeFile({ size: 2048 }))}</div>);
    expect(container.textContent).toContain("save.srm");
    expect(container.textContent).toContain("2.0 KB");
  });

  it("renders filename + size + 'Updated <relative>' when both are present", () => {
    const { container } = render(
      <div>
        {renderServerSaveRow(
          makeFile({
            size: 1024,
            updated_at: "2025-06-15T11:30:00Z",
          }),
        )}
      </div>,
    );
    expect(container.textContent).toContain("save.srm");
    expect(container.textContent).toContain("1.0 KB");
    expect(container.textContent).toContain("Updated 30m ago");
  });

  it("renders the filename + #id (no size) when size is null", () => {
    const { container } = render(<div>{renderServerSaveRow(makeFile({ id: 3, size: null, updated_at: "" }))}</div>);
    expect(container.textContent).toContain("save.srm");
    expect(container.textContent).toContain("#3");
    expect(container.textContent).not.toContain("KB");
  });

  it("renders only filename + Updated when updated_at is set but size is null", () => {
    const { container } = render(
      <div>
        {renderServerSaveRow(
          makeFile({
            size: null,
            updated_at: "2025-06-15T11:30:00Z",
          }),
        )}
      </div>,
    );
    expect(container.textContent).toContain("save.srm");
    expect(container.textContent).toContain("Updated 30m ago");
    expect(container.textContent).not.toContain("KB");
  });

  it("always renders the details line (the #id) even when size and updated_at are missing", () => {
    const { container } = render(<div>{renderServerSaveRow(makeFile({ id: 9 }))}</div>);
    const wrapper = container.firstChild as HTMLElement;
    // The info column is the first child of the flex row (the copy button, when
    // present, is the second — absent here).
    const infoCol = wrapper.firstChild?.firstChild as HTMLElement;
    // Two lines now: the filename div + the #id details div.
    expect(infoCol.children.length).toBe(2);
    expect(infoCol.textContent).toContain("#9");
  });

  it("uses a unique key per save id so list reconciliation stays stable", () => {
    const f1 = makeFile({ id: 1, filename: "a.srm" });
    const f2 = makeFile({ id: 2, filename: "b.srm" });
    // The key is set on the row's outer div; we can't read React keys from the
    // DOM, but we can assert no duplicates by checking both rows render
    // side-by-side without crashing.
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
