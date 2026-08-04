import { act, fireEvent, render, waitFor, type RenderResult } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { toaster } from "@decky/api";
import { showModal } from "@decky/ui";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import * as backend from "../api/backend";
import {
  beginPrunePreview,
  beginPruneRun,
  getPruneState,
  resetPruneState,
  setPruneComplete,
  setPruneProgress,
} from "../utils/pruneStore";
import {
  openRemovedGamesCleanupModal,
  RemovedGamesCleanupSection,
  stageInstalledSelections,
  STAGE_LABELS,
} from "./RemovedGamesCleanup";

const preview: backend.PrunePreviewResult = {
  success: true,
  preview_id: "preview-1",
  scope: "bulk",
  items: [
    {
      rom_id: 7,
      name: "Removed Game",
      name_truncated: false,
      fs_name: "Removed Game.gba",
      fs_name_truncated: false,
      platform_slug: "gba",
      group_id: "group-1",
      group_id_truncated: false,
      group_size: 1,
      bound_count: 0,
      candidate: true,
      installed: true,
      installed_bytes: 200,
      warning: null,
      warning_truncated: false,
    },
  ],
  offset: 0,
  limit: 50,
  total: 1,
  free_bytes: 100,
  recovery_root: "/home/deck/decky-romm-sync-recovery",
};

function shownModal(): ReactElement {
  const calls = vi.mocked(showModal).mock.calls;
  const element = calls[calls.length - 1]?.[0] as ReactElement | undefined;
  if (!element) throw new Error("Expected cleanup modal");
  return element;
}

/**
 * The "include installed ROM content" checkbox belonging to `gameName`'s row.
 *
 * The dialog's option toggles and its per-row content toggles share one flat
 * `toggle-input` list, so indexing it ties every row assertion to the number of
 * options rendered above the list. Throws rather than returning nothing: a row
 * that has lost its content toggle is the failure, not a silently skipped click.
 */
function contentToggleFor(modal: RenderResult, gameName: string): HTMLInputElement {
  const row = modal.getByText(gameName).parentElement;
  const toggle = [...(row?.querySelectorAll<HTMLElement>('[data-testid="toggle"]') ?? [])].find((el) =>
    el.textContent.includes("Include installed ROM content"),
  );
  const input = toggle?.querySelector<HTMLInputElement>('[data-testid="toggle-input"]');
  if (!input) throw new Error(`No installed-content toggle in the row for ${gameName}`);
  return input;
}

