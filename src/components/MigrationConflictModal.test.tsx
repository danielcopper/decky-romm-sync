import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { MigrationConflictModal } from "./MigrationConflictModal";

// The global @decky/ui mock renders ModalRoot as a pass-through <div> and
// DialogButton as a <button> — so the three actions surface as three native
// buttons with their text content as the accessible label.
function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent === text,
  );
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

describe("MigrationConflictModal", () => {
  it("renders the conflict count interpolated into the message", () => {
    const { container } = render(
      <MigrationConflictModal conflictCount={7} onChoice={vi.fn()} />,
    );
    expect(container.textContent).toContain("7 file(s) already exist");
    expect(container.textContent).toContain("Files Already Exist");
  });

  it("Overwrite button: closes modal then invokes onChoice('overwrite')", () => {
    const closeModal = vi.fn();
    const onChoice = vi.fn();
    const { container } = render(
      <MigrationConflictModal
        conflictCount={2}
        closeModal={closeModal}
        onChoice={onChoice}
      />,
    );
    fireEvent.click(buttonByText(container, "Overwrite"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("overwrite");
  });

  it("Skip button: closes modal then invokes onChoice('skip')", () => {
    const closeModal = vi.fn();
    const onChoice = vi.fn();
    const { container } = render(
      <MigrationConflictModal
        conflictCount={2}
        closeModal={closeModal}
        onChoice={onChoice}
      />,
    );
    fireEvent.click(buttonByText(container, "Skip"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("skip");
  });

  it("Cancel button: closes modal and does NOT invoke onChoice", () => {
    const closeModal = vi.fn();
    const onChoice = vi.fn();
    const { container } = render(
      <MigrationConflictModal
        conflictCount={2}
        closeModal={closeModal}
        onChoice={onChoice}
      />,
    );
    fireEvent.click(buttonByText(container, "Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).not.toHaveBeenCalled();
  });

  it("closeModal is optional — Overwrite still invokes onChoice when closeModal is undefined", () => {
    const onChoice = vi.fn();
    const { container } = render(
      <MigrationConflictModal conflictCount={1} onChoice={onChoice} />,
    );
    expect(() =>
      fireEvent.click(buttonByText(container, "Overwrite")),
    ).not.toThrow();
    expect(onChoice).toHaveBeenCalledWith("overwrite");
  });

  it("closeModal is optional — Cancel is a no-op when closeModal is undefined", () => {
    const onChoice = vi.fn();
    const { container } = render(
      <MigrationConflictModal conflictCount={1} onChoice={onChoice} />,
    );
    expect(() =>
      fireEvent.click(buttonByText(container, "Cancel")),
    ).not.toThrow();
    expect(onChoice).not.toHaveBeenCalled();
  });
});
