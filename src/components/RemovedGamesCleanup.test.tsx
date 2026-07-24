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
      fs_name: "Removed Game.gba",
      platform_slug: "gba",
      group_id: "group-1",
      group_size: 1,
      bound_count: 0,
      installed: true,
      installed_bytes: 200,
      warning: null,
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
    vi.mocked(backend.startPrune).mockReset();
    vi.mocked(showModal).mockReset();
    vi.mocked(toaster.toast).mockReset();
    resetPruneState();
    vi.mocked(backend.getPrunePreview).mockResolvedValue(preview);
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
      include_installed_rom_ids: [],
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
    expect(modal.container.textContent).toContain("1 removed; 0 skipped or failed.");
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
});
