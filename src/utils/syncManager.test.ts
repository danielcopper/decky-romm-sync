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
  getLiveRomMShortcutAppIds: vi.fn(),
}));

// gameDetailPatch pulls in Steam-internal @decky/ui + component imports; mock it
// so the manager's `registerRomMAppId` call is observable without loading them.
const registerRomMAppId = vi.fn();
vi.mock("../patches/gameDetailPatch", () => ({
  registerRomMAppId: (...args: unknown[]) => registerRomMAppId(...args),
}));

import { initUnitSyncManager, requestSyncCancel, resetSyncCancel, isCancelRequested } from "./syncManager";

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

describe("syncManager — group-aware emit: one Steam shortcut per game (ADR-0021)", () => {
  const EXE = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";

  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    vi.mocked(backend.reportUnitResults).mockClear();
    resetSyncDelta();
    resetSyncCancel();
    // The global `SteamClient` stub is torn down by test-setup's
    // `vi.unstubAllGlobals()` after the file's first test, so the update path's
    // bare `SteamClient.Apps.Set*` calls would throw here — re-stub it.
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn(),
        SetShortcutName: vi.fn(),
        SetShortcutExe: vi.fn(),
        SetShortcutStartDir: vi.fn(),
        SetAppLaunchOptions: vi.fn(),
        SetCustomArtworkForApp: vi.fn().mockResolvedValue(undefined),
        RemoveShortcut: vi.fn(),
      },
    });
  });

  function groupItem(
    overrides: Partial<SyncApplyUnitData["shortcuts"][number]>,
  ): SyncApplyUnitData["shortcuts"][number] {
    return {
      rom_id: 0,
      name: "Game",
      exe: EXE,
      start_dir: "/home/deck",
      launch_options: "",
      platform_name: "N64",
      cover_path: "",
      ...overrides,
    };
  }

  it("reuses the existing shortcut for a rebind entry and fetches the representative's artwork", async () => {
    // The backend collapsed a rebinding group to ONE entry keyed to the vanished
    // bound sibling's rom_id (already in Steam as appId 5000), naming the
    // representative in bind_rom_id. The frontend reuses that shortcut.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[1, 5000]]));
    vi.mocked(backend.getArtworkBase64).mockClear();
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "ZGF0YQ==" });
    const jpCmd = 'flatpak run net.retrodeck.retrodeck "/games/zelda_jp.z64"';
    const data: SyncApplyUnitData = {
      run_id: "run-rebind",
      unit_type: "platform",
      unit_id: 1,
      unit_name: "N64",
      unit_index: 0,
      total_units: 1,
      shortcuts: [groupItem({ rom_id: 1, name: "Zelda (USA)", launch_options: jpCmd, bind_rom_id: 2 })],
    };

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", data);
      await flush(150);
    });

    // Reused via the existing-shortcut update path — launch options re-baked to
    // the representative's path via the confirm-poll; never a second AddShortcut.
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(5000, jpCmd);
    expect(addShortcut).not.toHaveBeenCalled();
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "1": 5000 }, "run-rebind", 1);
    // Artwork follows the BINDING target (representative rom 2), NOT the vanished
    // sibling (rom 1) the shortcut is keyed to — covers can be edition-specific.
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(2);
    expect(vi.mocked(backend.getArtworkBase64)).not.toHaveBeenCalledWith(1);
    // And the fetched art was applied to the reused shortcut's appId.
    expect(SteamClient.Apps.SetCustomArtworkForApp).toHaveBeenCalledWith(5000, "ZGF0YQ==", "png", 0);
  });

  it("creates exactly one shortcut per group — a collapsed multi-version game never fans out", async () => {
    // The backend already collapsed each sibling group to ONE entry, so a unit
    // holding two games (both new) yields exactly two AddShortcut calls — never
    // one per underlying dump.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    let next = 6000;
    addShortcut.mockImplementation(async () => next++);
    const data: SyncApplyUnitData = {
      run_id: "run-two-groups",
      unit_type: "platform",
      unit_id: 1,
      unit_name: "N64",
      unit_index: 0,
      total_units: 1,
      shortcuts: [groupItem({ rom_id: 10, name: "Zelda" }), groupItem({ rom_id: 20, name: "Mario" })],
    };

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", data);
      await flush(250);
    });

    // Two groups → exactly two shortcuts, one per game.
    expect(addShortcut).toHaveBeenCalledTimes(2);
    expect(addShortcut.mock.calls.map((c) => c[0].rom_id).sort((a, b) => a - b)).toEqual([10, 20]);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "10": 6000, "20": 6001 }, "run-two-groups", 1);
  });
});

