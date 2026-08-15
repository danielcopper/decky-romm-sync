import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, act, waitFor } from "@testing-library/react";
import { AdoptExistingModal, comparisonForCandidate } from "./AdoptExistingModal";
import { emitDeckyEvent, deckyEventListenerCount } from "../test-utils/decky-api-mock";
import { verifyExistingContent } from "../api/backend";
import type { TargetOccupiedResult, VerifyContentResult, VerifyProgressEvent } from "../types";

vi.mock("../api/backend", () => ({
  verifyExistingContent: vi.fn(),
  debugLog: vi.fn(async () => {}),
}));

const ROM_ID = 42;

function occupied(overrides: Partial<TargetOccupiedResult> = {}): TargetOccupiedResult {
  return {
    success: false,
    reason: "target_occupied",
    message: "A file named 'Game.sfc' is already in place",
    existing: {
      name: "Game.sfc",
      path: "/roms/snes/Game.sfc",
      kind: "file",
      size_bytes: 2048,
      modified_at: 1_700_000_000,
    },
    incoming: { name: "Game.sfc", size_bytes: 1024 },
    sizes_match: false,
    adoptable: true,
    ...overrides,
  };
}

/** A shortcut at the ROM's own path: never adoptable, and its size is the path's. */
function linkOccupied(): TargetOccupiedResult {
  return occupied({
    message: "A shortcut named 'Game.sfc' is already in place",
    existing: { ...occupied().existing, kind: "link", size_bytes: 19 },
    sizes_match: null,
    adoptable: false,
  });
}

/** A named pipe or socket: there, and with no kind the backend will claim. */
function unknownOccupied(): TargetOccupiedResult {
  return occupied({
    message: "Something named 'Game.sfc' is already in place",
    existing: { ...occupied().existing, kind: null, size_bytes: 0 },
    sizes_match: null,
    adoptable: false,
  });
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  if (!btn) throw new Error(`button "${text}" not found`);
  return btn as HTMLButtonElement;
}

function renderModal(props: Partial<Parameters<typeof AdoptExistingModal>[0]> = {}) {
  const onChoice = vi.fn();
  const closeModal = vi.fn();
  const rendered = render(
    <AdoptExistingModal
      romId={ROM_ID}
      occupied={props.occupied ?? occupied()}
      candidatePath={props.candidatePath}
      closeModal={props.closeModal ?? closeModal}
      onChoice={props.onChoice ?? onChoice}
    />,
  );
  return { ...rendered, onChoice: props.onChoice ?? onChoice, closeModal: props.closeModal ?? closeModal };
}

beforeEach(() => {
  vi.mocked(verifyExistingContent).mockReset();
});

