import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { AdoptShapeConflictModal } from "./AdoptShapeConflictModal";
import type { ShapeConflictResult } from "../types";

const FOLDER_IN_THE_WAY: ShapeConflictResult = {
  success: false,
  reason: "shape_conflict",
  message: "'Game (U)' has this game's name but is a folder",
  incoming: { name: "Game (USA).gba", size_bytes: 100 },
  existing: [{ name: "Game (U)", path: "/roms/gba/Game (U)", is_dir: true }],
  served_is_dir: false,
  truncated: false,
};

const FILE_IN_THE_WAY: ShapeConflictResult = {
  ...FOLDER_IN_THE_WAY,
  incoming: { name: "Game (USA)", size_bytes: 100 },
  existing: [{ name: "Game (U).cue", path: "/roms/psx/Game (U).cue", is_dir: false }],
  served_is_dir: true,
};

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function renderModal(conflict: ShapeConflictResult = FOLDER_IN_THE_WAY) {
  const onChoice = vi.fn();
  const closeModal = vi.fn();
  const rendered = render(<AdoptShapeConflictModal conflict={conflict} closeModal={closeModal} onChoice={onChoice} />);
  return { ...rendered, onChoice, closeModal };
}

// What the user is owed here is unusually specific: they pressed a button that
// may have read "Use Existing Files", and the answer is that those files cannot
// be used. Each row is one sentence that has to survive a rewrite of the copy.
const STATED = [
  { purpose: "names the entry that is in the way", phrases: ["Game (U)"] },
  { purpose: "says which shape the server sends and which is here", phrases: ["a single file", "a folder"] },
  { purpose: "says the outcome of downloading anyway, in copies", phrases: ["two copies"] },
  { purpose: "says nothing is renamed, moved or deleted", phrases: ["renamed, moved or deleted"] },
  { purpose: "names the file it would fetch", phrases: ["Game (USA).gba"] },
];

describe("AdoptShapeConflictModal — what it states", () => {
  it.each(STATED)("$purpose", ({ phrases }) => {
    const { container } = renderModal();
    for (const phrase of phrases) expect(container.textContent).toContain(phrase);
  });

  it("reads the other way round when the server is the one sending a folder", () => {
    // Not a copy of the row above: the two shapes swap sides, and a sentence
    // that hardcoded either one would still pass every assertion up there.
    const { container } = renderModal(FILE_IN_THE_WAY);
    expect(container.textContent).toContain("Your server sends this game as a folder of several files");
    expect(container.textContent).toContain("what is in this folder is a single file");
    expect(container.textContent).toContain("Game (U).cue");
  });

  it("never offers to use, replace or remove what it just said cannot be used", () => {
    const { container } = renderModal();
    const labels = Array.from(container.querySelectorAll("button")).map((b) => b.textContent);
    expect(labels).toEqual(["Download Game (USA).gba Anyway", "Cancel"]);
  });

  it("states a cut list rather than implying it is complete", () => {
    const { container } = renderModal({
      ...FOLDER_IN_THE_WAY,
      existing: [
        { name: "Game (U)", path: "/roms/gba/Game (U)", is_dir: true },
        { name: "Game (E)", path: "/roms/gba/Game (E)", is_dir: true },
      ],
      truncated: true,
    });
    expect(container.textContent).toContain("there are more in this folder");
  });

  it("says nothing about a cut list when the list is whole", () => {
    const { container } = renderModal();
    expect(container.textContent).not.toContain("there are more in this folder");
  });
});

// Two exits, and neither touches a file: one fetches the server's copy beside
// what is there, the other does nothing at all.
const EXITS = [
  { button: "Download Game (USA).gba Anyway", resolves: "download" },
  { button: "Cancel", resolves: "cancel" },
];

describe("AdoptShapeConflictModal — the two exits", () => {
  it.each(EXITS)("$button closes the modal and resolves $resolves", ({ button, resolves }) => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, button));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith(resolves);
  });
});
