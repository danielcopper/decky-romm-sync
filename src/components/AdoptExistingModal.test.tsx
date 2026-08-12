import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, act, waitFor } from "@testing-library/react";
import { AdoptExistingModal } from "./AdoptExistingModal";
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
      is_dir: false,
      size_bytes: 2048,
      modified_at: 1_700_000_000,
    },
    incoming: { name: "Game.sfc", size_bytes: 1024 },
    sizes_match: false,
    adoptable: true,
    ...overrides,
  };
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
      occupied: occupied({ existing: { ...occupied().existing, is_dir: true } }),
    });
    expect(container.textContent).toContain("A folder is already where this game would be downloaded");
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
      occupied: occupied({ adoptable: false, existing: { ...occupied().existing, is_dir: true } }),
    });
    expect(buttonByText(container, "Can't use this folder for this game").disabled).toBe(true);
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
    expect(vi.mocked(verifyExistingContent)).toHaveBeenCalledWith(ROM_ID);
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