describe("AdoptExistingModal — the comparison", () => {
  it("names both sides and states how the sizes relate", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Game.sfc");
    // Not two bare numbers: the difference is stated for the user.
    expect(container.textContent).toContain("larger than what the server would send");
  });

  it("says the sizes match when they do", () => {
    const { container } = renderModal({
      occupied: occupied({ sizes_match: true, incoming: { name: "Game.sfc", size_bytes: 2048 } }),
    });
    expect(container.textContent).toContain("Both are the same size");
  });

  it("says the comparison cannot be made when the server stated no size", () => {
    const { container } = renderModal({
      occupied: occupied({ sizes_match: null, incoming: { name: "Game.sfc", size_bytes: 0 } }),
    });
    expect(container.textContent).toContain("did not state a size");
    expect(container.textContent).toContain("Size unknown");
  });

  it("calls the occupying content a folder when it is one", () => {
    const { container } = renderModal({
      occupied: occupied({ existing: { ...occupied().existing, kind: "dir" } }),
    });
    expect(container.textContent).toContain("A folder is already where this game would be downloaded");
  });

  it("calls a shortcut a shortcut, not a file", () => {
    // It was described as a file, which is what made adopting one look sensible.
    const { container } = renderModal({ occupied: linkOccupied() });
    expect(container.textContent).toContain("A shortcut to somewhere else is already where this game would be");
    expect(container.textContent).not.toContain("A file is already where");
  });

  it("does not print a shortcut's byte count, let alone compare it", () => {
    // `lstat` reports the length of the path a link stores. Printing that beside
    // the server's real size reads as a comparison however it is disclaimed two
    // lines below, so the number does not appear at all.
    const { container } = renderModal({ occupied: linkOccupied() });
    expect(container.textContent).toContain("Shortcut — no size of its own");
    expect(container.textContent).toContain("nothing here to compare");
    expect(container.textContent).not.toContain("19 B");
    for (const verdict of ["Both are the same size", "larger than what the server", "smaller than what the server"]) {
      expect(container.textContent).not.toContain(verdict);
    }
  });

  it("states when a file was last changed", () => {
    const { container } = renderModal();
    expect(container.textContent).toContain("Last changed");
  });

  it("withholds the timestamp for anything whose mtime is not the game's", () => {
    // `lstat` gives a link's own mtime — when it was pointed somewhere, not when
    // the game was touched — and under a column headed "On this device" that
    // reads as the game's. The size beside it is already withheld for the same
    // reason; this was the one line left implying otherwise.
    for (const occupied of [linkOccupied(), unknownOccupied()]) {
      const { container } = renderModal({ occupied });
      expect(container.textContent).not.toContain("Last changed");
    }
  });

  it("has no word and no number for something that is neither file, folder nor shortcut", () => {
    const { container } = renderModal({ occupied: unknownOccupied() });
    expect(container.textContent).toContain("A thing is already where this game would be downloaded");
    expect(container.textContent).toContain("No size to show");
    expect(container.textContent).toContain("nothing here to compare");
    expect(container.textContent).not.toContain("0 B");
  });
});

describe("AdoptExistingModal — the three exits", () => {
  it("Use These Files closes the modal and resolves adopt", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Use These Files"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("adopt");
  });

  it("Cancel closes the modal and resolves cancel", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Cancel"));
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("cancel");
  });

  it("Download Instead does NOT replace on the first press", () => {
    const { container, onChoice, closeModal } = renderModal();
    fireEvent.click(buttonByText(container, "Download Instead"));
    expect(onChoice).not.toHaveBeenCalled();
    expect(closeModal).not.toHaveBeenCalled();
  });

  it("the second confirmation names the deletion before replacing", () => {
    const { container, onChoice } = renderModal();
    fireEvent.click(buttonByText(container, "Download Instead"));
    expect(container.textContent).toContain("Downloading deletes the file that is here now");
    expect(container.textContent).toContain("Game.sfc");
    fireEvent.click(buttonByText(container, "Delete and Download"));
    expect(onChoice).toHaveBeenCalledWith("replace");
  });

  it("Go Back leaves the confirmation without replacing", () => {
    const { container, onChoice } = renderModal();
    fireEvent.click(buttonByText(container, "Download Instead"));
    fireEvent.click(buttonByText(container, "Go Back"));
    expect(onChoice).not.toHaveBeenCalled();
    expect(buttonByText(container, "Use These Files")).toBeTruthy();
  });

  it("adopt is disabled when the content is the wrong shape for this ROM", () => {
    const { container } = renderModal({
      occupied: occupied({ adoptable: false, existing: { ...occupied().existing, kind: "dir" } }),
    });
    expect(buttonByText(container, "Can't use this folder for this game").disabled).toBe(true);
  });

  it("adopt is disabled for a shortcut, and says so as a shortcut", () => {
    const { container } = renderModal({ occupied: linkOccupied() });
    expect(buttonByText(container, "Can't use this shortcut to somewhere else for this game").disabled).toBe(true);
  });

  it("the replace confirmation for a shortcut describes what is actually destroyed", () => {
    // "If it is your own dump, patch or romhack, it is gone" is false here: only
    // the shortcut is unlinked, and whatever it points at is untouched.
    const { container, onChoice } = renderModal({ occupied: linkOccupied() });
    fireEvent.click(buttonByText(container, "Download Instead"));
    expect(container.textContent).toContain("Downloading deletes the shortcut that is here now");
    expect(container.textContent).toContain("Whatever it points at is left alone");
    expect(container.textContent).not.toContain("If it is your own dump");
    fireEvent.click(buttonByText(container, "Delete and Download"));
    expect(onChoice).toHaveBeenCalledWith("replace");
  });

  it("the replace confirmation claims no more about a kindless entry than that it goes", () => {
    // It is neither a dump the user may have made nor a shortcut whose target
    // survives, so the confirmation says only what is certain.
    const { container } = renderModal({ occupied: unknownOccupied() });
    fireEvent.click(buttonByText(container, "Download Instead"));
    expect(container.textContent).toContain("Downloading removes what is here now");
    expect(container.textContent).toContain("Tender cannot tell what it is, only that it goes");
    expect(container.textContent).not.toContain("If it is your own dump");
    expect(container.textContent).not.toContain("0 B");
  });
});

