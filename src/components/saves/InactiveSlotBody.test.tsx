import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { InactiveSlotBody, type InactiveSlotBodyProps } from "./InactiveSlotBody";
import type { SlotSaveFile } from "../../types";

// Override the global @decky/ui DialogButton stub so it forwards `disabled` —
// the global passthrough <button> only wires `onClick` and silently drops
// disabled, which masks the prop-driven disable behavior we want to assert.
type AnyProps = Record<string, unknown> & { children?: unknown };
vi.mock("@decky/ui", () => {
  const passthrough = (tag: string) => (props: AnyProps) => createElement(tag, props, props.children as never);
  return {
    DialogButton: ({
      children,
      onClick,
      disabled,
    }: AnyProps & {
      onClick?: () => void;
      disabled?: boolean;
    }) => createElement("button", { onClick, disabled }, children as never),
    Focusable: passthrough("div"),
  };
});

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

function defaultProps(overrides: Partial<InactiveSlotBodyProps> = {}): InactiveSlotBodyProps {
  return {
    loadingSlot: false,
    slotFiles: null,
    switching: false,
    switchError: null,
    isOffline: false,
    isLegacy: false,
    handleActivate: vi.fn(),
    handleDelete: vi.fn(),
    deleting: false,
    ...overrides,
  };
}

