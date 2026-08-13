import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { AdoptCollisionModal } from "./AdoptCollisionModal";
import type { RenameCollision } from "../types";

const COLLISIONS: RenameCollision[] = [
  { name: "Game (USA).srm", path: "/saves/snes/Game (USA).srm", kind: "save" },
  { name: "Game (USA).state", path: "/states/Game (USA).state", kind: "savestate" },
];

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function renderModal(collisions: RenameCollision[] = COLLISIONS) {
  const onChoice = vi.fn();
  const closeModal = vi.fn();
  const rendered = render(<AdoptCollisionModal collisions={collisions} closeModal={closeModal} onChoice={onChoice} />);
  return { ...rendered, onChoice, closeModal };
}

describe("AdoptCollisionModal — what it states", () => {
  it("lists every collision, not just the first", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Game (USA).srm");
    expect(container.textContent).toContain("Game (USA).state");
  });

  it("says which kind of file each collision is", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("save");
    expect(container.textContent).toContain("savestate");
  });

  it("says nothing has been moved yet", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Nothing has been moved");
  });

  it("states the orphaning that Keep produces rather than implying a clean move", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("nothing will be reading them either");
  });

  it("tells the user where a replaced file goes, at the moment they choose", () => {
    // This is the only surface shown while deciding, so it is the only place the
    // recovery net can be learned about — and the whole reason Replace routes
    // through the backup funnel rather than deleting.
    const { container } = renderModal();
    expect(container.textContent).toContain(".romm-backup");
    expect(container.textContent).toContain("put one back by hand");
  });

  it("never claims Replace deletes, because it does not", () => {
    // Guards the contradiction this dialog carried against the user guide: the
    // guide said nothing is destroyed while the dialog said it is deleted.
    const { container } = renderModal();
    expect(container.textContent).toContain("Replace does not delete");
    expect(container.textContent).not.toContain("Replace deletes");
  });
});

describe("AdoptCollisionModal — the one decision for the whole set", () => {
  it("Replace Them closes the modal and resolves overwrite", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Replace Them"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("overwrite");
  });

  it("Keep Them closes the modal and resolves keep", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Keep Them"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("keep");
  });

  it("Cancel resolves cancel", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("cancel");
  });
});
