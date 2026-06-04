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
import type { SyncApplyUnitData } from "../types";

const setLaunchOptionsConfirmed = vi.fn().mockResolvedValue(true);
const addShortcut = vi.fn();
const getExistingRomMShortcuts = vi.fn();
vi.mock("./steamShortcuts", () => ({
  setLaunchOptionsConfirmed: (...args: unknown[]) => setLaunchOptionsConfirmed(...args),
  addShortcut: (...args: unknown[]) => addShortcut(...args),
  getExistingRomMShortcuts: (...args: unknown[]) => getExistingRomMShortcuts(...args),
}));

import { initUnitSyncManager } from "./syncManager";

function unit(launchOptions: string): SyncApplyUnitData {
  return {
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
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd));
      // One shortcut + the 50ms inter-item delay; give the async loop room.
      await flush(120);
    });

    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(5000, cmd);
    expect(addShortcut).not.toHaveBeenCalled();
    // The rom_id→appId binding is reported back to the backend.
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 5000 });
  });
});