describe("AdoptExistingModal — the content check", () => {
  it("renders a match distinctly", async () => {
    vi.mocked(verifyExistingContent).mockResolvedValue({
      status: "match",
      message: "These files match the ones on the server",
      differences: [],
    });
    const { container } = renderModal();

    fireEvent.click(buttonByText(container, "Check Against Server"));

    await waitFor(() => expect(container.textContent).toContain("These files match the ones on the server"));
    // A null candidate path checks the game's own location.
    expect(vi.mocked(verifyExistingContent)).toHaveBeenCalledWith(ROM_ID, null);
  });

  it("names what differed on a mismatch", async () => {
    vi.mocked(verifyExistingContent).mockResolvedValue({
      status: "mismatch",
      message: "These files differ from the ones on the server",
      differences: [{ name: "Game.sfc", detail: "contents differ from the server's copy" }],
    });
    const { container } = renderModal();

    fireEvent.click(buttonByText(container, "Check Against Server"));

    await waitFor(() => expect(container.textContent).toContain("Game.sfc: contents differ from the server's copy"));
  });

  it("gives every difference its own line", async () => {
    vi.mocked(verifyExistingContent).mockResolvedValue({
      status: "mismatch",
      message: "These files differ from the ones on the server",
      differences: [
        { name: "disc1.bin", detail: "expected 4096 bytes, found 1024" },
        { name: "disc2.bin", detail: "missing" },
      ],
    });
    const { container } = renderModal();

    fireEvent.click(buttonByText(container, "Check Against Server"));

    await waitFor(() => expect(container.textContent).toContain("disc2.bin: missing"));
    // Run together they read as one block; each finding is its own element.
    const lines = [...container.querySelectorAll("div")].map((node) => node.textContent);
    expect(lines).toContain("disc1.bin: expected 4096 bytes, found 1024");
    expect(lines).toContain("disc2.bin: missing");
  });

  it("renders 'the server cannot confirm this' as its own outcome", async () => {
    const unverifiable: VerifyContentResult = {
      status: "unverifiable",
      message: "This RomM server publishes no checksums, so it cannot confirm these files",
      differences: [],
    };
    vi.mocked(verifyExistingContent).mockResolvedValue(unverifiable);
    const { container } = renderModal();

    fireEvent.click(buttonByText(container, "Check Against Server"));

    await waitFor(() => expect(container.textContent).toContain("publishes no checksums"));
    // Never dressed up as either verdict.
    expect(container.textContent).not.toContain("match the ones on the server");
    expect(container.textContent).not.toContain("differ from the ones on the server");
  });

  it("surfaces a thrown check as an error rather than a silent no-op", async () => {
    vi.mocked(verifyExistingContent).mockRejectedValue(new Error("bridge down"));
    const { container } = renderModal();

    fireEvent.click(buttonByText(container, "Check Against Server"));

    await waitFor(() => expect(container.textContent).toContain("Couldn't reach the server to check these files"));
  });

  it("shows byte progress from verify_progress frames for this ROM", async () => {
    let resolveCheck: (r: VerifyContentResult) => void = () => {};
    vi.mocked(verifyExistingContent).mockReturnValue(
      new Promise<VerifyContentResult>((resolve) => {
        resolveCheck = resolve;
      }),
    );
    const { container } = renderModal();

    fireEvent.click(buttonByText(container, "Check Against Server"));
    await waitFor(() => expect(container.textContent).toContain("Checking the files…"));

    act(() => {
      emitDeckyEvent<[VerifyProgressEvent]>("verify_progress", {
        rom_id: ROM_ID,
        bytes_done: 50,
        bytes_total: 200,
      });
    });
    expect(container.textContent).toContain("Checking the files… 25%");

    await act(async () => {
      resolveCheck({ status: "match", message: "ok", differences: [] });
    });
  });

  it("ignores verify_progress frames for another ROM", async () => {
    let resolveCheck: (r: VerifyContentResult) => void = () => {};
    vi.mocked(verifyExistingContent).mockReturnValue(
      new Promise<VerifyContentResult>((resolve) => {
        resolveCheck = resolve;
      }),
    );
    const { container } = renderModal();

    fireEvent.click(buttonByText(container, "Check Against Server"));
    await waitFor(() => expect(container.textContent).toContain("Checking the files…"));

    act(() => {
      emitDeckyEvent<[VerifyProgressEvent]>("verify_progress", { rom_id: 999, bytes_done: 50, bytes_total: 200 });
    });
    expect(container.textContent).not.toContain("25%");

    await act(async () => {
      resolveCheck({ status: "match", message: "ok", differences: [] });
    });
  });

  it("unsubscribes from verify_progress on unmount", () => {
    const { unmount } = renderModal();
    expect(deckyEventListenerCount("verify_progress")).toBe(1);
    unmount();
    expect(deckyEventListenerCount("verify_progress")).toBe(0);
  });
});

