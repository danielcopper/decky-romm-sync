import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { toaster } from "@decky/api";
import { showModal } from "@decky/ui";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as backend from "../api/backend";
import {
  beginPrunePreview,
  beginPruneRun,
  getPruneState,
  resetPruneState,
  setPruneComplete,
  setPruneProgress,
} from "../utils/pruneStore";
import { openRemovedGamesCleanupModal, RemovedGamesCleanupSection } from "./RemovedGamesCleanup";

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

describe("RemovedGamesCleanup", () => {
  beforeEach(() => {
    vi.mocked(backend.getPrunePreview).mockReset();
    vi.mocked(backend.stagePruneInstalledSelection).mockReset();
    vi.mocked(backend.startPrune).mockReset();
    vi.mocked(showModal).mockReset();
    vi.mocked(toaster.toast).mockReset();
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

  it("uses safe option defaults and blocks confirmation when selected content exceeds free space", async () => {
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const toggles = modal.getAllByTestId("toggle-input") as HTMLInputElement[];

    expect(toggles.map((toggle) => toggle.checked)).toEqual([true, true, false, true, false]);
    const confirm = modal.getByRole("button", { name: "Confirm Cleanup" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
    expect(modal.container.textContent).toContain("Installed content is not backed up");

    fireEvent.click(toggles[4]!);

    expect(confirm.disabled).toBe(true);
    expect(modal.container.textContent).toContain("Not enough free space.");
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
      remove_fully_vanished: false,
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
    expect(modal.container.textContent).toContain("creating recovery · 1 of 2 · Removed Game");

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
      expect(modal.container.textContent).toContain("checking · 1 of 2 · Removed Game");
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

    await act(async () => {
      fireEvent.click(button);
    });
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
    expect(confirm.disabled).toBe(true);
    expect(modal.container.textContent).toContain("Load every page before confirming");

    fireEvent.click(modal.getByRole("button", { name: "Load more (1 of 2)" }));
    await waitFor(() => expect(modal.container.textContent).toContain("Current sibling"));
    expect(confirm.disabled).toBe(false);
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
    expect(modal.container.textContent).toContain("Installed content is not backed up");
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

  it("stages every installed selection above 256 in bounded pages", async () => {
    const items = Array.from({ length: 257 }, (_, index) => ({
      ...preview.items![0]!,
      rom_id: index + 1,
      installed_bytes: 0,
      name: `Game ${index + 1}`,
    }));
    vi.mocked(backend.getPrunePreview).mockResolvedValue({ ...preview, items, total: items.length, free_bytes: 1 });
    vi.mocked(backend.stagePruneInstalledSelection).mockImplementation(async (request) => ({
      success: true,
      selection_id: "selection-many",
      selected_count: request.rom_ids[request.rom_ids.length - 1] ?? 0,
      finalized: request.final,
    }));
    await openRemovedGamesCleanupModal();
    const modal = render(shownModal());
    const toggles = modal.getAllByTestId("toggle-input") as HTMLInputElement[];
    act(() => {
      for (const toggle of toggles.slice(4)) toggle.click();
    });

    fireEvent.click(modal.getByRole("button", { name: "Confirm Cleanup" }));
    await waitFor(() => expect(backend.startPrune).toHaveBeenCalled());

    const pages = vi.mocked(backend.stagePruneInstalledSelection).mock.calls.map(([request]) => request);
    expect(pages.map((page) => page.rom_ids.length)).toEqual([100, 100, 57]);
    expect(pages.map((page) => page.selection_id)).toEqual([null, "selection-many", "selection-many"]);
    expect(pages.map((page) => page.final)).toEqual([false, false, true]);
    expect(vi.mocked(backend.startPrune).mock.calls[0]?.[0].installed_selection_id).toBe("selection-many");
  }, 30_000);

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
    expect(confirm.disabled).toBe(true);
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
    expect(text).toContain("Still on RomM as of your last sync");
    expect(text).toContain("Other versions of the same games are listed below");
    expect(text).not.toContain("disclosed for whole-game removal");
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
