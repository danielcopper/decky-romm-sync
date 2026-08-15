import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { AdoptUnusableModal } from "./AdoptUnusableModal";
import type { UnusableNamesakeResult } from "../types";

const FOLDER_IN_THE_WAY: UnusableNamesakeResult = {
  success: false,
  reason: "unusable_namesake",
  message: "'Example Quest (U)' has this game's name but is a folder",
  incoming: { name: "Example Quest (USA).gba", size_bytes: 100 },
  existing: [{ name: "Example Quest (U)", path: "/roms/gba/Example Quest (U)", kind: "dir" }],
  served_is_dir: false,
  truncated: false,
};

const FILE_IN_THE_WAY: UnusableNamesakeResult = {
  ...FOLDER_IN_THE_WAY,
  incoming: { name: "Example Quest (USA)", size_bytes: 100 },
  existing: [{ name: "Example Quest (U).cue", path: "/roms/psx/Example Quest (U).cue", kind: "file" }],
  served_is_dir: true,
};

const LINK_IN_THE_WAY: UnusableNamesakeResult = {
  ...FOLDER_IN_THE_WAY,
  existing: [{ name: "Example Quest (U).gba", path: "/roms/gba/Example Quest (U).gba", kind: "link" }],
};

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function renderModal(unusable: UnusableNamesakeResult = FOLDER_IN_THE_WAY) {
  const onChoice = vi.fn();
  const closeModal = vi.fn();
  const rendered = render(<AdoptUnusableModal unusable={unusable} closeModal={closeModal} onChoice={onChoice} />);
  return { ...rendered, onChoice, closeModal };
}

// What the user is owed here is unusually specific: they pressed a button that
// may have read "Use Existing Files", and the answer is that what is here
// cannot be used. Each row is one sentence that has to survive a rewrite of the
// copy.
const STATED = [
  { purpose: "names the entry that is in the way", phrases: ["Example Quest (U)"] },
  { purpose: "says which form the server sends", phrases: ["a single file"] },
  { purpose: "says the outcome of downloading anyway, in copies", phrases: ["two copies"] },
  { purpose: "says nothing is renamed, moved or deleted", phrases: ["renamed, moved or deleted"] },
  { purpose: "names the file it would fetch", phrases: ["Example Quest (USA).gba"] },
];

describe("AdoptUnusableModal — what it states", () => {
  it.each(STATED)("$purpose", ({ phrases }) => {
    const { container } = renderModal();
    for (const phrase of phrases) expect(container.textContent).toContain(phrase);
  });

  it("names each entry for what it is", () => {
    expect(renderModal(FOLDER_IN_THE_WAY).container.textContent).toContain("(folder)");
    expect(renderModal(FILE_IN_THE_WAY).container.textContent).toContain("(file)");
    expect(renderModal(LINK_IN_THE_WAY).container.textContent).toContain("(shortcut to somewhere else)");
  });

  it("says which form the server sends when it is the folder side", () => {
    // Not a copy of the row above: the served form swaps, and a sentence that
    // hardcoded either one would still pass every assertion up there.
    const { container } = renderModal(FILE_IN_THE_WAY);
    expect(container.textContent).toContain("Your server sends this game as a folder of several files");
    expect(container.textContent).toContain("Example Quest (U).cue");
  });

  it("never offers to use, replace or remove what it just said cannot be used", () => {
    // A symlink is the case that most invites a Remove button: it looks like a
    // stray. There is no proof it holds no data, so no such button exists.
    const { container } = renderModal(LINK_IN_THE_WAY);
    const labels = Array.from(container.querySelectorAll("button")).map((b) => b.textContent);
    expect(labels).toEqual(["Download Example Quest (USA).gba Anyway", "Cancel"]);
  });

  it("states a cut list rather than implying it is complete", () => {
    const { container } = renderModal({
      ...FOLDER_IN_THE_WAY,
      existing: [
        { name: "Example Quest (U)", path: "/roms/gba/Example Quest (U)", kind: "dir" },
        { name: "Example Quest (E)", path: "/roms/gba/Example Quest (E)", kind: "dir" },
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
  { button: "Download Example Quest (USA).gba Anyway", resolves: "download" },
  { button: "Cancel", resolves: "cancel" },
];

describe("AdoptUnusableModal — the two exits", () => {
  it.each(EXITS)("$button closes the modal and resolves $resolves", ({ button, resolves }) => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, button));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith(resolves);
  });
});
