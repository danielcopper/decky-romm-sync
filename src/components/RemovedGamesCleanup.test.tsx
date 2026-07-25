import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { toaster } from "@decky/api";
import { showModal } from "@decky/ui";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as backend from "../api/backend";
import { resetPruneState, setPruneComplete, setPruneProgress } from "../utils/pruneStore";
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
        removed_rom_ids: [7],
        affected_app_ids: [],
        results: [{ group_id: "group-1", rom_ids: [7], status: "removed", message: "Removed." }],
      });
    });
    expect(modal.container.textContent).toContain("1 removed; 0 skipped, partial, or failed.");
    expect(modal.getByRole("button", { name: "Close" })).toBeTruthy();
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
    expect(modal.container.textContent).toContain("selected unavailable ROM");
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
  }, 15_000);

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
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7],
            status: "partial",
            message: "Local cleanup was incomplete.",
            warnings: ["Shared save was retained."],
            warning_count: 1,
          },
        ],
      });
    });

    expect(modal.container.textContent).toContain("Warning: Shared save was retained.");
  });
});