describe("InactiveSlotBody", () => {
  it("renders 'Loading...' when loadingSlot is true", () => {
    const { container } = render(<InactiveSlotBody {...defaultProps({ loadingSlot: true })} />);
    expect(container.textContent).toContain("Loading...");
  });

  it("renders no save list rows when slotFiles is null and not loading", () => {
    const { container } = render(<InactiveSlotBody {...defaultProps()} />);
    // Neither the loading placeholder nor the empty placeholder
    expect(container.textContent).not.toContain("Loading...");
    expect(container.textContent).not.toContain("No saves in this slot");
  });

  it("renders 'No saves in this slot' when slotFiles is an empty array", () => {
    const { container } = render(<InactiveSlotBody {...defaultProps({ slotFiles: [] })} />);
    expect(container.textContent).toContain("No saves in this slot");
  });

  it("renders one row per file when slotFiles is populated", () => {
    const { container } = render(
      <InactiveSlotBody
        {...defaultProps({
          slotFiles: [makeFile({ id: 1, filename: "a.srm" }), makeFile({ id: 2, filename: "b.srm" })],
        })}
      />,
    );
    expect(container.textContent).toContain("a.srm");
    expect(container.textContent).toContain("b.srm");
  });

  it("button label says 'Activate Slot' by default", () => {
    const { getByText } = render(<InactiveSlotBody {...defaultProps()} />);
    expect(getByText("Activate Slot")).toBeInTheDocument();
  });

  it("button label says 'Switching...' when switching is true", () => {
    const { getByText, queryByText } = render(<InactiveSlotBody {...defaultProps({ switching: true })} />);
    expect(getByText("Switching...")).toBeInTheDocument();
    expect(queryByText("Activate Slot")).toBeNull();
  });

  it("button label says 'Delete Slot' by default", () => {
    const { getByText } = render(<InactiveSlotBody {...defaultProps()} />);
    expect(getByText("Delete Slot")).toBeInTheDocument();
  });

  it("button label says 'Deleting...' when deleting is true", () => {
    const { getByText, queryByText } = render(<InactiveSlotBody {...defaultProps({ deleting: true })} />);
    expect(getByText("Deleting...")).toBeInTheDocument();
    expect(queryByText("Delete Slot")).toBeNull();
  });

  it("disables Activate when switching is true", () => {
    const { getByText } = render(<InactiveSlotBody {...defaultProps({ switching: true })} />);
    expect(getByText("Switching...")).toBeDisabled();
  });

  it("disables Activate when isOffline is true", () => {
    const { getByText } = render(<InactiveSlotBody {...defaultProps({ isOffline: true })} />);
    expect(getByText("Activate Slot")).toBeDisabled();
  });

  it("enables Activate when neither switching nor offline", () => {
    const { getByText } = render(<InactiveSlotBody {...defaultProps()} />);
    expect(getByText("Activate Slot")).not.toBeDisabled();
  });

  it("disables Delete when deleting is true", () => {
    const { getByText } = render(<InactiveSlotBody {...defaultProps({ deleting: true })} />);
    expect(getByText("Deleting...")).toBeDisabled();
  });

  it("disables Delete when switching is true (in-flight switch blocks delete)", () => {
    const { getByText } = render(<InactiveSlotBody {...defaultProps({ switching: true })} />);
    expect(getByText("Delete Slot")).toBeDisabled();
  });

  it("calls handleActivate when Activate is clicked", () => {
    const handleActivate = vi.fn();
    const { getByText } = render(<InactiveSlotBody {...defaultProps({ handleActivate })} />);
    fireEvent.click(getByText("Activate Slot"));
    expect(handleActivate).toHaveBeenCalledTimes(1);
  });

  it("calls handleDelete when Delete is clicked", () => {
    const handleDelete = vi.fn();
    const { getByText } = render(<InactiveSlotBody {...defaultProps({ handleDelete })} />);
    fireEvent.click(getByText("Delete Slot"));
    expect(handleDelete).toHaveBeenCalledTimes(1);
  });

  it("shows offline hint when isOffline is true", () => {
    const { container } = render(<InactiveSlotBody {...defaultProps({ isOffline: true })} />);
    expect(container.textContent).toContain("Offline — slot switching unavailable");
  });

  it("hides offline hint when not offline", () => {
    const { container } = render(<InactiveSlotBody {...defaultProps()} />);
    expect(container.textContent).not.toContain("Offline — slot switching unavailable");
  });

  it("shows switchError line when switchError is set", () => {
    const { container } = render(<InactiveSlotBody {...defaultProps({ switchError: "Something went wrong" })} />);
    expect(container.textContent).toContain("Something went wrong");
  });

  it("hides switchError line when switchError is null", () => {
    const { container } = render(<InactiveSlotBody {...defaultProps()} />);
    // No error text — just controls
    const errorEls = container.querySelectorAll("div");
    const errorTextNodes = Array.from(errorEls).filter(
      (el) => el.style.color === "rgb(217, 65, 38)" || el.style.color === "#d94126",
    );
    expect(errorTextNodes.length).toBe(0);
  });

  // The slot-less legacy bucket is fully read-only (#1276 retired switching in;
  // #1478 removed deletion): the panel drops BOTH the Activate and Delete
  // controls and the switch-only hints while keeping the file list.
  describe("read-only legacy bucket (isLegacy=true)", () => {
    it("hides both the Activate and Delete buttons", () => {
      const { queryByText } = render(<InactiveSlotBody {...defaultProps({ isLegacy: true })} />);
      expect(queryByText("Activate Slot")).toBeNull();
      expect(queryByText("Switching...")).toBeNull();
      expect(queryByText("Delete Slot")).toBeNull();
      expect(queryByText("Deleting...")).toBeNull();
    });

    it("still lists the slot's save files", () => {
      const { container } = render(
        <InactiveSlotBody {...defaultProps({ isLegacy: true, slotFiles: [makeFile({ filename: "legacy.srm" })] })} />,
      );
      expect(container.textContent).toContain("legacy.srm");
    });

    it("hides the offline switching hint even when offline", () => {
      const { container } = render(<InactiveSlotBody {...defaultProps({ isLegacy: true, isOffline: true })} />);
      expect(container.textContent).not.toContain("Offline — slot switching unavailable");
    });

    it("hides the switchError line", () => {
      const { container } = render(
        <InactiveSlotBody {...defaultProps({ isLegacy: true, switchError: "Something went wrong" })} />,
      );
      expect(container.textContent).not.toContain("Something went wrong");
    });
  });
});
