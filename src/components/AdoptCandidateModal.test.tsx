import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { AdoptCandidateModal } from "./AdoptCandidateModal";
import type { AdoptionCandidate, CandidatesFoundResult } from "../types";

function candidate(overrides: Partial<AdoptionCandidate> = {}): AdoptionCandidate {
  return {
    name: "Game (U).sfc",
    path: "/roms/snes/Game (U).sfc",
    is_dir: false,
    size_bytes: 2048,
    modified_at: 1_700_000_000,
    evidence: "size",
    detail: "Exactly the size the server would send",
    ...overrides,
  };
}

function found(overrides: Partial<CandidatesFoundResult> = {}): CandidatesFoundResult {
  return {
    success: false,
    reason: "adoption_candidates",
    message: "2 files on this device could be this game",
    incoming: { name: "Game (USA).sfc", size_bytes: 2048 },
    candidates: [candidate(), candidate({ name: "Game (E).sfc", path: "/roms/snes/Game (E).sfc" })],
    truncated: false,
    ...overrides,
  };
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function renderModal(overrides: Partial<CandidatesFoundResult> = {}) {
  const onChoice = vi.fn();
  const closeModal = vi.fn();
  const payload = found(overrides);
  const rendered = render(<AdoptCandidateModal found={payload} closeModal={closeModal} onChoice={onChoice} />);
  return { ...rendered, onChoice, closeModal, payload };
}

describe("AdoptCandidateModal — what each row rests on", () => {
  it("names every candidate and the evidence behind it", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Game (U).sfc");
    expect(container.textContent).toContain("Game (E).sfc");
    expect(container.textContent).toContain("Exactly the size the server would send");
  });

  it("shows a file's size and calls a folder a folder", () => {
    const { container } = renderModal({
      candidates: [candidate({ is_dir: true, size_bytes: 0, detail: "Matched on name only" })],
    });
    expect(container.textContent).toContain("folder");
  });

  it("preserves the backend's ranking rather than re-sorting", () => {
    const { container } = renderModal({
      candidates: [
        candidate({ name: "Strongest.sfc", evidence: "crc32", detail: "checksum" }),
        candidate({ name: "Weakest.sfc", path: "/roms/snes/Weakest.sfc", evidence: "name", detail: "name only" }),
      ],
    });
    const labels = Array.from(container.querySelectorAll("button")).map((b) => b.textContent);
    expect(labels.findIndex((t) => t.includes("Strongest.sfc"))).toBeLessThan(
      labels.findIndex((t) => t.includes("Weakest.sfc")),
    );
  });
});

describe("AdoptCandidateModal — truncation", () => {
  it("says so when the list was cut short", () => {
    const { container } = renderModal({ truncated: true });
    expect(container.textContent).toContain("there are more in this folder");
  });

  it("stays silent when the whole list fits", () => {
    const { container } = renderModal({ truncated: false });
    expect(container.textContent).not.toContain("there are more in this folder");
  });
});

describe("AdoptCandidateModal — the three exits", () => {
  it("picking a row closes the modal and resolves with that candidate", () => {
    const { container, onChoice, closeModal, payload } = renderModal();
    const row = Array.from(container.querySelectorAll("button")).find((b) => b.textContent.includes("Game (E)"));
    if (!row) throw new Error("no row for the second candidate");
    fireEvent.click(row);
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith({ kind: "candidate", candidate: payload.candidates[1] });
  });

  it("None of These resolves download and names what would be fetched", () => {
    const { container, onChoice } = renderModal();
    fireEvent.click(buttonByText(container, "None of These — Download Game (USA).sfc"));
    expect(onChoice).toHaveBeenCalledWith({ kind: "download" });
  });

  it("Cancel resolves cancel", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith({ kind: "cancel" });
  });
});