describe("syncManager — registers resolved appIds as RomM-owned at ack time (#1205)", () => {
  const EXE = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";

  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    registerRomMAppId.mockClear();
    vi.mocked(backend.getArtworkBase64).mockReset();
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "ZGF0YQ==" });
    resetSyncDelta();
    resetSyncCancel();
    // The global SteamClient stub is torn down by test-setup after the file's
    // first test; the update/rebind paths call bare `SteamClient.Apps.Set*`, so
    // re-stub it here.
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn(),
        SetShortcutName: vi.fn(),
        SetShortcutExe: vi.fn(),
        SetShortcutStartDir: vi.fn(),
        SetAppLaunchOptions: vi.fn(),
        SetCustomArtworkForApp: vi.fn().mockResolvedValue(undefined),
        RemoveShortcut: vi.fn(),
      },
    });
  });

  function item(overrides: Partial<SyncApplyUnitData["shortcuts"][number]>): SyncApplyUnitData["shortcuts"][number] {
    return {
      rom_id: 0,
      name: "Game",
      exe: EXE,
      start_dir: "/home/deck",
      launch_options: "",
      platform_name: "PSX",
      cover_path: "",
      ...overrides,
    };
  }

  function unitOf(shortcuts: SyncApplyUnitData["shortcuts"], runId: string): SyncApplyUnitData {
    return {
      run_id: runId,
      unit_type: "platform",
      unit_id: 1,
      unit_name: "PSX",
      unit_index: 0,
      total_units: 1,
      shortcuts,
    };
  }

  it("registers a newly created shortcut's appId", async () => {
    // rom 42 has no existing appId → create path → addShortcut returns 6000.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    addShortcut.mockResolvedValue(6000);

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        unitOf([item({ rom_id: 42, name: "Test ROM" })], "run-reg-create"),
      );
      await flush(120);
    });

    expect(registerRomMAppId).toHaveBeenCalledWith(6000);
  });

  it("registers an existing shortcut's appId on the update path", async () => {
    // rom 42 already maps to appId 5000 → update path, never addShortcut.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        unitOf([item({ rom_id: 42, name: "Test ROM" })], "run-reg-update"),
      );
      await flush(120);
    });

    expect(registerRomMAppId).toHaveBeenCalledWith(5000);
    expect(addShortcut).not.toHaveBeenCalled();
  });

  it("registers the reused shortcut's appId for a rebind entry (bind_rom_id)", async () => {
    // Rebind: the entry is keyed to the vanished bound sibling (rom 1, already in
    // Steam as appId 5000). The shortcut is reused by rom_id, so 5000 is the appId
    // the game-detail patch + launch interceptor must recognise — NOT the
    // representative's (bind_rom_id 2, which only steers artwork).
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[1, 5000]]));

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        unitOf([item({ rom_id: 1, name: "Zelda (USA)", bind_rom_id: 2 })], "run-reg-rebind"),
      );
      await flush(150);
    });

    expect(registerRomMAppId).toHaveBeenCalledWith(5000);
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

describe("syncManager — per-run cancel reset (#1198)", () => {
  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    vi.mocked(backend.reportUnitResults).mockClear();
  });

  it("resetSyncCancel clears a stale cancel on the skip-only path (no sync_apply_unit)", () => {
    // Stale cancel from a prior (cancelled) run. resetSyncCancel is what the
    // sync_plan listener calls, and sync_plan fires once per run BEFORE any
    // unit — so even a run whose only work is an incremental SKIP (no
    // sync_apply_unit, hence no per-unit handler reset) must start with a clean
    // flag (#1198). This exercises that exact path: NO sync_apply_unit is
    // dispatched, so the only thing that can clear the flag is resetSyncCancel.
    // Non-vacuous: if resetSyncCancel's `_cancelRequested = false` line were
    // removed, isCancelRequested() would still be true here and this fails.
    requestSyncCancel();
    expect(isCancelRequested()).toBe(true);

    resetSyncCancel();

    expect(isCancelRequested()).toBe(false);
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