describe("AdoptExistingModal — a candidate found under another name", () => {
  const CANDIDATE_PATH = "/roms/snes/Game (U).sfc";

  function renderCandidate() {
    return renderModal({
      occupied: occupied({
        existing: { ...occupied().existing, name: "Game (U).sfc", path: CANDIDATE_PATH },
        incoming: { name: "Game (USA).sfc", size_bytes: 2048 },
        sizes_match: true,
      }),
      candidatePath: CANDIDATE_PATH,
    });
  }

  it("does not claim the file is where the download would land", () => {
    const { container } = renderCandidate();
    expect(container.textContent).not.toContain("already where this game would be downloaded");
    expect(container.textContent).toContain("carries this game's name");
  });

  it("states the rename and that saves travel with it", () => {
    const { container } = renderCandidate();
    expect(container.textContent).toContain("renames it to Game (USA).sfc");
    expect(container.textContent).toContain("saves and savestates");
  });

  it("checks the candidate's own path, not the game's empty location", async () => {
    vi.mocked(verifyExistingContent).mockResolvedValue({ status: "match", message: "ok", differences: [] });
    const { container } = renderCandidate();

    fireEvent.click(buttonByText(container, "Check Against Server"));

    await waitFor(() => expect(container.textContent).toContain("ok"));
    expect(vi.mocked(verifyExistingContent)).toHaveBeenCalledWith(ROM_ID, CANDIDATE_PATH);
  });

  it("says nothing about a rename for content at the game's own location", () => {
    const { container } = renderModal();
    expect(container.textContent).not.toContain("renames it to");
  });

  it("names the candidate — not the server's file — in the deletion confirmation", () => {
    // The sentence promises this exact file is deleted, and the backend now
    // keeps that promise. It has to name the file the user is looking at.
    const { container } = renderCandidate();

    fireEvent.click(buttonByText(container, "Download Instead"));

    expect(container.textContent).toContain("Downloading deletes the file that is here now");
    expect(container.textContent).toContain("Game (U).sfc");
    expect(container.textContent).toContain("If it is your own dump, patch or romhack, it is gone");
  });

  it("Delete and Download resolves replace for a candidate too", () => {
    const { container, onChoice, closeModal } = renderCandidate();

    fireEvent.click(buttonByText(container, "Download Instead"));
    fireEvent.click(buttonByText(container, "Delete and Download"));

    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith("replace");
  });

  it("never prints 0 B for a folder the search deliberately did not measure", () => {
    const { container } = renderModal({
      occupied: occupied({
        existing: { ...occupied().existing, name: "Game (U)", path: "/roms/psx/Game (U)", kind: "dir", size_bytes: 0 },
        sizes_match: null,
      }),
      candidatePath: "/roms/psx/Game (U)",
    });

    expect(container.textContent).not.toContain("0 B");
    expect(container.textContent).toContain("not measured");
  });

  it("does not blame the server for a comparison we chose not to make", () => {
    const { container } = renderModal({
      occupied: occupied({
        existing: { ...occupied().existing, name: "Game (U)", path: "/roms/psx/Game (U)", kind: "dir", size_bytes: 0 },
        incoming: { name: "Game (USA)", size_bytes: 4096 },
        sizes_match: null,
      }),
      candidatePath: "/roms/psx/Game (U)",
    });

    // The server did state a size — it is rendered on the other side of the
    // comparison — so saying it did not would be false.
    expect(container.textContent).toContain("4.0 KB");
    expect(container.textContent).not.toContain("The server did not state a size");
    expect(container.textContent).toContain("Folders are not measured");
  });

  it("the deletion confirmation does not claim a folder is 0 B either", () => {
    const { container } = renderModal({
      occupied: occupied({
        existing: { ...occupied().existing, name: "Game (U)", path: "/roms/psx/Game (U)", kind: "dir", size_bytes: 0 },
        sizes_match: null,
      }),
      candidatePath: "/roms/psx/Game (U)",
    });

    fireEvent.click(buttonByText(container, "Download Instead"));

    expect(container.textContent).toContain("Downloading deletes the folder that is here now");
    expect(container.textContent).not.toContain("0 B");
  });

  it("still blames the server when the server really did state no size", () => {
    const { container } = renderModal({
      occupied: occupied({ sizes_match: null, incoming: { name: "Game (USA).sfc", size_bytes: 0 } }),
      candidatePath: CANDIDATE_PATH,
    });

    expect(container.textContent).toContain("The server did not state a size");
  });

  it("Go Back leaves the candidate's confirmation without choosing", () => {
    const { container, onChoice } = renderCandidate();

    fireEvent.click(buttonByText(container, "Download Instead"));
    fireEvent.click(buttonByText(container, "Go Back"));

    expect(onChoice).not.toHaveBeenCalled();
    expect(buttonByText(container, "Use These Files")).toBeTruthy();
  });
});

