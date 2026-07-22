import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import type { ReactElement } from "react";
import { useCopyToSlot } from "./useCopyToSlot";
import * as backend from "../../api/backend";
import { showSyncConflictModal } from "../SyncConflictModal";
import type { CopySaveToSlotStatus, SaveSlotSummary, SyncConflict } from "../../types";

// Control the callable + the conflict modal directly; everything else (showModal,
// toaster) comes from the global @decky/ui / @decky/api stubs.
vi.mock("../../api/backend", () => ({
  copySaveToSlot: vi.fn(),
  debugLog: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../SyncConflictModal", () => ({
  showSyncConflictModal: vi.fn().mockResolvedValue("resolved"),
}));

const SLOTS: SaveSlotSummary[] = [
  { slot: "autosave", source: "server", count: 1, latest_updated_at: null },
  { slot: "backup", source: "server", count: 1, latest_updated_at: null },
];

const conflict: SyncConflict = {
  type: "sync_conflict",
  rom_id: 42,
  filename: "save.srm",
  server_save_id: 7,
  server_updated_at: "2026-01-01T00:00:00Z",
  server_size: 1024,
  local_path: "/local/save.srm",
  local_hash: "abc",
  local_mtime: "2026-01-01T00:00:00Z",
  local_size: 1024,
  created_at: "2026-01-01T00:00:00Z",
};

const flush = () => new Promise((r) => setTimeout(r, 0));

const dataChanged = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  globalThis.addEventListener("romm_data_changed", dataChanged);
});
afterEach(() => {
  globalThis.removeEventListener("romm_data_changed", dataChanged);
});

// Open the picker for one source save, then drive its onSubmit with a target,
// awaiting the detached copy flow.
async function runCopy(result: CopySaveToSlotStatus, { saveId = 10, sourceSlot = "backup", target = "promoted" } = {}) {
  vi.mocked(backend.copySaveToSlot).mockResolvedValue(result);
  const hook = renderHook(() => useCopyToSlot(42, SLOTS));
  await act(async () => {
    hook.result.current(saveId, sourceSlot);
  });
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[calls.length - 1]![0] as ReactElement;
  const onSubmit = (el.props as { onSubmit: (t: string) => void }).onSubmit;
  await act(async () => {
    onSubmit(target);
    await flush();
  });
}

describe("useCopyToSlot", () => {
  it("opens the picker modal for the source save", async () => {
    const hook = renderHook(() => useCopyToSlot(42, SLOTS));
    act(() => hook.result.current(10, "backup"));
    expect(showModal).toHaveBeenCalledTimes(1);
    const el = vi.mocked(showModal).mock.calls[0]![0] as ReactElement;
    expect((el.props as { sourceSlot: string }).sourceSlot).toBe("backup");
  });

  it("ok (no-slot → new named slot): toasts and dispatches romm_data_changed", async () => {
    await runCopy({ status: "ok" }, { saveId: 10, sourceSlot: "backup", target: "promoted" });

    // Non-vacuous: the distinctive target value flowed through to the callable.
    expect(backend.copySaveToSlot).toHaveBeenCalledWith(42, 10, "promoted");
    expect(toaster.toast).toHaveBeenCalledWith(expect.objectContaining({ body: expect.stringContaining("promoted") }));
    expect(dataChanged).toHaveBeenCalled();
  });

  it("ok (default → autosave): the autosave target value flows through", async () => {
    await runCopy({ status: "ok" }, { saveId: 100, sourceSlot: "default", target: "autosave" });

    expect(backend.copySaveToSlot).toHaveBeenCalledWith(42, 100, "autosave");
    expect(toaster.toast).toHaveBeenCalledWith(expect.objectContaining({ body: expect.stringContaining("autosave") }));
    expect(dataChanged).toHaveBeenCalled();
  });

  it("already_present: toasts the existing save location and does NOT refresh", async () => {
    await runCopy({ status: "already_present", existing_id: 55 }, { target: "promoted" });

    // Names the slot + the existing save id; the "copied" toast and the refresh
    // are both withheld because nothing was copied.
    expect(toaster.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Already in slot 'promoted' as #55") }),
    );
    expect(dataChanged).not.toHaveBeenCalled();
  });

  it("conflict_blocked: opens the sync-conflict modal on the first conflict, no toast, no refresh", async () => {
    await runCopy({ status: "conflict_blocked", conflicts: [conflict] });

    expect(showSyncConflictModal).toHaveBeenCalledWith(conflict);
    // The modal owns the feedback — no stacked toast, and no refresh (unresolved).
    expect(toaster.toast).not.toHaveBeenCalled();
    expect(dataChanged).not.toHaveBeenCalled();
  });

  it("conflict_blocked with an empty conflict list: falls back to a toast", async () => {
    await runCopy({ status: "conflict_blocked", conflicts: [] });

    expect(showSyncConflictModal).not.toHaveBeenCalled();
    expect(toaster.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("sync conflict") }),
    );
  });

  it("target_slot_busy: toasts to resolve the target slot first, no refresh", async () => {
    await runCopy({ status: "target_slot_busy", message: "busy" }, { target: "promoted" });

    expect(toaster.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("sync it first") }),
    );
    expect(dataChanged).not.toHaveBeenCalled();
  });

  const REFUSALS: Array<{ result: CopySaveToSlotStatus; needle: string }> = [
    { result: { status: "version_deleted" }, needle: "no longer exists" },
    { result: { status: "rom_not_installed" }, needle: "no longer installed" },
    { result: { status: "not_configured" }, needle: "Set up save slots" },
    { result: { status: "invalid_slot_name" }, needle: "valid slot name" },
    { result: { status: "server_unreachable", message: "boom" }, needle: "Couldn't reach RomM" },
    { result: { status: "preflight_failed", errors: ["net"] }, needle: "Sync failed before copy" },
    { result: { status: "copy_failed", message: "oops" }, needle: "Couldn't copy the save" },
    { result: { status: "unsupported", reason: "savefiles_in_content_dir" }, needle: "writes saves next to the ROM" },
    { result: { status: "unsupported" }, needle: "multi-file" },
  ];

  it.each(REFUSALS)("refusal $result.status surfaces a toast", async ({ result, needle }) => {
    await runCopy(result);
    expect(toaster.toast).toHaveBeenCalledWith(
      needle ? expect.objectContaining({ body: expect.stringContaining(needle) }) : expect.anything(),
    );
    // A refusal never refreshes the views.
    expect(dataChanged).not.toHaveBeenCalled();
  });

  it("swallows a rejected copy call and surfaces the generic failure toast", async () => {
    vi.mocked(backend.copySaveToSlot).mockRejectedValue(new Error("network down"));
    const hook = renderHook(() => useCopyToSlot(42, SLOTS));
    await act(async () => {
      hook.result.current(10, "backup");
    });
    const calls = vi.mocked(showModal).mock.calls;
    const el = calls[calls.length - 1]![0] as ReactElement;
    const onSubmit = (el.props as { onSubmit: (t: string) => void }).onSubmit;
    await act(async () => {
      onSubmit("promoted");
      await flush();
    });

    // Post-catch state: the fallback toast fired and the error was debug-logged.
    expect(toaster.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Couldn't copy the save") }),
    );
    expect(backend.debugLog).toHaveBeenCalled();
    expect(dataChanged).not.toHaveBeenCalled();
  });
});
