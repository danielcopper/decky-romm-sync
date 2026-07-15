/**
 * Exercises the per-unit sync manager's existing-shortcut update path:
 * when a `sync_apply_unit` shortcut is already present in Steam, the manager
 * updates it in place and sets its launch options via the confirm-poll
 * (`setLaunchOptionsConfirmed`) rather than fire-and-forget.
 *
 * steamShortcuts is mocked so the confirm-poll and the existing-shortcut map
 * are observable; backend callables default to the test-setup undefined-stub.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act } from "@testing-library/react";
import * as backend from "../api/backend";
import { emitDeckyEvent } from "../test-utils/decky-api-mock";
import { resetSyncDelta, getSyncDelta } from "./syncDeltaStore";
import { getSyncProgress, onSyncProgressChange } from "./syncProgress";
import type { SyncApplyUnitData, SyncProgress } from "../types";

const setLaunchOptionsConfirmed = vi.fn().mockResolvedValue(true);
const addShortcut = vi.fn();
const getExistingRomMShortcuts = vi.fn();
const getLiveRomMShortcutAppIds = vi.fn();
vi.mock("./steamShortcuts", () => ({
  setLaunchOptionsConfirmed: (...args: unknown[]) => setLaunchOptionsConfirmed(...args),
  addShortcut: (...args: unknown[]) => addShortcut(...args),
  getExistingRomMShortcuts: (...args: unknown[]) => getExistingRomMShortcuts(...args),
  getLiveRomMShortcutAppIds: (...args: unknown[]) => getLiveRomMShortcutAppIds(...args),
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
    chunk_index: 0,
    chunk_count: 1,
    chunk_offset: 0,
    unit_total: 1,
    shortcuts: [
      {
        rom_id: 42,
        name: "Test ROM",
        exe: "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher",
        start_dir: "/home/deck",
        launch_options: launchOptions,
        platform_name: "PSX",
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
    // run + unit + chunk identity so the backend can reject a stale ack (#1041/#1025).
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 5000 }, "run-confirm", 1, 0);
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
      ...overrides,
    };
  }

  it("reuses the existing shortcut for a rebind entry without applying cover artwork", async () => {
    // The backend collapsed a rebinding group to ONE entry keyed to the vanished
    // bound sibling's rom_id (already in Steam as appId 5000). The frontend reuses
    // that shortcut by rom_id, so a rebind lands on the update path.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[1, 5000]]));
    vi.mocked(backend.getArtworkBase64).mockClear();
    const jpCmd = 'flatpak run net.retrodeck.retrodeck "/games/zelda_jp.z64"';
    const data: SyncApplyUnitData = {
      run_id: "run-rebind",
      unit_type: "platform",
      unit_id: 1,
      unit_name: "N64",
      unit_index: 0,
      total_units: 1,
      chunk_index: 0,
      chunk_count: 1,
      chunk_offset: 0,
      unit_total: 1,
      shortcuts: [groupItem({ rom_id: 1, name: "Zelda (USA)", launch_options: jpCmd })],
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
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "1": 5000 }, "run-rebind", 1, 0);
    // Covers are applied to CREATES only. A rebind is an update (existing shortcut),
    // so no cover is fetched or pushed here — its existing grid file stays.
    expect(vi.mocked(backend.getArtworkBase64)).not.toHaveBeenCalled();
    expect(SteamClient.Apps.SetCustomArtworkForApp).not.toHaveBeenCalled();
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
      chunk_index: 0,
      chunk_count: 1,
      chunk_offset: 0,
      unit_total: 2,
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
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith(
      { "10": 6000, "20": 6001 },
      "run-two-groups",
      1,
      0,
    );
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
      chunk_index: 0,
      chunk_count: 1,
      chunk_offset: 0,
      unit_total: shortcuts.length,
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

  it("registers the reused shortcut's appId for a rebind entry", async () => {
    // Rebind: the entry is keyed to the vanished bound sibling (rom 1, already in
    // Steam as appId 5000). The shortcut is reused by rom_id, so 5000 is the appId
    // the game-detail patch + launch interceptor must recognise. The binding target
    // rides the backend's internal state, never the wire — the frontend needs only
    // the entry's own rom_id here.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[1, 5000]]));

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        unitOf([item({ rom_id: 1, name: "Zelda (USA)" })], "run-reg-rebind"),
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

describe("syncManager — chunked apply (#1025)", () => {
  const EXE = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";

  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    // Update path: every rom already maps to an appId, so the loop takes the
    // in-place Set* branch (no addShortcut / artwork-fetch complexity).
    getExistingRomMShortcuts.mockResolvedValue(
      new Map<number, number>([
        [1, 5001],
        [2, 5002],
        [3, 5003],
      ]),
    );
    vi.mocked(backend.reportUnitResults).mockClear();
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

  function sc(romId: number): SyncApplyUnitData["shortcuts"][number] {
    return {
      rom_id: romId,
      name: `ROM ${romId}`,
      exe: EXE,
      start_dir: "/home/deck",
      launch_options: "",
      platform_name: "PSX",
    };
  }

  function chunkOf(
    shortcuts: SyncApplyUnitData["shortcuts"],
    opts: { chunkIndex: number; chunkOffset: number; chunkCount: number; unitTotal: number; runId: string },
  ): SyncApplyUnitData {
    return {
      run_id: opts.runId,
      unit_type: "platform",
      unit_id: 1,
      unit_name: "PSX",
      unit_index: 0,
      total_units: 1,
      chunk_index: opts.chunkIndex,
      chunk_count: opts.chunkCount,
      chunk_offset: opts.chunkOffset,
      unit_total: opts.unitTotal,
      shortcuts,
    };
  }

  it("advances progress continuously across two chunks of one unit", async () => {
    // Unit "PSX" has 3 shortcuts split into chunk 0 (rom 1,2) and chunk 1
    // (rom 3). Progress must read unit-wide — 1/3, 2/3, 3/3 — never restart at
    // 0 per chunk, so a 3084-ROM platform shows one smooth bar.
    const snapshots: { current: number | undefined; message: string | undefined; total: number | undefined }[] = [];
    const unsub = onSyncProgressChange(() => {
      const p = getSyncProgress();
      snapshots.push({ current: p.current, message: p.message, total: p.total });
    });

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        chunkOf([sc(1), sc(2)], { chunkIndex: 0, chunkOffset: 0, chunkCount: 2, unitTotal: 3, runId: "run-chunked" }),
      );
      await flush(180);
    });
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        chunkOf([sc(3)], { chunkIndex: 1, chunkOffset: 2, chunkCount: 2, unitTotal: 3, runId: "run-chunked" }),
      );
      await flush(120);
    });
    unsub();

    const currents = snapshots.map((s) => s.current ?? 0);
    // The unit-wide counter is monotonic non-decreasing — chunk 1 never resets
    // the bar back to 0 (the buggy per-chunk reset would break this).
    let prev = currents[0] ?? 0;
    for (const c of currents.slice(1)) {
      expect(c).toBeGreaterThanOrEqual(prev);
      prev = c;
    }
    // Every item's message counts against the UNIT total, not the chunk length.
    const messages = snapshots.map((s) => s.message);
    expect(messages).toContain("PSX: 1/3");
    expect(messages).toContain("PSX: 2/3");
    expect(messages).toContain("PSX: 3/3");
    // Chunk 1 would read "PSX: 1/1" if progress used the chunk length — it must not.
    expect(messages).not.toContain("PSX: 1/1");
    // Final state: the whole unit is done, 3/3.
    const final = snapshots[snapshots.length - 1];
    expect(final?.current).toBe(3);
    expect(final?.total).toBe(3);
  });

  it("per-item update carries the chunk-constant fields so the store self-heals after a QAM remount wipe", async () => {
    // Every per-item update must re-assert the full field set (running / stage /
    // total / step / totalSteps), not just current + message — so a mid-chunk
    // QAM remount that replaced the module store with the backend's coarse
    // snapshot is healed on the next item, well before the chunk boundary.
    const snapshots: SyncProgress[] = [];
    const unsub = onSyncProgressChange(() => {
      snapshots.push({ ...getSyncProgress() });
    });

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        chunkOf([sc(1), sc(2)], { chunkIndex: 0, chunkOffset: 0, chunkCount: 1, unitTotal: 2, runId: "run-selfheal" }),
      );
      await flush(180);
    });
    unsub();

    // The first per-item snapshot (fine current advanced to 1) carries the whole
    // field set, not a bare current/message pair.
    const perItem = snapshots.find((s) => s.message === "PSX: 1/2");
    expect(perItem).toBeDefined();
    expect(perItem).toMatchObject({
      running: true,
      stage: "applying",
      current: 1,
      total: 2,
      step: 1,
      totalSteps: 1,
      message: "PSX: 1/2",
    });
  });

  it("acks each chunk with its own chunk_index", async () => {
    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        chunkOf([sc(1), sc(2)], { chunkIndex: 0, chunkOffset: 0, chunkCount: 2, unitTotal: 3, runId: "run-ack" }),
      );
      await flush(180);
    });
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        chunkOf([sc(3)], { chunkIndex: 1, chunkOffset: 2, chunkCount: 2, unitTotal: 3, runId: "run-ack" }),
      );
      await flush(120);
    });

    // Each chunk acks only its own bindings, echoing its own chunk index so the
    // backend commits the matching chunk (a stale-chunk ack is rejected).
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenNthCalledWith(1, { "1": 5001, "2": 5002 }, "run-ack", 1, 0);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenNthCalledWith(2, { "3": 5003 }, "run-ack", 1, 1);
  });

  it("drops a second sync_apply_unit that arrives while one is mid-processing", async () => {
    // Event 1 hangs at the once-per-run existing-shortcut scan via a deferred
    // promise, holding the module's in-flight guard set. Event 2 then arrives with
    // a DIFFERENT run_id — so, were the guard absent, it would kick off its own
    // (cache-missing) scan and its own ack. The guard must drop it instead. This
    // is the overlap that, unguarded, corrupts the shared per-unit state.
    let resolveScan!: (m: Map<number, number>) => void;
    getExistingRomMShortcuts.mockReturnValue(
      new Promise<Map<number, number>>((r) => {
        resolveScan = r;
      }),
    );

    initUnitSyncManager();

    // Event 1: starts, suspends on the hung scan (in-flight guard now set).
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        chunkOf([sc(1)], { chunkIndex: 0, chunkOffset: 0, chunkCount: 1, unitTotal: 1, runId: "run-guard-1" }),
      );
      await flush(0);
    });

    // Event 2: arrives while event 1 is still hung → dropped by the guard before
    // it can scan or process anything.
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        chunkOf([sc(2)], { chunkIndex: 0, chunkOffset: 0, chunkCount: 1, unitTotal: 1, runId: "run-guard-2" }),
      );
      await flush(0);
    });

    // Observable proof of the drop: only event 1's scan ever ran, and no ack has
    // fired (event 1 is still hung, event 2 never processed).
    expect(getExistingRomMShortcuts).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.reportUnitResults)).not.toHaveBeenCalled();

    // Release event 1 and let it run to completion.
    await act(async () => {
      resolveScan(new Map<number, number>([[1, 5001]]));
      await flush(120);
    });

    // Event 1 finished normally and acked exactly once for its own run; event 2's
    // binding never reached the backend — still one scan, one ack.
    expect(getExistingRomMShortcuts).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "1": 5001 }, "run-guard-1", 1, 0);
  });
});

describe("syncManager — applies cover artwork to created shortcuts via the API (#1391)", () => {
  const EXE = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";
  const setCustomArtwork = vi.fn().mockResolvedValue(undefined);
  // logError is a plain wrapper (not a callable), so spy to observe the fail-soft path.
  let logErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    vi.mocked(backend.reportUnitResults).mockClear();
    vi.mocked(backend.getArtworkBase64).mockReset();
    setCustomArtwork.mockClear();
    setCustomArtwork.mockResolvedValue(undefined);
    logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    resetSyncDelta();
    resetSyncCancel();
    // test-setup's afterEach vi.unstubAllGlobals wipes the ambient globals after the
    // file's first test, so re-stub SteamClient with the artwork method the create
    // path pushes covers through.
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn(),
        SetShortcutName: vi.fn(),
        SetShortcutExe: vi.fn(),
        SetShortcutStartDir: vi.fn(),
        SetAppLaunchOptions: vi.fn(),
        SetCustomArtworkForApp: setCustomArtwork,
        RemoveShortcut: vi.fn(),
      },
    });
  });

  afterEach(() => {
    logErrorSpy.mockRestore();
  });

  function sc(romId: number): SyncApplyUnitData["shortcuts"][number] {
    return {
      rom_id: romId,
      name: `ROM ${romId}`,
      exe: EXE,
      start_dir: "/home/deck",
      launch_options: "",
      platform_name: "PSX",
    };
  }

  function chunkOf(shortcuts: SyncApplyUnitData["shortcuts"], runId: string): SyncApplyUnitData {
    return {
      run_id: runId,
      unit_type: "platform",
      unit_id: 1,
      unit_name: "PSX",
      unit_index: 0,
      total_units: 1,
      chunk_index: 0,
      chunk_count: 1,
      chunk_offset: 0,
      unit_total: shortcuts.length,
      shortcuts,
    };
  }

  it("applies the fetched cover to each newly created shortcut (appId, base64, png, 0)", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    addShortcut.mockResolvedValue(6000);
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "COVERPNG" });

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", chunkOf([sc(42)], "run-cover-create"));
      await flush(120);
    });

    // The cover is fetched by the item's OWN rom_id (the representative on create)
    // and pushed through Steam's artwork API for the created appId, "png", flag 0.
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(42);
    expect(setCustomArtwork).toHaveBeenCalledWith(6000, "COVERPNG", "png", 0);
  });

  it("does NOT apply a cover on the update path (existing shortcut)", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "COVERPNG" });

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", chunkOf([sc(42)], "run-cover-update"));
      await flush(120);
    });

    // An updated shortcut keeps its existing grid file — no cover fetched or applied.
    expect(addShortcut).not.toHaveBeenCalled();
    expect(vi.mocked(backend.getArtworkBase64)).not.toHaveBeenCalled();
    expect(setCustomArtwork).not.toHaveBeenCalled();
  });

  it("fetches the cover but applies nothing (no error) when the ROM has no cover (base64: null)", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    addShortcut.mockResolvedValue(6000);
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", chunkOf([sc(42)], "run-cover-null"));
      await flush(120);
    });

    // base64 null → the artwork API is never called and nothing errors; the shortcut
    // is still created and acked.
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(42);
    expect(setCustomArtwork).not.toHaveBeenCalled();
    expect(logErrorSpy).not.toHaveBeenCalled();
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 6000 }, "run-cover-null", 1, 0);
  });

  it("fail-soft: a cover apply failure is logged and the created shortcut is still acked", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    addShortcut.mockResolvedValue(6000);
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "COVERPNG" });
    setCustomArtwork.mockRejectedValueOnce(new Error("artwork boom"));

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", chunkOf([sc(42)], "run-cover-fail"));
      await flush(120);
    });

    // The failure is logged and the item is NOT failed — its binding is still acked
    // (a cover that can't apply never breaks the shortcut; the backend grid copy is
    // the durability net).
    expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("failed to apply cover for rom 42"));
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 6000 }, "run-cover-fail", 1, 0);
  });

  it("re-applies covers for the chunk's cover_refreshes entries (existing shortcuts, #1386)", async () => {
    // The backend's invalidation pass found two existing shortcuts whose server
    // cover changed. The frontend must fetch each fresh cover (already re-downloaded
    // into the backend cache) and push it through SetCustomArtworkForApp so the
    // tile refreshes in-session — then still ack the chunk.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    vi.mocked(backend.getArtworkBase64).mockImplementation(async (romId: number) => ({
      base64: `COVER-${romId}`,
    }));

    const data = chunkOf([sc(42)], "run-cover-refresh");
    data.cover_refreshes = [
      { rom_id: 42, app_id: 5000 },
      { rom_id: 77, app_id: 5077 },
    ];

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", data);
      await flush(300);
    });

    // Each refresh entry is fetched by rom_id and applied to its EXISTING appId
    // ("png", flag 0) — including rom 77, which is not in the chunk's shortcuts
    // at all (a delta-skipped item whose cover changed).
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(42);
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(77);
    expect(setCustomArtwork).toHaveBeenCalledWith(5000, "COVER-42", "png", 0);
    expect(setCustomArtwork).toHaveBeenCalledWith(5077, "COVER-77", "png", 0);
    // The refreshes ran BEFORE the ack (the backend's heartbeat-fed wait covers
    // them), and the chunk still acked its own binding.
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 5000 }, "run-cover-refresh", 1, 0);
  });

  it("cover-only chunk: empty shortcuts with cover_refreshes applies every cover and acks (#1386 flow gap)", async () => {
    // The empty-delta apply vehicle: a cover-only sync emits ONE chunk with
    // `shortcuts: []` whose only payload is the refresh list. No shortcut work
    // happens, every refresh entry is pushed to its existing tile, and the
    // empty ack still fires so the backend commits the unit.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    vi.mocked(backend.getArtworkBase64).mockImplementation(async (romId: number) => ({
      base64: `COVER-${romId}`,
    }));

    const data = chunkOf([], "run-cover-only");
    data.cover_refreshes = [
      { rom_id: 10, app_id: 7010 },
      { rom_id: 11, app_id: 7011 },
    ];

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", data);
      await flush(300);
    });

    expect(addShortcut).not.toHaveBeenCalled();
    expect(setCustomArtwork).toHaveBeenCalledWith(7010, "COVER-10", "png", 0);
    expect(setCustomArtwork).toHaveBeenCalledWith(7011, "COVER-11", "png", 0);
    expect(logErrorSpy).not.toHaveBeenCalled();
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({}, "run-cover-only", 1, 0);
  });

  it("fail-soft: one refresh entry's failure never blocks the rest or the ack (#1386)", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "COVERPNG" });
    setCustomArtwork.mockRejectedValueOnce(new Error("artwork boom"));

    const data = chunkOf([], "run-cover-refresh-fail");
    data.cover_refreshes = [
      { rom_id: 1, app_id: 5001 },
      { rom_id: 2, app_id: 5002 },
    ];

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", data);
      await flush(250);
    });

    // Entry 1 failed (logged), entry 2 still applied, and the chunk acked.
    expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("failed to apply cover for rom 1"));
    expect(setCustomArtwork).toHaveBeenCalledWith(5002, "COVERPNG", "png", 0);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({}, "run-cover-refresh-fail", 1, 0);
  });

  it("skips remaining refresh entries once cancel is requested (#1386)", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    vi.mocked(backend.getArtworkBase64).mockImplementation(async (romId: number) => {
      // Cancel lands while the FIRST refresh entry is mid-fetch.
      if (romId === 1) requestSyncCancel();
      return { base64: `COVER-${romId}` };
    });

    const data = chunkOf([], "run-cover-refresh-cancel");
    data.cover_refreshes = [
      { rom_id: 1, app_id: 5001 },
      { rom_id: 2, app_id: 5002 },
    ];

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", data);
      await flush(250);
    });

    // Entry 1 completes (already in flight); entry 2 is never fetched, and the
    // cancelled chunk is not acked.
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledTimes(1);
    expect(setCustomArtwork).toHaveBeenCalledTimes(1);
    expect(setCustomArtwork).toHaveBeenCalledWith(5001, "COVER-1", "png", 0);
    expect(vi.mocked(backend.reportUnitResults)).not.toHaveBeenCalled();
  });

  it("processes no refreshes when the field is absent (older payload shape)", async () => {
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "COVERPNG" });

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", chunkOf([sc(42)], "run-no-refresh-field"));
      await flush(120);
    });

    // Update path + no cover_refreshes → no cover work at all, ack still fires.
    expect(vi.mocked(backend.getArtworkBase64)).not.toHaveBeenCalled();
    expect(setCustomArtwork).not.toHaveBeenCalled();
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 5000 }, "run-no-refresh-field", 1, 0);
  });

  it("applies no cover for items reached after the cancel check breaks the loop", async () => {
    // Cancel is requested during the once-per-run scan, before the loop. The first
    // item is processed and applies its cover, then the post-item cancel check breaks
    // the loop — the second item is never reached, so its cover is never fetched, and
    // the cancelled unit is not acked.
    getExistingRomMShortcuts.mockImplementation(async () => {
      requestSyncCancel();
      return new Map<number, number>();
    });
    let next = 6000;
    addShortcut.mockImplementation(async () => next++);
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "COVERPNG" });

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", chunkOf([sc(10), sc(20)], "run-cover-cancel"));
      await flush(200);
    });

    // Only the first item's cover was fetched/applied; the second item after the
    // cancel check got none, and the cancelled unit is not acked.
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(10);
    expect(setCustomArtwork).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.reportUnitResults)).not.toHaveBeenCalled();
  });
});

describe("syncManager — adopts orphan shortcuts instead of creating duplicates (#1366)", () => {
  const EXE = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";
  const setShortcutName = vi.fn();
  const setShortcutExe = vi.fn();
  const setShortcutStartDir = vi.fn();
  const setCustomArtwork = vi.fn().mockResolvedValue(undefined);
  // The active appStore.GetAppOverviewByAppID spy, re-created by each stubAppStore
  // call so a test can assert the pool build did (or did NOT) resolve any names.
  let getAppOverview: ReturnType<typeof vi.fn>;

  /** Stub appStore so orphan appIds resolve to the given display names (unlisted → null overview). */
  function stubAppStore(names: Record<number, string>): void {
    getAppOverview = vi.fn((appId: number) =>
      names[appId] !== undefined ? { appid: appId, strDisplayName: names[appId], display_name: names[appId] } : null,
    );
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: getAppOverview, allApps: [] });
  }

  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    addShortcut.mockReset();
    getExistingRomMShortcuts.mockReset();
    getLiveRomMShortcutAppIds.mockReset();
    registerRomMAppId.mockClear();
    vi.mocked(backend.reportUnitResults).mockClear();
    vi.mocked(backend.getArtworkBase64).mockReset();
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
    setShortcutName.mockClear();
    setShortcutExe.mockClear();
    setShortcutStartDir.mockClear();
    setCustomArtwork.mockClear();
    setCustomArtwork.mockResolvedValue(undefined);
    resetSyncDelta();
    resetSyncCancel();
    // test-setup's afterEach vi.unstubAllGlobals wipes ambient globals after the
    // file's first test — re-stub the Set* / artwork surface the adoption path uses.
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn(),
        SetShortcutName: setShortcutName,
        SetShortcutExe: setShortcutExe,
        SetShortcutStartDir: setShortcutStartDir,
        SetAppLaunchOptions: vi.fn(),
        SetCustomArtworkForApp: setCustomArtwork,
        RemoveShortcut: vi.fn(),
      },
    });
    stubAppStore({});
  });

  function item(overrides: Partial<SyncApplyUnitData["shortcuts"][number]>): SyncApplyUnitData["shortcuts"][number] {
    return {
      rom_id: 0,
      name: "Game",
      exe: EXE,
      start_dir: "/home/deck",
      launch_options: "",
      platform_name: "PSX",
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
      chunk_index: 0,
      chunk_count: 1,
      chunk_offset: 0,
      unit_total: shortcuts.length,
      shortcuts,
    };
  }

  it("adopts a live orphan matched by name — no AddShortcut, Set* on the orphan appId, cover applied", async () => {
    // rom 42 is unbound (create path). A live RomM-owned orphan (appId 9000)
    // carries the same display name → adopt it instead of minting a duplicate.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    stubAppStore({ 9000: "Test ROM" });
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "COVERPNG" });
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-happy"));
      await flush(150);
    });

    // No new shortcut minted — the orphan's appId is reused and rewritten via the
    // update-path Set* calls + the launch-options confirm-poll.
    expect(addShortcut).not.toHaveBeenCalled();
    expect(setShortcutName).toHaveBeenCalledWith(9000, "Test ROM");
    expect(setShortcutExe).toHaveBeenCalledWith(9000, EXE);
    expect(setShortcutStartDir).toHaveBeenCalledWith(9000, "/home/deck");
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(9000, cmd);
    // The reused appId is registered RomM-owned and reported back for rom 42.
    expect(registerRomMAppId).toHaveBeenCalledWith(9000);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 9000 }, "run-adopt-happy", 1, 0);
    // Cover is applied like a fresh create (idempotent overwrite of the grid file).
    expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(42);
    expect(setCustomArtwork).toHaveBeenCalledWith(9000, "COVERPNG", "png", 0);
    // Delta decision (#1366): an adoption brings a game under management → counted
    // as "added" in the user-facing toast delta, exactly like a fresh create.
    expect(getSyncDelta()).toEqual({ added: 1, removed: 0 });
  });

  it("creates a fresh shortcut when no orphan name matches", async () => {
    // A live orphan exists (appId 9000) but its name is different, so rom 42's
    // create finds no adoptable match → AddShortcut mints a new shortcut.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    stubAppStore({ 9000: "Some Other Game" });
    addShortcut.mockResolvedValue(6000);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-nomatch"));
      await flush(150);
    });

    // No orphan of this name → fresh create, and the orphan's appId is untouched.
    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(setShortcutName).not.toHaveBeenCalledWith(9000, expect.anything());
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 6000 }, "run-adopt-nomatch", 1, 0);
    expect(getSyncDelta()).toEqual({ added: 1, removed: 0 });
  });

  it("disables adoption for the run when the live scan is null (store unreadable)", async () => {
    // A null live scan means Steam's shortcut store was unreadable — never adopt
    // against a store we couldn't read; the pool is empty and rom 42 is created.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue(null);
    addShortcut.mockResolvedValue(6000);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-null"));
      await flush(150);
    });

    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 6000 }, "run-adopt-null", 1, 0);
  });

  it("never adopts the same orphan twice — two same-named items, one orphan → first adopts, second creates", async () => {
    // Two unbound ROMs share a display name; a single orphan (appId 9000) matches
    // it. The first item adopts the orphan; the second finds the bucket drained
    // and mints a fresh shortcut. The live scan runs exactly once for the run.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    stubAppStore({ 9000: "Dup" });
    addShortcut.mockResolvedValue(6000);

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        unitOf([item({ rom_id: 10, name: "Dup" }), item({ rom_id: 20, name: "Dup" })], "run-adopt-twice"),
      );
      await flush(250);
    });

    // Exactly one fresh create (the second item); the orphan 9000 was adopted once.
    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(setShortcutName).toHaveBeenCalledWith(9000, "Dup");
    // rom 10 adopted 9000, rom 20 created 6000 — the orphan is bound to one rom only.
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith(
      { "10": 9000, "20": 6000 },
      "run-adopt-twice",
      1,
      0,
    );
    // The expensive live scan ran a single time this run (once in the shared
    // existing-shortcut scan), and the pool reused it — not a second sweep.
    expect(getLiveRomMShortcutAppIds).toHaveBeenCalledTimes(1);
    // Both are newly-managed → 2 added (one adopt + one create).
    expect(getSyncDelta()).toEqual({ added: 2, removed: 0 });
  });

  it("skips the second scan — the orphan pool reuses the run's one live scan", async () => {
    // A create-bearing run must run the expensive RegisterForAppDetails sweep
    // exactly once: the existing-shortcut scan owns it, and the orphan pool reuses
    // that cached list rather than scanning again (#1366).
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    stubAppStore({ 9000: "Test ROM" });
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-onescan"));
      await flush(150);
    });

    // The orphan was adopted (single scan feeds both the bound map and the pool)…
    expect(setShortcutName).toHaveBeenCalledWith(9000, "Test ROM");
    // …and the sweep ran exactly once for the whole run.
    expect(getLiveRomMShortcutAppIds).toHaveBeenCalledTimes(1);
  });

  it("builds no orphan pool on a run with zero creates (lazy — pool build skipped)", async () => {
    // Every rom is already bound → the update path only. The once-per-run live
    // scan still runs (it builds the bound map), but the orphan POOL — the
    // appStore name-resolution pass — is built lazily on the first CREATE
    // candidate, so a create-free run never touches appStore. liveAppIds carries a
    // would-be orphan (9000) so the assertion is non-vacuous: an eager build would
    // resolve its name.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-lazy"));
      await flush(150);
    });

    // Update path only → the orphan pool (appStore name resolution) was never built.
    expect(addShortcut).not.toHaveBeenCalled();
    expect(getAppOverview).not.toHaveBeenCalled();
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 5000 }, "run-adopt-lazy", 1, 0);
  });

  it("never adopts an appId already bound to another rom, even on a name match", async () => {
    // The real hazard: rom 42 is bound to appId 5000 ("Bound Game"), and rom 43 is
    // an UNBOUND create with the SAME display name. If the pool build failed to
    // skip already-bound live appIds, 5000 would sit in the pool under "Bound
    // Game" and rom 43 would ADOPT it — double-binding one appId to two roms. The
    // skip must exclude 5000, so rom 43 finds no match and mints a fresh shortcut.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>([[42, 5000]]));
    getLiveRomMShortcutAppIds.mockResolvedValue([5000, 9000]);
    // appStore names 5000 "Bound Game" so a BROKEN skip would make it adoptable
    // under rom 43's name — the non-vacuous trap. 9000 is an unrelated orphan.
    stubAppStore({ 5000: "Bound Game", 9000: "Orphan Game" });
    addShortcut.mockResolvedValue(6000);

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>(
        "sync_apply_unit",
        unitOf([item({ rom_id: 42, name: "Bound Game" }), item({ rom_id: 43, name: "Bound Game" })], "run-adopt-bound"),
      );
      await flush(200);
    });

    // rom 43 minted a fresh shortcut (6000) rather than stealing the bound 5000 —
    // the ack maps 43 to 6000, NOT to 5000 (which would be the double-bind bug).
    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(addShortcut).toHaveBeenCalledWith(expect.objectContaining({ rom_id: 43 }));
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith(
      { "42": 5000, "43": 6000 },
      "run-adopt-bound",
      1,
      0,
    );
  });

  it("disables adoption when appStore is unavailable", async () => {
    // The live scan found an orphan (9000) but appStore is gone, so no name can be
    // resolved — the pool is empty (disabled) and rom 42 is freshly created.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    vi.stubGlobal("appStore", undefined);
    addShortcut.mockResolvedValue(6000);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-noappstore"));
      await flush(150);
    });

    // appStore gone → adoption disabled → rom 42 minted fresh (non-vacuous: a
    // present appStore naming 9000 "Test ROM" would have adopted instead).
    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 6000 }, "run-adopt-noappstore", 1, 0);
  });

  it("skips an orphan whose overview resolves to no display name", async () => {
    // The orphan's overview carries an empty display name → it can't be matched by
    // name and is skipped, so rom 42 falls through to a fresh create.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: () => ({ appid: 9000, strDisplayName: "", display_name: "" }),
      allApps: [],
    });
    addShortcut.mockResolvedValue(6000);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-noname"));
      await flush(150);
    });

    // Empty name → orphan not poolable → fresh create.
    expect(addShortcut).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.reportUnitResults)).toHaveBeenCalledWith({ "42": 6000 }, "run-adopt-noname", 1, 0);
  });

  it("rebuilds the pool for a new run_id (per-run cache reset)", async () => {
    // Run A adopts the orphan; run B (a fresh run_id) with its own create must
    // re-scan — the pool is keyed by run_id, so a new run mints a fresh pool.
    getExistingRomMShortcuts.mockResolvedValue(new Map<number, number>());
    getLiveRomMShortcutAppIds.mockResolvedValue([9000]);
    stubAppStore({ 9000: "Test ROM" });
    addShortcut.mockResolvedValue(6000);
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/test.bin"';

    initUnitSyncManager();
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-cacheA"));
      await flush(150);
    });
    await act(async () => {
      emitDeckyEvent<[SyncApplyUnitData]>("sync_apply_unit", unit(cmd, "run-adopt-cacheB"));
      await flush(150);
    });

    // Two distinct runs → two scans (one per run), not one shared across runs.
    expect(getLiveRomMShortcutAppIds).toHaveBeenCalledTimes(2);
  });
});
