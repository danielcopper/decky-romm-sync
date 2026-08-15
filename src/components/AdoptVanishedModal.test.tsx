import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { AdoptVanishedModal } from "./AdoptVanishedModal";
import type { CandidateVanishedResult } from "../types";

const VANISHED: CandidateVanishedResult = {
  success: false,
  reason: "candidate_vanished",
  message: "What was found on this device is no longer there, or can no longer be matched to this game",
  incoming: { name: "Example Quest (USA).gba", size_bytes: 100 },
};

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function renderModal() {
  const onChoice = vi.fn();
  const closeModal = vi.fn();
  const rendered = render(<AdoptVanishedModal vanished={VANISHED} closeModal={closeModal} onChoice={onChoice} />);
  return { ...rendered, onChoice, closeModal };
}

describe("AdoptVanishedModal — what it states", () => {
  it("says the page found a copy and that looking again turns up nothing", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("found a copy on this device");
    expect(container.textContent).toContain("turns up nothing that matches");
  });

  it("says nothing on the device was changed", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Nothing has been changed on your device");
  });

  it("names the file it would fetch", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Example Quest (USA).gba");
  });

  it("names no cause, because it knows of none", () => {
    // The backstop knows the two searches disagreed, not why. One way they can:
    // the page matches `roms.fs_name` while the click path matches the name
    // derived from the server's payload, so a rename on the server leaves the
    // entry sitting untouched in the folder while the click search finds
    // nothing — and "you moved, renamed or deleted it" is then simply false.
    //
    // Asserted against the words a cause would be given in, not against
    // "error"/"failed" — the sentence this replaced passed those and still
    // claimed a cause.
    const { container } = renderModal();
    for (const cause of ["moved", "renamed", "deleted", "removed", "because"]) {
      expect(container.textContent).not.toContain(cause);
    }
  });
});

const EXITS = [
  { button: "Download Example Quest (USA).gba", resolves: "download" },
  { button: "Cancel", resolves: "cancel" },
];

describe("AdoptVanishedModal — the two exits", () => {
  it.each(EXITS)("$button closes the modal and resolves $resolves", ({ button, resolves }) => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, button));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith(resolves);
  });
});
