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

  it("says plainly that Replace deletes", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Replace deletes the files listed above");
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