describe("comparisonForCandidate", () => {
  const base = {
    name: "Game (U).sfc",
    path: "/roms/snes/Game (U).sfc",
    is_dir: false,
    size_bytes: 2048,
    modified_at: 1_700_000_000,
    evidence: "size" as const,
    detail: "Exactly the size the server would send",
  };

  it("carries the candidate's own numbers into the comparison", () => {
    const comparison = comparisonForCandidate(base, { name: "Game (USA).sfc", size_bytes: 2048 });
    expect(comparison.existing).toEqual({
      name: "Game (U).sfc",
      path: "/roms/snes/Game (U).sfc",
      kind: "file",
      size_bytes: 2048,
      modified_at: 1_700_000_000,
    });
    expect(comparison.sizes_match).toBe(true);
  });

  it("reports a size difference rather than hiding it", () => {
    const comparison = comparisonForCandidate(base, { name: "Game (USA).sfc", size_bytes: 4096 });
    expect(comparison.sizes_match).toBe(false);
  });

  it("cannot compare a folder, because the search never sized one", () => {
    const comparison = comparisonForCandidate(
      { ...base, is_dir: true, size_bytes: 0 },
      { name: "Game (USA)", size_bytes: 4096 },
    );
    expect(comparison.sizes_match).toBeNull();
  });

  it("cannot compare when the server stated no size", () => {
    const comparison = comparisonForCandidate(base, { name: "Game (USA).sfc", size_bytes: 0 });
    expect(comparison.sizes_match).toBeNull();
  });

  it("always offers the candidate, because the search only ever returns a usable shape", () => {
    expect(comparisonForCandidate(base, { name: "Game (USA).sfc", size_bytes: 2048 }).adoptable).toBe(true);
  });
});
