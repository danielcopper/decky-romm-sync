import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { AdoptUnreadableModal } from "./AdoptUnreadableModal";
import type { UnreadableEntryResult } from "../types";

const BROKEN_LINK: UnreadableEntryResult = {
  success: false,
  reason: "unreadable_entry",
  message: "'Example Quest (U).gba' has this game's name but cannot be read",
  incoming: { name: "Example Quest (USA).gba", size_bytes: 100 },
  existing: [{ name: "Example Quest (U).gba", path: "/roms/gba/Example Quest (U).gba", removable: true }],
  truncated: false,
};

const UNREADABLE_CONTENT: UnreadableEntryResult = {
  ...BROKEN_LINK,
  existing: [{ name: "Example Quest (U).gba", path: "/roms/gba/Example Quest (U).gba", removable: false }],
};

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function renderModal(unreadable: UnreadableEntryResult = BROKEN_LINK) {
  const onChoice = vi.fn();
  const closeModal = vi.fn();
  const rendered = render(<AdoptUnreadableModal unreadable={unreadable} closeModal={closeModal} onChoice={onChoice} />);
  return { ...rendered, onChoice, closeModal };
}

// The dialog must say what is known and nothing more: the plugin could not read
// the entry, so it can neither offer it nor call it wrong.
const STATED = [
  { purpose: "names the entry that is in the way", phrases: ["Example Quest (U).gba"] },
  { purpose: "says plainly that it could not be read", phrases: ["cannot read it"] },
  { purpose: "says the outcome of downloading anyway, in copies", phrases: ["the copy it fetches"] },
  { purpose: "says nothing is renamed, moved or deleted by the download", phrases: ["renamed, moved or deleted"] },
];

describe("AdoptUnreadableModal — what it states", () => {
  it.each(STATED)("$purpose", ({ phrases }) => {
    const { container } = renderModal();
    for (const phrase of phrases) expect(container.textContent).toContain(phrase);
  });

  it("never claims to know whether the entry is this game", () => {
    const { container } = renderModal();
    expect(container.textContent).not.toContain("is not this game");
    expect(container.textContent).toContain("cannot tell");
  });
});

describe("AdoptUnreadableModal — when removal may be offered", () => {
  it("offers removal for a link proven to point nowhere, and says why that is safe", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("a link pointing nowhere");
    expect(container.textContent).toContain("deletes no game files");
    expect(buttonByText(container, "Remove the Broken Link and Download")).toBeTruthy();
  });

  it("never offers removal for something merely unreadable", () => {
    // The register's rule: an entry that only failed to be read may be the only
    // copy of something, and this dialog is the last thing between it and rm.
    const { container } = renderModal(UNREADABLE_CONTENT);
    const labels = Array.from(container.querySelectorAll("button")).map((b) => b.textContent);
    expect(labels).toEqual(["Download Example Quest (USA).gba Anyway", "Cancel"]);
    expect(container.textContent).toContain("could not be read");
  });

  it("never offers removal when more than one entry is listed", () => {
    // The wire carries one path; "remove them all" is a promise this dialog
    // cannot keep, least of all for a list that may have been capped.
    const { container } = renderModal({
      ...BROKEN_LINK,
      existing: [
        { name: "Example Quest (U).gba", path: "/roms/gba/Example Quest (U).gba", removable: true },
        { name: "Example Quest (E).gba", path: "/roms/gba/Example Quest (E).gba", removable: true },
      ],
    });
    const labels = Array.from(container.querySelectorAll("button")).map((b) => b.textContent);
    expect(labels).toEqual(["Download Example Quest (USA).gba Anyway", "Cancel"]);
  });

  it("never offers removal when the list was capped", () => {
    const { container } = renderModal({ ...BROKEN_LINK, truncated: true });
    const labels = Array.from(container.querySelectorAll("button")).map((b) => b.textContent);
    expect(labels).toEqual(["Download Example Quest (USA).gba Anyway", "Cancel"]);
    expect(container.textContent).toContain("there are more in this folder");
  });
});

describe("AdoptUnreadableModal — the exits", () => {
  it("Remove resolves with the path the backend proved removable", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Remove the Broken Link and Download"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith({ kind: "remove", path: "/roms/gba/Example Quest (U).gba" });
  });

  it("Download Anyway names no path, so nothing is removed", () => {
    const { container, onChoice } = renderModal();
    fireEvent.click(buttonByText(container, "Download Example Quest (USA).gba Anyway"));
    expect(onChoice).toHaveBeenCalledWith({ kind: "download" });
  });

  it("Cancel resolves without a path either", () => {
    const { container, onChoice } = renderModal();
    fireEvent.click(buttonByText(container, "Cancel"));
    expect(onChoice).toHaveBeenCalledWith({ kind: "cancel" });
  });
});
