/**
 * ListDetail tests — focus selects, and A stays with the row's own control.
 *
 * The @decky/ui stub in `src/test-setup.ts` renders every Focusable as a div
 * that forwards onFocus, so focus moving onto a row is driven with a real
 * focusin event rather than Steam's gamepad engine.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useState, type FC } from "react";
import { ListDetail, type ListDetailItem } from "./ListDetail";

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
