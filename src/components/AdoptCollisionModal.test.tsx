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

// Every sentence this dialog owes the user, and what each one is for. The test
// name IS the purpose, so parameterizing costs no legibility — and the two that
// carry the most weight stay findable by it: the recovery net (the only surface
// where it can be learned) and the plain statement that Replace does not delete
// (the sentence that once contradicted the user guide).
const STATED = [
  { purpose: "lists every collision, not just the first", phrases: ["Game (USA).srm", "Game (USA).state"] },
  { purpose: "says which kind of file each collision is", phrases: ["save", "savestate"] },
  { purpose: "says nothing has been moved yet", phrases: ["Nothing has been moved"] },
  {
    purpose: "states the orphaning Keep produces rather than implying a clean move",
    phrases: ["nothing will be reading them either"],
  },
  {
    purpose: "tells the user where a replaced file goes, at the moment they choose",
    phrases: [".romm-backup", "put one back by hand"],
  },
  { purpose: "says plainly that Replace does not delete", phrases: ["Replace does not delete"] },
];

describe("AdoptCollisionModal — what it states", () => {
  it.each(STATED)("$purpose", ({ phrases }) => {
    const { container } = renderModal();
    for (const phrase of phrases) expect(container.textContent).toContain(phrase);
  });

  it("never claims Replace deletes, because it does not", () => {
    // Separate from the table on purpose: this is the only assertion here that
    // forbids rather than requires, and it guards a specific regression — the
    // dialog once said the files are deleted while the user guide shipped in the
    // same commit said they are not.
    const { container } = renderModal();
    expect(container.textContent).not.toContain("Replace deletes");
  });
});

// One answer covers the whole colliding set, so the three exits differ only in
// which answer they resolve — the table states that rather than repeating it.
const EXITS = [
  { button: "Replace Them", resolves: "overwrite" },
  { button: "Keep Them", resolves: "keep" },
  { button: "Cancel", resolves: "cancel" },
];

describe("AdoptCollisionModal — the one decision for the whole set", () => {
  it.each(EXITS)("$button closes the modal and resolves $resolves", ({ button, resolves }) => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, button));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith(resolves);
  });
});