describe("RemovedGamesCleanup", () => {
  // logInfo/logWarn/logError are plain wrappers over the frontend_log callable,
  // so they are spied rather than module-mocked. Fresh per test: the confirm
  // path asserts on them in both directions, and a call leaking in from a
  // sibling test would satisfy either.
  let logs: { info: MockInstance; warn: MockInstance; error: MockInstance };

  beforeEach(() => {
    vi.mocked(backend.getPrunePreview).mockReset();
    vi.mocked(backend.stagePruneInstalledSelection).mockReset();
    vi.mocked(backend.startPrune).mockReset();
    vi.mocked(backend.cancelPrune).mockReset();
    vi.mocked(showModal).mockReset();
    vi.mocked(toaster.toast).mockReset();
    logs = {
      info: vi.spyOn(backend, "logInfo").mockImplementation(() => {}),
      warn: vi.spyOn(backend, "logWarn").mockImplementation(() => {}),
      error: vi.spyOn(backend, "logError").mockImplementation(() => {}),
    };
    resetPruneState();
    vi.mocked(backend.getPrunePreview).mockResolvedValue(preview);
    vi.mocked(backend.stagePruneInstalledSelection).mockResolvedValue({
      success: true,
      selection_id: "selection-1",
      selected_count: 1,
      finalized: true,
    });
    vi.mocked(backend.startPrune).mockResolvedValue({ success: true, run_id: "run-1", status: "running" });
  });

  afterEach(() => vi.restoreAllMocks());

  it("performs the first local scan before showing the confirmation modal", async () => {
    await expect(openRemovedGamesCleanupModal()).resolves.toBe(true);

    expect(vi.mocked(backend.getPrunePreview)).toHaveBeenCalledWith({
      scope: "bulk",
      rom_id: null,
      preview_id: null,
      offset: 0,
      limit: 50,
    });
    expect(showModal).toHaveBeenCalledTimes(1);
  });

  it("returns false and shows no modal when the scan is empty", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({ ...preview, items: [], total: 0 });

    await expect(openRemovedGamesCleanupModal(7)).resolves.toBe(false);

    expect(vi.mocked(backend.getPrunePreview)).toHaveBeenCalledWith({
      scope: "rom",
      rom_id: 7,
      preview_id: null,
      offset: 0,
      limit: 50,
    });
    expect(showModal).not.toHaveBeenCalled();
  });

  it("uses the shipped option defaults and blocks confirmation when selected content exceeds free space", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const toggles = modal.getAllByTestId("toggle-input") as HTMLInputElement[];

    // repoint, remove rows, remove fully vanished, recovery bundle, per-ROM
    // content. Whole-game removal is on: it is what the dialog exists for, and
    // the default-on recovery bundle is what makes it recoverable. Per-ROM
    // content stays off — it is the only one that can exhaust the disk.
    expect(toggles.map((toggle) => toggle.checked)).toEqual([true, true, true, true, false]);
    const confirm = modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
    expect(modal.container.textContent).toContain("Without a backup, the downloaded ROM file is deleted");

    fireEvent.click(toggles[4]!);

    expect(modal.container.textContent).toContain("Not enough free space.");
    fireEvent.click(confirm);
    await act(async () => Promise.resolve());
    expect(backend.startPrune).not.toHaveBeenCalled();
    expect(modal.container.textContent).toContain("doesn't fit in the free space");
  });

  it("submits all temporary options and re-enables confirmation after a failed start", async () => {
    vi.mocked(backend.startPrune).mockRejectedValueOnce(new Error("bridge offline"));
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const confirm = modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement;

    fireEvent.click(confirm);
    await waitFor(() => expect(modal.container.textContent).toContain("bridge offline"));

    expect(confirm.disabled).toBe(false);
    expect(vi.mocked(backend.startPrune)).toHaveBeenCalledWith({
      preview_id: "preview-1",
      confirmed: true,
      repoint_shortcuts: true,
      remove_rows: true,
      remove_fully_vanished: true,
      create_recovery_bundle: true,
      installed_selection_id: null,
    });
  });

  it("stays open and renders live progress through completion", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const confirm = modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement;

    fireEvent.click(confirm);
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));
    expect(confirm.disabled).toBe(true);

    act(() => {
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 1,
        total: 2,
        stage: "creating_recovery",
        rom_ids: [7],
        name: "Removed Game",
      });
    });
    expect(modal.container.textContent).toContain("Backing up — Removed Game");

    act(() => {
      setPruneComplete({
        success: true,
        partial: false,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [7],
        affected_app_ids: [],
        results: [{ group_id: "group-1", rom_ids: [7], status: "removed", message: "Removed." }],
      });
    });
    expect(modal.container.textContent).toContain("1 removed; 0 skipped, partial, or failed.");
    expect(modal.getByRole("button", { name: "Close" })).toBeTruthy();
  });

  it("adopts a matching run frame when the successful start response is lost", async () => {
    vi.useFakeTimers();
    vi.mocked(backend.startPrune).mockImplementation(() => new Promise(() => {}));
    try {
      await openRemovedGamesCleanupModal();
      const modal = render(shownModal());
      fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
      await act(async () => Promise.resolve());

      act(() => {
        setPruneProgress({
          run_id: "adopted-run",
          preview_id: "preview-1",
          current: 1,
          total: 2,
          stage: "checking",
          rom_ids: [7],
          name: "Removed Game",
        });
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });

      expect(modal.container.textContent).toContain("Cleanup running...");
      expect(modal.container.textContent).toContain("Checking with RomM — Removed Game");
      expect(modal.container.textContent).not.toContain("Cleanup could not start");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows an enabled Close control immediately when completion wins the start-response race", async () => {
    vi.mocked(backend.startPrune).mockImplementation(() => new Promise(() => {}));
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await act(async () => Promise.resolve());

    act(() => {
      setPruneComplete({
        success: true,
        partial: false,
        run_id: "event-first-run",
        preview_id: "preview-1",
        removed_rom_ids: [7],
        affected_app_ids: [],
        results: [],
      });
    });

    const close = modal.getByRole("button", { name: "Close" }) as HTMLButtonElement;
    expect(close.disabled).toBe(false);
    expect(modal.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(modal.container.textContent).toContain("1 removed");
  });

  it("surfaces scan rejection and re-enables the Danger Zone action", async () => {
    vi.mocked(backend.getPrunePreview).mockRejectedValue(new Error("offline"));
    const section = render(createElement(RemovedGamesCleanupSection));
    const button = section.getByRole("button", { name: "Clean Up Removed RomM Games" }) as HTMLButtonElement;

    fireEvent.click(button);
    await waitFor(() =>
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Could not scan removed RomM games.",
      }),
    );

    expect(button.disabled).toBe(false);
  });

  it("requires every preview page to be disclosed before confirmation", async () => {
    vi.mocked(backend.getPrunePreview)
      .mockResolvedValueOnce({ ...preview, total: 2 })
      .mockResolvedValueOnce({
        ...preview,
        offset: 1,
        total: 2,
        items: [{ ...preview.items![0]!, rom_id: 8, candidate: false, name: "Current sibling" }],
      });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const confirm = modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement;
    expect(modal.container.textContent).toContain("Load every page before confirming");

    // Pressing early must refuse and SAY so — the run may not start against a
    // half-disclosed list, and a press that does nothing is the reported defect.
    fireEvent.click(confirm);
    await act(async () => Promise.resolve());
    expect(backend.startPrune).not.toHaveBeenCalled();
    expect(modal.container.textContent).toContain("Cleanup did not start: Load all 2 entries before confirming.");

    fireEvent.click(modal.getByRole("button", { name: "Load more (1 of 2)" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Current sibling"));
    fireEvent.click(confirm);
    await waitFor(() => expect(backend.startPrune).toHaveBeenCalled());
  });

  it("clears and disables installed-content selections when recovery is off", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    let toggles = modal.getAllByTestId("toggle-input") as HTMLInputElement[];
    fireEvent.click(toggles[4]!);
    expect(toggles[4]!.checked).toBe(true);

    fireEvent.click(toggles[3]!);
    await waitFor(() => {
      toggles = modal.getAllByTestId("toggle-input") as HTMLInputElement[];
      const content = toggles[toggles.length - 1]!;
      expect(content.checked).toBe(false);
      expect(modal.container.textContent).toContain("Selected ROM-content recovery estimate: 0 B");
    });
    expect(modal.container.textContent).toContain("Without a backup, the downloaded ROM file is deleted");
  });

  it("allows a repoint-only run and resets an old completion when opening", async () => {
    setPruneComplete({
      success: true,
      partial: false,
      run_id: "old",
      preview_id: "preview-old",
      removed_rom_ids: [99],
      affected_app_ids: [],
      results: [],
    });
    await openRemovedGamesCleanupModal(7);
    const modal = render(shownModal());
    const toggles = modal.getAllByTestId("toggle-input") as HTMLInputElement[];
    fireEvent.click(toggles[1]!);
    const confirm = modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);

    fireEvent.click(confirm);
    await waitFor(() => expect(backend.startPrune).toHaveBeenCalled());
    expect(vi.mocked(backend.startPrune).mock.calls[0]?.[0].remove_rows).toBe(false);
    expect(modal.container.textContent).toContain("1 locally kept version of this game is no longer on your RomM");
  });

  it("refreshes the free-space snapshot without replacing the preview", async () => {
    vi.mocked(backend.getPrunePreview)
      .mockResolvedValueOnce(preview)
      .mockResolvedValueOnce({ ...preview, items: [], limit: 0, free_bytes: 500 });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    fireEvent.click(modal.getByRole("button", { name: "Refresh free space" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Free at target: 500 B"));
    expect(vi.mocked(backend.getPrunePreview).mock.calls[1]?.[0]).toMatchObject({
      preview_id: "preview-1",
      limit: 0,
    });
  });

  it("stages the checked installed content and carries its selection id into the run", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      total: 2,
      free_bytes: 1,
      items: [
        { ...preview.items![0]!, rom_id: 11, installed_bytes: 0, name: "Game 11" },
        { ...preview.items![0]!, rom_id: 12, installed_bytes: 0, name: "Game 12" },
      ],
    });
    vi.mocked(backend.stagePruneInstalledSelection).mockResolvedValue({
      success: true,
      selection_id: "selection-final",
      selected_count: 2,
      finalized: true,
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(contentToggleFor(modal, "Game 11"));
    fireEvent.click(contentToggleFor(modal, "Game 12"));

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(backend.startPrune).toHaveBeenCalled());

    expect(vi.mocked(backend.stagePruneInstalledSelection)).toHaveBeenCalledExactlyOnceWith({
      preview_id: "preview-1",
      selection_id: null,
      rom_ids: [11, 12],
      final: true,
    });
    expect(vi.mocked(backend.startPrune).mock.calls[0]?.[0].installed_selection_id).toBe("selection-final");
  });

  it("does not start the run when the staged selection is refused", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      free_bytes: 1,
      items: [{ ...preview.items![0]!, installed_bytes: 0 }],
    });
    vi.mocked(backend.stagePruneInstalledSelection).mockResolvedValue({
      success: false,
      reason: "stale_preview",
      message: "That cleanup preview is no longer current.",
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(contentToggleFor(modal, "Removed Game"));

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => {
      expect(modal.container.textContent).toContain("That cleanup preview is no longer current.");
      // Back at its resting label, so the whole Confirm path has run and the
      // control is usable for a retry.
      expect((modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement).disabled).toBe(false);
    });

    // The run would otherwise execute against a selection the backend never accepted.
    expect(backend.startPrune).not.toHaveBeenCalled();
  });

  describe("stageInstalledSelections", () => {
    // Enough to cross the page boundary twice and leave a short final page. The
    // dialog would need one rendered row and one click per id to reach it.
    const romIds = Array.from({ length: 257 }, (_, index) => index + 1);

    it("splits a selection into bounded pages, each chained onto the one before", async () => {
      let page = 0;
      vi.mocked(backend.stagePruneInstalledSelection).mockImplementation(async (request) => ({
        success: true,
        selection_id: `selection-${++page}`,
        selected_count: request.rom_ids.length,
        finalized: request.final,
      }));

      await expect(stageInstalledSelections("preview-1", romIds, vi.fn())).resolves.toEqual({
        ok: true,
        selectionId: "selection-3",
      });

      const pages = vi.mocked(backend.stagePruneInstalledSelection).mock.calls.map(([request]) => request);
      expect(pages.map((request) => request.rom_ids.length)).toEqual([100, 100, 57]);
      expect(pages.map((request) => request.selection_id)).toEqual([null, "selection-1", "selection-2"]);
      // Only the last page finalizes — an early close would drop the rest.
      expect(pages.map((request) => request.final)).toEqual([false, false, true]);
      expect(pages.flatMap((request) => request.rom_ids)).toEqual(romIds);
    });

    it("stops at a refused page and reports the backend's message", async () => {
      const setStatus = vi.fn();
      vi.mocked(backend.stagePruneInstalledSelection)
        .mockResolvedValueOnce({ success: true, selection_id: "selection-1", selected_count: 100, finalized: false })
        .mockResolvedValueOnce({
          success: false,
          reason: "stale_preview",
          message: "That cleanup preview is no longer current.",
        });

      await expect(stageInstalledSelections("preview-1", romIds, setStatus)).resolves.toEqual({ ok: false });

      // The third page is never sent: a partially staged selection must not be
      // extended past the point the backend stopped accepting it.
      expect(backend.stagePruneInstalledSelection).toHaveBeenCalledTimes(2);
      expect(setStatus).toHaveBeenCalledExactlyOnceWith("That cleanup preview is no longer current.");
      expect(logs.warn).toHaveBeenCalledWith(
        expect.stringContaining("Confirm aborted while staging installed content"),
      );
    });

    it("treats a page that reports success without a selection id as refused", async () => {
      const setStatus = vi.fn();
      vi.mocked(backend.stagePruneInstalledSelection).mockResolvedValue({
        success: true,
        selected_count: 100,
        message: "The selection was accepted but not stored.",
      });

      await expect(stageInstalledSelections("preview-1", romIds, setStatus)).resolves.toEqual({ ok: false });

      // There is nothing to chain the next page onto. Carrying on would open a
      // second, unrelated selection and stage the remaining ids into it, and the
      // run would then be started against whichever one Confirm happened to hold.
      expect(backend.stagePruneInstalledSelection).toHaveBeenCalledTimes(1);
      expect(setStatus).toHaveBeenCalledExactlyOnceWith("The selection was accepted but not stored.");
    });

    it("says something of its own when a refusal carries no message", async () => {
      const setStatus = vi.fn();
      vi.mocked(backend.stagePruneInstalledSelection).mockResolvedValue({ success: false, reason: "stale_preview" });

      await expect(stageInstalledSelections("preview-1", romIds, setStatus)).resolves.toEqual({ ok: false });

      // Confirm reports through this string alone, so an unexplained refusal
      // must not surface as a blank line in the dialog.
      expect(setStatus).toHaveBeenCalledExactlyOnceWith("Installed-content selections could not be staged.");
      expect(logs.warn).toHaveBeenCalledWith(expect.stringContaining("installed content: no message"));
    });
  });

  it("surfaces bounded save warnings in terminal details", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneComplete({
        success: false,
        partial: true,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7],
            status: "partial",
            message: "Local cleanup was incomplete.",
            message_truncated: true,
            warnings: ["Shared save was retained."],
            warning_count: 7,
            warnings_omitted: true,
            warnings_truncated: true,
          },
        ],
      });
    });

    expect(modal.container.textContent).toContain("Warning: Shared save was retained.");
    expect(modal.container.textContent).toContain("6 additional warning(s) omitted.");
    expect(modal.container.textContent).toContain("One or more displayed warnings were shortened.");
    expect(modal.container.textContent).toContain("Detail was shortened");
    const details = modal.getByRole("region", { name: "Cleanup details" });
    expect(details.getAttribute("tabindex")).toBe("0");
    expect(modal.getByRole("status").textContent).not.toContain("Shared save was retained");
  });

  it("does not call omitted short warnings shortened", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneComplete({
        success: false,
        partial: true,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7],
            status: "partial",
            message: "Local cleanup was incomplete.",
            warnings: ["One", "Two", "Three", "Four", "Five"],
            warning_count: 6,
            warnings_omitted: true,
            warnings_truncated: false,
          },
        ],
      });
    });

    expect(modal.container.textContent).toContain("1 additional warning(s) omitted.");
    expect(modal.container.textContent).not.toContain("displayed warnings were shortened");
  });

  it("surfaces warnings from successful removals and a run-level terminal error", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneComplete({
        success: false,
        partial: true,
        run_id: "run-1",
        preview_id: "preview-1",
        reason: "unknown",
        message: "The run stopped after committed work.",
        removed_rom_ids: [7],
        affected_app_ids: [],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7],
            status: "removed",
            message: "Removed.",
            warnings: ["A shared save was retained."],
            warning_count: 1,
            warnings_truncated: true,
          },
        ],
      });
    });

    expect(modal.container.textContent).toContain("unknown: The run stopped after committed work.");
    expect(modal.container.textContent).toContain("Warning: A shared save was retained.");
    expect(modal.container.textContent).toContain("One or more displayed warnings were shortened.");
    expect(modal.container.textContent).not.toContain("0 additional warning(s)");
  });

  it("blocks confirmation when a selected installed ROM has no measurable size", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      free_bytes: 1_000_000,
      items: [{ ...preview.items![0]!, installed_bytes: null }],
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const confirm = modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement;
    // Unselected, an unmeasurable ROM blocks nothing — it is simply not copied.
    expect(confirm.disabled).toBe(false);
    expect(modal.container.textContent).not.toContain("no safe measurable size");

    const toggles = modal.getAllByTestId("toggle-input") as HTMLInputElement[];
    fireEvent.click(toggles[4]!);

    // Selected for recovery, its required space cannot be proven, so the run is
    // refused rather than started on an unbacked estimate.
    fireEvent.click(confirm);
    await act(async () => Promise.resolve());
    expect(backend.startPrune).not.toHaveBeenCalled();
    expect(modal.container.textContent).toContain("A selected installed ROM has no safe measurable size.");
    expect(modal.container.textContent).not.toContain("Not enough free space.");
  });

  it("counts only removable rows in the headline and labels a disclosed sibling as kept", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      total: 2,
      candidate_total: 1,
      items: [
        preview.items![0]!,
        { ...preview.items![0]!, rom_id: 8, candidate: false, installed: false, name: "Live sibling", group_size: 2 },
      ],
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const text = modal.container.textContent;

    // The headline must not inflate itself with the sibling RomM still serves.
    expect(text).toContain("1 locally kept version is no longer on your RomM server");
    expect(text).not.toContain("2 locally kept");
    // The sibling is still disclosed — a fresh probe, not the local fetch
    // generation, decides whole-game removal — but never called a candidate.
    expect(text).toContain("Live sibling");
    expect(text).toContain("Other versions of these games — kept");
    expect(text).toContain("Still on RomM at your last sync");
    expect(text).toContain("Other versions of the same games are listed below");
    expect(text).not.toContain("disclosed for whole-game removal");
  });

  it("hides the disclosure rows when whole-game removal is switched off", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      total: 2,
      candidate_total: 1,
      items: [
        preview.items![0]!,
        { ...preview.items![0]!, rom_id: 8, candidate: false, installed: false, name: "Live sibling", group_size: 2 },
      ],
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    expect(modal.container.textContent).toContain("Live sibling");

    fireEvent.click((modal.getAllByTestId("toggle-input") as HTMLInputElement[])[2]!);

    // With whole-game removal off, `selected_prune_ids` can never return a
    // non-candidate, so disclosing one describes a thing that cannot happen.
    expect(modal.container.textContent).not.toContain("Live sibling");
    expect(modal.container.textContent).not.toContain("Other versions of these games — kept");
    expect(modal.container.textContent).not.toContain("Other versions of the same games are listed below");
    // The headline counted candidates only, so it does not move.
    expect(modal.container.textContent).toContain("1 locally kept version is no longer on your RomM server");
  });

  it("unstages a hidden row's content when whole-game removal is switched off", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      total: 2,
      candidate_total: 1,
      free_bytes: 10_000,
      items: [
        { ...preview.items![0]!, installed: false, installed_bytes: null },
        { ...preview.items![0]!, rom_id: 8, candidate: false, name: "Live sibling", installed_bytes: 512 },
      ],
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    const contentToggle = (modal.getAllByTestId("toggle-input") as HTMLInputElement[])[4]!;
    fireEvent.click(contentToggle);
    expect(modal.container.textContent).toContain("recovery estimate: 512 B");

    fireEvent.click((modal.getAllByTestId("toggle-input") as HTMLInputElement[])[2]!);
    await waitFor(() => expect(modal.container.textContent).toContain("recovery estimate: 0 B"));

    // A selection the user can no longer see must not be staged behind their back.
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(backend.startPrune).toHaveBeenCalled());
    expect(backend.stagePruneInstalledSelection).not.toHaveBeenCalled();
    expect(vi.mocked(backend.startPrune).mock.calls[0]?.[0].installed_selection_id).toBeNull();
  });

  it("states what whole-game removal takes and what the recovery bundle keeps", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const descriptions = [...modal.container.querySelectorAll('[data-testid="toggle"]')]
      .map((el) => el.textContent)
      .join(" ");

    // The option ships on, so its description has to carry the full weight.
    expect(modal.container.textContent).toContain("Only for games where the server confirms every single version is");
    expect(modal.container.textContent).toContain("rebuild it by hand");
    expect(descriptions).not.toContain("Off by default");
  });

  it("drops the disclosure sentence and heading when every listed row is removable", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const text = modal.container.textContent;

    expect(text).toContain("1 locally kept version is no longer on your RomM server");
    expect(text).not.toContain("Other versions of the same games are listed below");
    expect(text).not.toContain("Other versions of these games — kept");
  });

  it("says so when no listed version has ROM files on this device", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      items: [{ ...preview.items![0]!, installed: false, installed_bytes: null }],
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    // Otherwise the per-row content option is simply absent and reads as missing.
    expect(modal.container.textContent).toContain("None of these versions has ROM files downloaded on this device");
  });

  it("claims nothing about ROM files while a page is still undisclosed", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValue({
      ...preview,
      total: 2,
      items: [{ ...preview.items![0]!, installed: false, installed_bytes: null }],
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    // An unloaded page could still hold an installed row.
    expect(modal.container.textContent).not.toContain("None of these versions has ROM files");
  });

  it("shows the installed-content option instead of the empty state when a row has files", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    expect(modal.container.textContent).toContain("Include installed ROM content (200 B)");
    expect(modal.container.textContent).not.toContain("None of these versions has ROM files");
  });

  it("keeps the confirm dialog to one scroll region so the estimate cannot cover the last row", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    const scrollers = [...modal.container.querySelectorAll<HTMLElement>("div")].filter(
      (el) => el.style.overflowY === "auto",
    );
    expect(scrollers).toHaveLength(1);
    // The space estimate scrolls WITH the list rather than sitting pinned over it.
    expect(scrollers[0]!.textContent).toContain("Removed Game");
    expect(scrollers[0]!.textContent).toContain("Selected ROM-content recovery estimate");
  });

  it("brings the intro back into view when the first option takes focus", async () => {
    vi.useFakeTimers();
    try {
      await openRemovedGamesCleanupModal();
      const modal = render(shownModal());
      const body = [...modal.container.querySelectorAll<HTMLElement>("div")].find(
        (el) => el.style.overflowY === "auto",
      )!;
      Object.defineProperty(body, "scrollHeight", { value: 2000, configurable: true });
      Object.defineProperty(body, "clientHeight", { value: 600, configurable: true });
      const scrollTo = vi.fn();
      body.scrollTo = scrollTo as unknown as typeof body.scrollTo;

      fireEvent.focusIn((modal.getAllByTestId("toggle-input") as HTMLInputElement[])[0]!);
      expect(scrollTo).not.toHaveBeenCalled();
      act(() => {
        vi.runAllTimers();
      });

      // Steam's focus engine stops at the control itself, stranding the intro
      // above it off-screen on a controller.
      expect(scrollTo).toHaveBeenCalledExactlyOnceWith({ top: 0, behavior: "smooth" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("says why Confirm is unavailable instead of leaving a dead control", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValueOnce({ ...preview, total: 2 });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    // The "load every page" warning sits far above the button in a scrolling
    // dialog, so the reason has to be readable from where the button is.
    expect((modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement).disabled).toBe(false);
    expect(modal.container.textContent).toContain("Load all 2 entries before confirming.");
  });

  it("reports a locally refused Confirm in the dialog and the log", async () => {
    vi.mocked(backend.getPrunePreview).mockResolvedValueOnce({ ...preview, total: 2 });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await act(async () => Promise.resolve());

    expect(logs.info).toHaveBeenCalledWith(expect.stringContaining("[prune] Confirm pressed"));
    expect(logs.warn).toHaveBeenCalledWith(expect.stringContaining("[prune] Confirm refused locally"));
    expect(backend.startPrune).not.toHaveBeenCalled();
  });

  it("logs the confirm press and the accepted run id", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    expect(logs.info).toHaveBeenCalledWith(expect.stringContaining("[prune] Confirm pressed"));
    expect(logs.info).toHaveBeenCalledWith("[prune] startPrune accepted: run=run-1");
  });

  it("logs a backend refusal alongside the message it shows", async () => {
    vi.mocked(backend.startPrune).mockResolvedValue({
      success: false,
      reason: "prune_active",
      message: "A removed-game cleanup is already running.",
    });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("already running"));

    expect(logs.warn).toHaveBeenCalledWith(expect.stringContaining("reason=prune_active"));
  });

  it("does not claim a run is progressing when the store refused to adopt it", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    // Another preview claimed the pending slot between opening and confirming.
    act(() => beginPrunePreview("preview-2"));

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("lost track of it"));

    // The run is executing; its frames can never be admitted, so "running..."
    // would promise a progress line that cannot arrive.
    expect(modal.container.textContent).not.toContain("Cleanup running...");
    expect(getPruneState().runId).toBeNull();
    expect(logs.error).toHaveBeenCalledWith(expect.stringContaining("no longer pending"));
  });

  it("names the shortcut removal as conditional on the fully-vanished toggle", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    // Rows in a fully vanished group may have no shortcut at all, so the label
    // must not promise one is removed.
    expect(modal.container.textContent).toContain("Remove fully vanished games, including any Steam shortcut");
  });

  it("surfaces a success response that carries no run id instead of wedging admission", async () => {
    vi.mocked(backend.startPrune).mockResolvedValue({ success: true, status: "running" });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("carried no run id"));

    // No run was adopted by id, and the control stays usable for a retry.
    expect(getPruneState().runId).toBeNull();
    expect((modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("rejects a scan response that carries no preview id", async () => {
    const malformed: backend.PrunePreviewResult = { ...preview };
    delete malformed.preview_id;
    vi.mocked(backend.getPrunePreview).mockResolvedValue(malformed);

    await expect(openRemovedGamesCleanupModal()).rejects.toThrow("carried no preview id");
    expect(showModal).not.toHaveBeenCalled();
  });

  it("keeps the Danger Zone showing a run that has not emitted progress yet", async () => {
    const section = render(createElement(RemovedGamesCleanupSection));
    const button = section.getByRole("button", { name: "Clean Up Removed RomM Games" }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);

    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-1", "preview-1");
    });

    // The window between start and the first frame is exactly where a second
    // scan would collide, so the entry point has to close on the run id.
    expect(button.disabled).toBe(true);
    expect(section.container.textContent).toContain("Cleanup starting...");
    expect(section.container.textContent).toContain("A cleanup is running.");
  });

  it("offers a Stop control in the Danger Zone and sets expectations for it", async () => {
    vi.mocked(backend.cancelPrune).mockResolvedValue({ success: true, message: "ok", already_cancelling: false });
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-7", "preview-1");
    });

    // The copy must not promise a rollback the backend never performs.
    expect(section.container.textContent).toContain("The one being processed now finishes");

    fireEvent.click(section.getByRole("button", { name: "Stop Cleanup" }));
    await waitFor(() => expect(backend.cancelPrune).toHaveBeenCalledWith("run-7"));
    expect(logs.info).toHaveBeenCalledWith("[prune] Cancel pressed for run run-7");
  });

  it("surfaces a refused cancellation instead of pretending the run stopped", async () => {
    vi.mocked(backend.cancelPrune).mockResolvedValue({
      success: false,
      reason: "stale_run",
      message: "That cleanup run is not running.",
    });
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-7", "preview-1");
    });

    fireEvent.click(section.getByRole("button", { name: "Stop Cleanup" }));

    await waitFor(() => expect(section.container.textContent).toContain("That cleanup run is not running."));
    expect(logs.warn).toHaveBeenCalledWith(expect.stringContaining("reason=stale_run"));
  });

  it("reports a thrown cancellation rather than leaving the button spinning", async () => {
    vi.mocked(backend.cancelPrune).mockRejectedValue(new Error("bridge offline"));
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-7", "preview-1");
    });

    fireEvent.click(section.getByRole("button", { name: "Stop Cleanup" }));

    await waitFor(() => expect(section.container.textContent).toContain("Could not request cancellation"));
    expect((section.getByRole("button", { name: "Stop Cleanup" }) as HTMLButtonElement).disabled).toBe(false);
    expect(logs.error).toHaveBeenCalledWith(expect.stringContaining("bridge offline"));
  });

  it("offers Stop inside the modal while a run is progressing", async () => {
    vi.mocked(backend.cancelPrune).mockResolvedValue({ success: true, message: "ok", already_cancelling: false });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 1,
        total: 2,
        stage: "checking",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    fireEvent.click(modal.getByRole("button", { name: "Stop Cleanup" }));
    await waitFor(() => expect(backend.cancelPrune).toHaveBeenCalledWith("run-1"));
  });

  it("renders a real progress bar with the phase in words", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 1,
        total: 4,
        stage: "creating_recovery",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    // A stage slug is not a sentence — the bar and the phase have to be readable.
    expect(modal.container.textContent).toContain("Backing up — Removed Game");
    expect(modal.container.textContent).toContain("1 / 4");
    // The first group is in flight, so nothing is behind the run yet.
    expect(modal.container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("0");
    expect(modal.container.querySelector('[data-testid="progress-indeterminate"]')?.textContent).toBe("false");
  });

  // The bar answers "how much of this run is behind me", so the group being
  // worked on is not counted until a later frame moves past it. Filling by the
  // RUNNING group put a one-group run at 100% from its first frame and parked a
  // multi-minute content backup under a full bar.
  it.each([
    { current: 1, total: 2, fill: "0" },
    { current: 2, total: 2, fill: "50" },
    { current: 1, total: 1, fill: "0" },
  ])("fills the bar with the groups already finished ($current of $total)", ({ current, total, fill }) => {
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-1", "preview-1");
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current,
        total,
        stage: "creating_recovery",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    expect(section.container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe(fill);
    expect(section.container.querySelector('[data-testid="progress-indeterminate"]')?.textContent).toBe("false");
    // The caption keeps naming the group in flight — only the bar counts finished ones.
    expect(section.container.textContent).toContain(`${current} / ${total}`);
  });

  it.each([
    { current: 0, total: 2, fill: "0" },
    { current: 5, total: 2, fill: "100" },
  ])("keeps an out-of-order frame inside the bar ($current of $total)", ({ current, total, fill }) => {
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-1", "preview-1");
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current,
        total,
        stage: "checking",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    expect(section.container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe(fill);
  });

  it("fills the Danger Zone bar only when the run reaches its terminal frame", () => {
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-1", "preview-1");
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 2,
        total: 2,
        stage: "removing",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    expect(section.container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("50");

    act(() => {
      setPruneComplete({
        success: true,
        partial: false,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [7],
        affected_app_ids: [],
        results: [],
      });
    });

    // The run is over, so the bar is full — and the summary beside it says how
    // it ended. A bar that only ever disappears never reports being finished.
    expect(section.container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("100");
    expect(section.container.textContent).toContain("1 removed");
  });

  it("fills the modal bar when the run reaches its terminal frame", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneComplete({
        success: false,
        partial: true,
        run_id: "run-1",
        preview_id: "preview-1",
        reason: "cancelled",
        message: "Cleanup was cancelled.",
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [],
      });
    });

    // Full means the run has ended, not that it did everything — a cancelled
    // run gets the same last frame, with its outcome spelled out next to it.
    expect(modal.container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("100");
    expect(modal.container.textContent).toContain("cancelled: Cleanup was cancelled.");
  });

  it("keeps the bar indeterminate while the run has no group total", () => {
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-1", "preview-1");
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 0,
        total: 0,
        stage: "checking",
        rom_ids: [],
        name: "",
      });
    });

    // Nothing to divide by: an empty bar would claim a run that has not started.
    expect(section.container.querySelector('[data-testid="progress-indeterminate"]')?.textContent).toBe("true");
    expect(section.container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("undefined");
  });

  // Every stage services/prune/executor.py emits, from its emit_progress call
  // sites. A caption is only correct if it survives a real run, so each one is
  // driven through the component rather than asserted against the map alone.
  const BACKEND_STAGES = [
    "checking",
    "creating_recovery",
    "recovery_sealed",
    "repointing",
    "removing_shortcut",
    "removing",
    "removed",
  ];

  it("maps exactly the stages the backend emits — no more, no fewer", () => {
    // An entry for a stage that is never emitted is how three real captions
    // ended up rendering as raw slugs while the map looked complete.
    expect(Object.keys(STAGE_LABELS).sort()).toEqual([...BACKEND_STAGES].sort());
  });

  it.each(BACKEND_STAGES)("renders a plain-language caption for the %s stage", (stage) => {
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-1", "preview-1");
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 1,
        total: 2,
        stage,
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    expect(section.container.textContent).toContain(`${STAGE_LABELS[stage]} — Removed Game`);
    // The raw slug must never reach the caption for a stage we do know.
    expect(section.container.textContent).not.toContain(stage.replace(/_/g, " ") + " — Removed Game");
  });

  it("degrades an unknown backend stage to something readable", async () => {
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-1", "preview-1");
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 2,
        total: 3,
        stage: "some_new_stage",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    // A stage the frontend has not learned yet must still say something.
    expect(section.container.textContent).toContain("some new stage — Removed Game");
    expect(section.container.querySelector('[data-testid="progress"]')).toBeTruthy();
  });

  it("leads a result line with the game's name, not its metadata key", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneComplete({
        success: false,
        partial: false,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [
          {
            group_id: "igdb:1217:53",
            name: "Shenmue II",
            rom_ids: [4375],
            status: "skipped",
            reason: "liveness_uncertain",
            message: "RomM could not confirm 1 of this game's version(s); nothing was removed.",
          },
        ],
      });
    });

    // A metadata key in front of plain-language copy undoes the copy.
    expect(modal.container.textContent).toContain("Shenmue II: RomM could not confirm");
    expect(modal.container.textContent).not.toContain("igdb:1217:53");
  });

  it("falls back to the group key when a result carries no name", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneComplete({
        success: false,
        partial: false,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [
          {
            group_id: "igdb:1217:53",
            rom_ids: [4375],
            status: "skipped",
            message: "Nothing was removed.",
          },
        ],
      });
    });

    // Identifying the group badly beats not identifying it at all.
    expect(modal.container.textContent).toContain("igdb:1217:53: Nothing was removed.");
  });

  it("says a bundle was left behind when a group backed up but removed nothing", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));

    act(() => {
      setPruneComplete({
        success: true,
        partial: false,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7],
            status: "removed",
            message: "Cancelled after the backup sealed.",
            bundle_path: "/home/deck/decky-romm-sync-recovery/bundles/Shenmue-II_2026-07-31_07f4953b",
            removed_rom_ids: [],
          },
        ],
      });
    });

    // Otherwise the leftover folder is a mystery the user has to reverse-engineer.
    expect(modal.container.textContent).toContain("Backup created, nothing removed.");
    expect(modal.container.textContent).toContain("Shenmue-II_2026-07-31_07f4953b");
  });

  it("keeps Stop locked after the cancel callable resolves, until the run ends", async () => {
    vi.mocked(backend.cancelPrune).mockResolvedValue({ success: true, message: "ok", already_cancelling: false });
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-7", "preview-1");
    });

    fireEvent.click(section.getByRole("button", { name: "Stop Cleanup" }));
    await act(async () => Promise.resolve());

    // The callable has resolved. That means the request was received, NOT that
    // the run stopped — tying the lock to it let seven presses through in three
    // seconds on device.
    expect(backend.cancelPrune).toHaveBeenCalledTimes(1);
    const stop = section.getByRole("button", { name: "Stopping..." }) as HTMLButtonElement;
    expect(stop.disabled).toBe(true);
    expect(section.container.textContent).toContain("finishing the current safe step");

    act(() => {
      setPruneComplete({
        success: true,
        partial: false,
        run_id: "run-7",
        preview_id: "preview-1",
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [],
      });
    });

    // The terminal frame is the exit — the control is gone with the run.
    expect(section.queryByRole("button", { name: "Stopping..." })).toBeNull();
    expect(section.queryByRole("button", { name: "Stop Cleanup" })).toBeNull();
  });

  it("does not re-arm Stop for a run that never stops reporting", async () => {
    vi.mocked(backend.cancelPrune).mockResolvedValue({ success: true, message: "ok", already_cancelling: false });
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-7", "preview-1");
    });

    fireEvent.click(section.getByRole("button", { name: "Stop Cleanup" }));
    await act(async () => Promise.resolve());
    // Progress keeps arriving while the in-flight group finishes; that is not
    // an invitation to press again.
    act(() => {
      setPruneProgress({
        run_id: "run-7",
        preview_id: "preview-1",
        current: 2,
        total: 3,
        stage: "removing",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    expect((section.getByRole("button", { name: "Stopping..." }) as HTMLButtonElement).disabled).toBe(true);
    expect(backend.cancelPrune).toHaveBeenCalledTimes(1);
  });

  it("re-opens Stop when the backend says the run is not running", async () => {
    vi.mocked(backend.cancelPrune).mockResolvedValue({
      success: false,
      reason: "stale_run",
      message: "That cleanup run is not running.",
    });
    const section = render(createElement(RemovedGamesCleanupSection));
    act(() => {
      beginPrunePreview("preview-1");
      beginPruneRun("run-7", "preview-1");
    });

    fireEvent.click(section.getByRole("button", { name: "Stop Cleanup" }));
    await waitFor(() => expect(section.container.textContent).toContain("That cleanup run is not running."));

    // A refusal is the one outcome with no terminal frame behind it, so it must
    // re-open the control rather than leave it locked forever.
    expect((section.getByRole("button", { name: "Stop Cleanup" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("keeps the modal's Stop locked after its callable resolves", async () => {
    vi.mocked(backend.cancelPrune).mockResolvedValue({ success: true, message: "ok", already_cancelling: false });
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Cleanup running..."));
    act(() => {
      setPruneProgress({
        run_id: "run-1",
        preview_id: "preview-1",
        current: 1,
        total: 2,
        stage: "checking",
        rom_ids: [7],
        name: "Removed Game",
      });
    });

    fireEvent.click(modal.getByRole("button", { name: "Stop Cleanup" }));
    await act(async () => Promise.resolve());

    expect(backend.cancelPrune).toHaveBeenCalledTimes(1);
    expect((modal.getByRole("button", { name: "Stopping..." }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("finishing the current safe step");
  });

  it("recovers the entry point and warns when a run's result never arrives", async () => {
    vi.useFakeTimers();
    try {
      const section = render(createElement(RemovedGamesCleanupSection));
      const button = section.getByRole("button", { name: "Clean Up Removed RomM Games" }) as HTMLButtonElement;

      act(() => {
        beginPrunePreview("preview-1");
        beginPruneRun("run-lost", "preview-1");
        setPruneProgress({
          run_id: "run-lost",
          preview_id: "preview-1",
          current: 1,
          total: 2,
          stage: "removing_rows",
          rom_ids: [7],
          name: "Removed Game",
        });
      });
      expect(button.disabled).toBe(true);

      // The terminal chunk never lands (a dropped completion frame).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15 * 60_000);
      });

      expect(button.disabled).toBe(false);
      expect(section.container.textContent).toContain(
        "The cleanup result was lost — check your library and run the scan again.",
      );
      expect(section.container.textContent).not.toContain("removing rows");
    } finally {
      vi.useRealTimers();
    }
  });
});
