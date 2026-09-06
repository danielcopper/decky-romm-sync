/**
 * Columns tests — one row of scrolling regions: how a column is sized, that
 * each is a region of its own, and that a column is remounted exactly when its
 * region key changes.
 *
 * The scroll panel is read at module scope by `ScrollRegion`, so the tests that
 * need to see the regions load a copy of the component with a stub in its place.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { CSSProperties, FC, ReactNode } from "react";
import type { ColumnsProps } from "./Columns";

/** Stand-in for Steam's scroll panel: it keeps the style, which is where a
 *  column's width lands, and is identifiable as one region per column. */
const StubScrollPanel: FC<{ style?: CSSProperties; children?: ReactNode }> = ({ style, children }) => (
  <div data-testid="scroll-panel" style={style}>
    {children}
  </div>
);

async function loadColumns(): Promise<FC<ColumnsProps>> {
  vi.resetModules();
  vi.doMock("../../utils/deckyUiInternals", () => ({ ScrollPanel: StubScrollPanel }));
  return (await import("./Columns")).Columns;
}

describe("Columns", () => {
  afterEach(() => {
    vi.doUnmock("../../utils/deckyUiInternals");
    vi.resetModules();
  });

  it("gives every column a scrolling region of its own", async () => {
    const Columns = await loadColumns();

    render(
      <Columns
        columns={[
          { id: "table", content: <button>a table row</button> },
          { id: "controls", width: "220px", content: <button>a control</button> },
        ]}
      />,
    );
    const [table, controls] = screen.getAllByTestId("scroll-panel");

    // Two regions rather than one around both: the columns scroll
    // independently, so a long table must not carry the controls up with it.
    expect(table).toContainElement(screen.getByRole("button", { name: "a table row" }));
    expect(controls).toContainElement(screen.getByRole("button", { name: "a control" }));
    expect(table).not.toContainElement(screen.getByRole("button", { name: "a control" }));
  });

  it("pins a column to its width and lets the one without take the rest", async () => {
    const Columns = await loadColumns();

    render(
      <Columns
        columns={[
          { id: "fixed", width: "264px", content: <div>fixed</div> },
          { id: "rest", content: <div>rest</div> },
        ]}
      />,
    );
    const [fixed, rest] = screen.getAllByTestId("scroll-panel");

    expect(fixed?.style.flex).toBe("0 0 264px");
    expect(fixed?.style.width).toBe("264px");
    expect(rest?.style.flex).toBe("1 1 auto");
    // A flex item's floor is its content, so without the reset one over-wide row
    // widens the column past its share.
    expect(rest?.style.minWidth).toBe("0");
  });

  it("lays the columns out in one horizontal row the stick can cross", async () => {
    const Columns = await loadColumns();

    const { container } = render(<Columns columns={[{ id: "only", content: <div>only</div> }]} />);
    const row = container.firstElementChild as HTMLElement;

    expect(row.style.display).toBe("flex");
    expect(row.style.gap).toBe("12px");
    // The row is bounded by the height the frame measured, and a flex child's
    // min-height is its content unless the floor is reset — without both the
    // regions grow instead of scrolling.
    expect(row.style.height).toBe("100%");
    expect(row.style.minHeight).toBe("0");
  });

  it("remounts a column when its region key changes, and leaves the others alone", async () => {
    const Columns = await loadColumns();
    const columns = (key: string): ColumnsProps["columns"] => [
      { id: "list", content: <div>the list</div> },
      { id: "detail", regionKey: key, content: <div>{`detail ${key}`}</div> },
    ];

    const { rerender } = render(<Columns columns={columns("n64")} />);
    const [listBefore, detailBefore] = screen.getAllByTestId("scroll-panel");
    rerender(<Columns columns={columns("psx")} />);
    const [listAfter, detailAfter] = screen.getAllByTestId("scroll-panel");

    // The remount is how a fresh region opens at its own top; the neighbour
    // keeping its element is what stops it losing the offset the reader left it
    // at.
    expect(detailBefore).not.toBeInTheDocument();
    expect(detailAfter).toContainElement(screen.getByText("detail psx"));
    expect(listAfter).toBe(listBefore);
  });

  it("keeps a column mounted across a re-render when it names no region key", async () => {
    const Columns = await loadColumns();
    const columns = (label: string): ColumnsProps["columns"] => [{ id: "only", content: <div>{label}</div> }];

    const { rerender } = render(<Columns columns={columns("before")} />);
    const before = screen.getByTestId("scroll-panel");
    rerender(<Columns columns={columns("after")} />);

    // The key joins the id to an empty region key, so it never changes: a
    // column that says nothing about remounting is never remounted under its
    // own content.
    expect(screen.getByTestId("scroll-panel")).toBe(before);
    expect(screen.getByText("after")).toBeInTheDocument();
  });
});
