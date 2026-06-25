/**
 * Exercises the per-unit sync manager's existing-shortcut update path:
 * when a `sync_apply_unit` shortcut is already present in Steam, the manager
 * updates it in place and sets its launch options via the confirm-poll
 * (`setLaunchOptionsConfirmed`) rather than fire-and-forget.
 *
 * steamShortcuts is mocked so the confirm-poll and the existing-shortcut map
 * are observable; backend callables default to the test-setup undefined-stub.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { act } from "@testing-library/react";
import * as backend from "../api/backend";
import { emitDeckyEvent } from "../test-utils/decky-api-mock";
import { resetSyncDelta, getSyncDelta } from "./syncDeltaStore";
import type { SyncApplyUnitData } from "../types";

const setLaunchOptionsConfirmed = vi.fn().mockResolvedValue(true);
const addShortcut = vi.fn();
const getExistingRomMShortcuts = vi.fn();
vi.mock("./steamShortcuts", () => ({
  setLaunchOptionsConfirmed: (...args: unknown[]) => setLaunchOptionsConfirmed(...args),
  addShortcut: (...args: unknown[]) => addShortcut(...args),
  getExistingRomMShortcuts: (...args: unknown[]) => getExistingRomMShortcuts(...args),
}));

import { initUnitSyncManager, requestSyncCancel } from "./syncManager";

function unit(launchOptions: string, runId = "run-1"): SyncApplyUnitData {
  return {
    run_id: runId,
    unit_type: "platform",
    unit_id: 1,
    unit_name: "PSX",
    unit_index: 0,
    total_units: 1,
    shortcuts: [
      {
        rom_id: 42,
        name: "Test ROM",
        exe: "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher",
        start_dir: "/home/deck",
        launch_options: launchOptions,
        platform_name: "PSX",
        cover_path: "",
      },
    ],
  };
}

function flush(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

describe("syncManager — existing-shortcut update uses confirm-poll", () => {
  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
  });

  it("calls setLaunchOptionsConfirmed (not bare SetAppLaunchOptions) for an existing shortcut", async () => {
    // rom 42 already maps to appId 5000 → update path, never addShortcut.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-confirm"));
      // One shortcut + the 50ms inter-item delay; give the async loop room.
      await flush(120);
    });

    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(5000, cmd);
    expect(addShortcut).not.toHaveBeenCalled();
    // The rom_id→appId binding is reported back to the backend, echoing the
    // run + unit identity so the backend can reject a stale ack (#1041).
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 5000 }, "run-confirm", 1);
  });
});

describe("syncManager — does not ack a cancelled unit (#1041)", () => {
  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    vi.mocked(backend.reportUnitResults).mockClear();
  });

  it("skips reportUnitResults when cancel is requested during the unit loop", async () => {
    // Cancel is requested during the once-per-run existing-shortcut scan (a
    // fresh-run cache miss always calls it), which runs before the unit loop —
    // so the loop's cancel check breaks early and the post-loop guard skips the
    // ack. A unique run_id guarantees the module-level scan cache misses here.
    getExistingRomMShortcuts.mockImplementation(async () => {
      requestSyncCancel();
      return new Map<number, number>([[42, 5000]]);
    });
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-cancel-1041"));
      await flush(120);
    });

    // Observable effect of the post-cancel guard: the ack callable is NEVER
    // invoked, so a cancelled run's bindings can't be credited to a fresh run.
    expect(vi.mocked(backend.reportUnitResults)).not.toHaveBeenCalled();
  });
});

describe("syncManager — once-per-run existing-shortcut scan cache", () => {
  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
  });

  it("scans once for two units sharing the same run_id", async () => {
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';
    initUnitSyncManager();

    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-same"));
      await flush(120);
    });
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-same"));
      await flush(120);
    });

    // Second unit reuses the cached scan from the first.
    expect(getExistingRomMShortcuts).toHaveBeenCalledTimes(1);
  });

  it("re-scans when a second unit carries a different run_id", async () => {
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';
    initUnitSyncManager();

    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-diff-a"));
      await flush(120);
    });
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-diff-b"));
      await flush(120);
    });

    // A new run_id is a cache miss → fresh scan.
    expect(getExistingRomMShortcuts).toHaveBeenCalledTimes(2);
  });
});

describe("syncManager — records created shortcuts into the per-run delta store", () => {
  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    resetSyncDelta();
  });

  it("records a freshly created shortcut's appId as an 'added' delta", async () => {
    // rom 42 has no existing appId → create path → addShortcut returns 6000.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    addShortcut.mockResolvedValue(6000);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-create"));
      await flush(120);
    });

    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(getSyncDelta()).toEqual({ added: 1, removed: 0 });
  });

  it("does NOT record the update path (existing shortcut) as a delta", async () => {
    // rom 42 already maps to appId 5000 → update path, never addShortcut.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-update"));
      await flush(120);
    });

    expect(addShortcut).not.toHaveBeenCalled();
    expect(getSyncDelta()).toEqual({ added: 0, removed: 0 });
  });

  it("does NOT record when addShortcut fails to resolve an appId (null)", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    addShortcut.mockResolvedValue(null);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-create-fail"));
      await flush(120);
    });

    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(getSyncDelta()).toEqual({ added: 0, removed: 0 });
  });
});
