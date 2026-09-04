/**
 * ListDetail tests — focus selects, and A stays with the row's own control.
 *
 * The @decky/ui stub in `src/test-setup.ts` renders every Focusable as a div
 * that forwards onFocus, so focus moving onto a row is driven with a real
 * focusin event rather than Steam's gamepad engine.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useState, type CSSProperties, type FC, type ReactNode } from "react";
import { ListDetail, type ListDetailItem, type ListDetailProps } from "./ListDetail";

const PLATFORMS = [
  { id: "n64", name: "Nintendo 64" },
  { id: "psx", name: "PlayStation" },
];

function platformItems(onToggle: (id: string) => void): ListDetailItem[] {
  return PLATFORMS.map((platform) => ({
    id: platform.id,
    render: (selected: boolean) => (
      <button onClick={() => onToggle(platform.id)}>
        {platform.name}
        {selected ? " (selected)" : ""}
      </button>
    ),
  }));
}

/** A page-shaped host: it owns the selection, as a real wide page does. */
const ControlledHost: FC<{ onSelect?: (id: string) => void; onToggle?: (id: string) => void }> = ({
  onSelect,
  onToggle,
}) => {
  const [selectedId, setSelectedId] = useState<string | null>("n64");
  return (
    <ListDetail
      items={platformItems(onToggle ?? (() => {}))}
      selectedId={selectedId}
      onSelect={(id) => {
        setSelectedId(id);
        onSelect?.(id);
      }}
      renderDetail={(id) => <div>detail for {id ?? "nothing"}</div>}
    />
  );
};

describe("ListDetail", () => {
  // The scroll-panel test swaps deckyUiInternals for a stub in the module
  // registry, where a `vi.doMock` otherwise stands for the rest of the file and
  // would reach any later test that imports dynamically.
  afterEach(() => {
    vi.doUnmock("../../utils/deckyUiInternals");
    vi.resetModules();
  });

  it("selects the row that takes focus and swaps the detail with it", () => {
    const onSelect = vi.fn();
    render(<ControlledHost onSelect={onSelect} />);

    expect(screen.getByText("detail for n64")).toBeInTheDocument();
    fireEvent.focusIn(screen.getByRole("button", { name: /PlayStation/ }));

    expect(onSelect).toHaveBeenCalledWith("psx");
    expect(screen.getByText("detail for psx")).toBeInTheDocument();
    expect(screen.queryByText("detail for n64")).not.toBeInTheDocument();
  });

  it("tells the row whether it is the selected one", () => {
    render(<ControlledHost />);

    expect(screen.getByRole("button", { name: "Nintendo 64 (selected)" })).toBeInTheDocument();
    fireEvent.focusIn(screen.getByRole("button", { name: "PlayStation" }));

    expect(screen.getByRole("button", { name: "PlayStation (selected)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nintendo 64" })).toBeInTheDocument();
  });

  it("reports a selection only when it changes", () => {
    const onSelect = vi.fn();
    render(<ControlledHost onSelect={onSelect} />);

    // focusin fires again for every move between controls inside one row, and on
    // the way back from the detail pane. A page may do real work on onSelect, so
    // the same id must not arrive twice.
    fireEvent.focusIn(screen.getByRole("button", { name: /Nintendo 64/ }));
    fireEvent.focusIn(screen.getByRole("button", { name: /Nintendo 64/ }));

    expect(onSelect).not.toHaveBeenCalled();

    fireEvent.focusIn(screen.getByRole("button", { name: /PlayStation/ }));
    fireEvent.focusIn(screen.getByRole("button", { name: /PlayStation/ }));

    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("leaves A to the row's own control", () => {
    const onSelect = vi.fn();
    const onToggle = vi.fn();
    render(<ControlledHost onSelect={onSelect} onToggle={onToggle} />);

    fireEvent.click(screen.getByRole("button", { name: "PlayStation" }));

    expect(onToggle).toHaveBeenCalledWith("psx");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("puts the list and the detail in two separately navigable regions", () => {
    render(<ControlledHost />);
    const [list, detail] = [...screen.getAllByTestId("focusable")[0]!.children] as HTMLElement[];

    expect(list).toContainElement(screen.getByRole("button", { name: /Nintendo 64/ }));
    expect(list).toContainElement(screen.getByRole("button", { name: /PlayStation/ }));
    expect(detail).toContainElement(screen.getByText("detail for n64"));
    expect(list?.style.overflow).toBe("auto");
    expect(detail?.style.overflow).toBe("auto");
  });

  it("gives each pane Steam's scroll panel, so the two scroll independently", async () => {
    vi.resetModules();
    vi.doMock("../../utils/deckyUiInternals", () => ({
      ScrollPanel: ({ style, children }: { style?: CSSProperties; children?: ReactNode }) => (
        <div data-testid="scroll-panel" style={style}>
          {children}
        </div>
      ),
    }));
    const Scrolling = (await import("./ListDetail")).ListDetail as FC<ListDetailProps>;

    render(
      <Scrolling
        items={platformItems(() => {})}
        selectedId="n64"
        onSelect={vi.fn()}
        renderDetail={() => <button>a detail row</button>}
      />,
    );
    const [list, detail] = screen.getAllByTestId("scroll-panel");

    // Two panels rather than one around both: the panes scroll independently, so
    // a long detail must not carry the list up with it.
    expect(list).toContainElement(screen.getByRole("button", { name: /Nintendo 64/ }));
    expect(detail).toContainElement(screen.getByRole("button", { name: "a detail row" }));
    expect(list?.style.width).toBe("264px");
    expect(detail?.style.height).toBe("100%");
  });

  it("opens a newly selected entry's detail at its own top", () => {
    // The pane is remounted on selection, so its scroll position cannot carry
    // over from the platform before it — a new pane opening part-way down its
    // own BIOS table is what this prevents. happy-dom lays nothing out, so what
    // is pinned is the remount; that the scroll actually returns to the top is
    // the browser's own behaviour for a fresh element.
    const { rerender } = render(
      <ListDetail
        items={platformItems(() => {})}
        selectedId="n64"
        onSelect={vi.fn()}
        renderDetail={(id) => <div>detail for {id ?? "nothing"}</div>}
      />,
    );
    const before = screen.getByText("detail for n64").closest("[style]");

    rerender(
      <ListDetail
        items={platformItems(() => {})}
        selectedId="psx"
        onSelect={vi.fn()}
        renderDetail={(id) => <div>detail for {id ?? "nothing"}</div>}
      />,
    );

    expect(screen.getByText("detail for psx")).toBeInTheDocument();
    expect(before).not.toBeInTheDocument();
  });

  it("puts a whole-list control above the rows without making it a selection", () => {
    const onSelect = vi.fn();
    const onEnableAll = vi.fn();
    render(
      <ListDetail
        items={platformItems(() => {})}
        listHeader={<button onClick={onEnableAll}>Enable all</button>}
        selectedId="n64"
        onSelect={onSelect}
        renderDetail={(id) => <div>detail for {id ?? "nothing"}</div>}
      />,
    );

    const header = screen.getByRole("button", { name: "Enable all" });
    // Reaching it must not report a selection: it belongs to the list, not to a
    // row of it, and a page may do real work on onSelect.
    fireEvent.focusIn(header);
    fireEvent.click(header);

    expect(onEnableAll).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("renders a detail for an empty selection", () => {
    render(
      <ListDetail
        items={[]}
        selectedId={null}
        onSelect={vi.fn()}
        renderDetail={(id) => <div>{id ?? "nothing"}</div>}
      />,
    );

    expect(screen.getByText("nothing")).toBeInTheDocument();
  });
});
