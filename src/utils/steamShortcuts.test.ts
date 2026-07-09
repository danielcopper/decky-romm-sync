import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as backend from "../api/backend";
import {
  addShortcut,
  getExistingRomMShortcuts,
  getLiveRomMShortcutAppIds,
  setLaunchOptionsConfirmed,
} from "./steamShortcuts";
import type { SyncAddItem } from "../types";

const ROM_LAUNCHER = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";

/**
 * Builds a RegisterForAppDetails mock that, on registration, schedules a single
 * callback fire (via queueMicrotask) carrying the details produced by
 * ``detailsFor(appId)``. Returning ``undefined`` simulates a runtime that never
 * delivers usable details (early/no-data fire), driving the timeout branch.
 */
function makeRegisterForAppDetails(detailsFor: (appId: number) => SteamAppDetails | undefined) {
  const unregister = vi.fn();
  const fn = vi.fn((appId: number, callback: (d: SteamAppDetails | undefined) => void) => {
    queueMicrotask(() => callback(detailsFor(appId)));
    return { unregister };
  });
  return { fn, unregister };
}

describe("setLaunchOptionsConfirmed", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  it("fires SetAppLaunchOptions and resolves true when the read-back matches", async () => {
    const value = 'flatpak run net.retrodeck.retrodeck "/games/x.bin"';
    const setLaunchOptions = vi.fn();
    const { fn } = makeRegisterForAppDetails(() => ({ strLaunchOptions: value }));
    vi.stubGlobal("SteamClient", {
      Apps: { SetAppLaunchOptions: setLaunchOptions, RegisterForAppDetails: fn },
    });

    await expect(setLaunchOptionsConfirmed(123, value)).resolves.toBe(true);
    expect(setLaunchOptions).toHaveBeenCalledWith(123, value);
  });

  it("confirms an empty-string value against an empty read-back", async () => {
    const setLaunchOptions = vi.fn();
    const { fn } = makeRegisterForAppDetails(() => ({ strLaunchOptions: "" }));
    vi.stubGlobal("SteamClient", {
      Apps: { SetAppLaunchOptions: setLaunchOptions, RegisterForAppDetails: fn },
    });

    await expect(setLaunchOptionsConfirmed(7, "")).resolves.toBe(true);
    expect(setLaunchOptions).toHaveBeenCalledWith(7, "");
  });

  it("resolves false and unregisters on timeout when the read-back never matches", async () => {
    vi.useFakeTimers();
    const setLaunchOptions = vi.fn();
    // Read-back always reports a stale value, so the match never happens.
    const { fn, unregister } = makeRegisterForAppDetails(() => ({ strLaunchOptions: "stale" }));
    vi.stubGlobal("SteamClient", {
      Apps: { SetAppLaunchOptions: setLaunchOptions, RegisterForAppDetails: fn },
    });

    const promise = setLaunchOptionsConfirmed(99, "new-value", 2000);
    // Flush the queued microtask callback (reports "stale", no match) then the timeout.
    await vi.advanceTimersByTimeAsync(2000);

    await expect(promise).resolves.toBe(false);
    expect(setLaunchOptions).toHaveBeenCalledWith(99, "new-value");
    expect(unregister).toHaveBeenCalled();
  });
});

describe("getLiveRomMShortcutAppIds", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the raw appIds of our-exe shortcuts with no backend-map intersection", async () => {
    const exeByAppId: Record<number, string> = {
      10: ROM_LAUNCHER,
      20: ROM_LAUNCHER,
      30: "/usr/bin/some-other-game",
    };
    const { fn } = makeRegisterForAppDetails((appId) => ({ strShortcutExe: exeByAppId[appId] ?? "" }));
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: fn } });
    vi.stubGlobal("collectionStore", {
      deckDesktopApps: {
        apps: new Map([
          [10, {}],
          [20, {}],
          [30, {}],
        ]),
      },
    });
    // Reconcile must NOT touch the backend map — the raw live set is exe-only.
    const mapSpy = vi.mocked(backend.getAppIdRomIdMap);

    const result = await getLiveRomMShortcutAppIds();
    expect(result).toEqual([10, 20]);
    expect(mapSpy).not.toHaveBeenCalled();
  });

  it("returns null when collectionStore is undefined (scan could not run)", async () => {
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: vi.fn() } });
    vi.stubGlobal("collectionStore", undefined);
    const result = await getLiveRomMShortcutAppIds();
    expect(result).toBeNull();
  });

  it("returns null when deckDesktopApps.apps is absent (store unreadable)", async () => {
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: vi.fn() } });
    vi.stubGlobal("collectionStore", { deckDesktopApps: undefined });
    const result = await getLiveRomMShortcutAppIds();
    expect(result).toBeNull();
  });

  it("returns [] when the scan ran but found no RomM shortcuts", async () => {
    const { fn } = makeRegisterForAppDetails(() => ({ strShortcutExe: "/usr/bin/other" }));
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: fn } });
    vi.stubGlobal("collectionStore", { deckDesktopApps: { apps: new Map([[10, {}]]) } });
    const result = await getLiveRomMShortcutAppIds();
    expect(result).toEqual([]);
  });
});

describe("getExistingRomMShortcuts", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("maps romId→appId for shortcuts with our exe AND a backend binding", async () => {
    const exeByAppId: Record<number, string> = {
      10: "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher",
      20: "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher",
    };
    const { fn } = makeRegisterForAppDetails((appId) => ({ strShortcutExe: exeByAppId[appId] ?? "" }));
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: fn } });
    vi.stubGlobal("collectionStore", {
      deckDesktopApps: {
        apps: new Map([
          [10, {}],
          [20, {}],
        ]),
      },
    });
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ "10": 101, "20": 202 });

    const result = await getExistingRomMShortcuts();
    expect(result.get(101)).toBe(10);
    expect(result.get(202)).toBe(20);
    expect(result.size).toBe(2);
  });

  it("excludes shortcuts whose exe is not our rom-launcher", async () => {
    const exeByAppId: Record<number, string> = {
      10: "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher",
      30: "/usr/bin/some-other-game",
    };
    const { fn } = makeRegisterForAppDetails((appId) => ({ strShortcutExe: exeByAppId[appId] ?? "" }));
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: fn } });
    vi.stubGlobal("collectionStore", {
      deckDesktopApps: {
        apps: new Map([
          [10, {}],
          [30, {}],
        ]),
      },
    });
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ "10": 101, "30": 303 });

    const result = await getExistingRomMShortcuts();
    expect(result.get(101)).toBe(10);
    expect(result.has(303)).toBe(false);
    expect(result.size).toBe(1);
  });

  it("excludes our-exe appIds absent from the backend map (orphans after DB reset)", async () => {
    const exe = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";
    const { fn } = makeRegisterForAppDetails(() => ({ strShortcutExe: exe }));
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: fn } });
    vi.stubGlobal("collectionStore", {
      deckDesktopApps: {
        apps: new Map([
          [10, {}],
          [20, {}],
        ]),
      },
    });
    // Backend map empty (DB reset) — our shortcuts are detected by exe but unmapped.
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({});

    const result = await getExistingRomMShortcuts();
    expect(result.size).toBe(0);
  });

  it("returns empty and logs when the backend map fetch rejects", async () => {
    const exe = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";
    const { fn } = makeRegisterForAppDetails(() => ({ strShortcutExe: exe }));
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: fn } });
    vi.stubGlobal("collectionStore", { deckDesktopApps: { apps: new Map([[10, {}]]) } });
    vi.mocked(backend.getAppIdRomIdMap).mockRejectedValue(new Error("network down"));
    const logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});

    const result = await getExistingRomMShortcuts();
    expect(result.size).toBe(0);
    // Non-vacuous: the catch path produces the empty map AND surfaces the failure.
    expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("network down"));
  });

  it("returns empty when there are no desktop apps", async () => {
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: vi.fn() } });
    vi.stubGlobal("collectionStore", { deckDesktopApps: undefined });
    const result = await getExistingRomMShortcuts();
    expect(result.size).toBe(0);
  });

  it("emits a heartbeat when the scan crosses the 10s window across batches", async () => {
    // Two full batches (CONCURRENCY=10 → 20 appIds across two iterations).
    const apps = new Map<number, object>();
    for (let appId = 1; appId <= 20; appId++) apps.set(appId, {});
    const exe = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";
    const { fn } = makeRegisterForAppDetails(() => ({ strShortcutExe: exe }));
    vi.stubGlobal("SteamClient", { Apps: { RegisterForAppDetails: fn } });
    vi.stubGlobal("collectionStore", { deckDesktopApps: { apps } });
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({});

    // Drive Date.now() so the elapsed-since-last-heartbeat check trips once the
    // first batch completes. The loop seeds lastHeartbeat at the first call;
    // every later call returns a value > 10s past it.
    const base = 1_000_000;
    let calls = 0;
    const nowSpy = vi.spyOn(Date, "now").mockImplementation(() => {
      calls += 1;
      // First read seeds lastHeartbeat; subsequent reads are 11s later.
      return calls <= 1 ? base : base + 11_000;
    });

    await getExistingRomMShortcuts();

    // Non-vacuous: crossing the window fires the fire-and-forget heartbeat.
    expect(vi.mocked(backend.syncHeartbeat)).toHaveBeenCalled();
    nowSpy.mockRestore();
  });
});

describe("addShortcut — overview-readiness poll + empty-launch-options skip", () => {
  const EXE = "/home/deck/homebrew/plugins/decky-romm-sync/bin/rom-launcher";

  function item(launchOptions: string): SyncAddItem {
    return {
      rom_id: 1,
      name: "Test ROM",
      exe: EXE,
      start_dir: "/home/deck",
      launch_options: launchOptions,
      platform_name: "PSX",
      cover_path: "",
    };
  }

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("sets shortcut properties as soon as the overview appears (no fixed 500ms wait)", async () => {
    vi.useFakeTimers();
    const setName = vi.fn();
    // Overview is absent on the first poll, present on the second — the poll,
    // not a fixed delay, gates the Set* calls.
    let overviewCalls = 0;
    const getOverview = vi.fn(() => (++overviewCalls >= 2 ? ({ appid: 4242 } as SteamAppOverview) : null));
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: getOverview, allApps: [] });
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn().mockResolvedValue(4242),
        SetShortcutName: setName,
        SetShortcutExe: vi.fn(),
        SetShortcutStartDir: vi.fn(),
        SetAppLaunchOptions: vi.fn(),
        RegisterForAppDetails: vi.fn(() => ({ unregister: vi.fn() })),
      },
    });

    const promise = addShortcut(item(""));
    // Advance one poll interval so the second (truthy) overview check runs.
    await vi.advanceTimersByTimeAsync(100);
    const appId = await promise;

    expect(appId).toBe(4242);
    // The overview was polled exactly twice; Set* fired after the second check.
    expect(getOverview).toHaveBeenCalledTimes(2);
    expect(setName).toHaveBeenCalledWith(4242, "Test ROM");
  });

  it("proceeds with Set* (and logs) after the readiness timeout when the overview never appears", async () => {
    vi.useFakeTimers();
    const setName = vi.fn();
    const getOverview = vi.fn(() => null); // never ready
    const logInfoSpy = vi.spyOn(backend, "logInfo").mockImplementation(() => {});
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: getOverview, allApps: [] });
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn().mockResolvedValue(7),
        SetShortcutName: setName,
        SetShortcutExe: vi.fn(),
        SetShortcutStartDir: vi.fn(),
        SetAppLaunchOptions: vi.fn(),
        RegisterForAppDetails: vi.fn(() => ({ unregister: vi.fn() })),
      },
    });

    const promise = addShortcut(item(""));
    // Exhaust the 1000ms readiness budget.
    await vi.advanceTimersByTimeAsync(1000);
    const appId = await promise;

    expect(appId).toBe(7);
    // Non-vacuous: the timeout path both proceeds (Set* fired) and logs.
    expect(setName).toHaveBeenCalledWith(7, "Test ROM");
    expect(logInfoSpy).toHaveBeenCalledWith(expect.stringContaining("not ready"));
  });

  it("skips SetAppLaunchOptions and the confirm poll for an empty launch_options (uninstalled ROM)", async () => {
    const setLaunchOptions = vi.fn();
    const registerForAppDetails = vi.fn(() => ({ unregister: vi.fn() }));
    // Overview ready immediately, so no timers are involved.
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: vi.fn(() => ({ appid: 9 }) as SteamAppOverview), allApps: [] });
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn().mockResolvedValue(9),
        SetShortcutName: vi.fn(),
        SetShortcutExe: vi.fn(),
        SetShortcutStartDir: vi.fn(),
        SetAppLaunchOptions: setLaunchOptions,
        RegisterForAppDetails: registerForAppDetails,
      },
    });

    const appId = await addShortcut(item(""));

    expect(appId).toBe(9);
    // Nothing to write or confirm for an empty command — both are skipped, so
    // the fat-AppDetails-cache hit of the confirm poll is avoided.
    expect(setLaunchOptions).not.toHaveBeenCalled();
    expect(registerForAppDetails).not.toHaveBeenCalled();
  });

  it("takes the confirmed-write path for a non-empty launch_options (installed ROM)", async () => {
    const cmd = 'flatpak run net.retrodeck.retrodeck "/games/x.bin"';
    const setLaunchOptions = vi.fn();
    // RegisterForAppDetails reports the written value back → the confirm matches.
    const registerForAppDetails = vi.fn((_appId: number, cb: (d: SteamAppDetails | undefined) => void) => {
      queueMicrotask(() => cb({ strLaunchOptions: cmd }));
      return { unregister: vi.fn() };
    });
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: vi.fn(() => ({ appid: 55 }) as SteamAppOverview), allApps: [] });
    vi.stubGlobal("SteamClient", {
      Apps: {
        AddShortcut: vi.fn().mockResolvedValue(55),
        SetShortcutName: vi.fn(),
        SetShortcutExe: vi.fn(),
        SetShortcutStartDir: vi.fn(),
        SetAppLaunchOptions: setLaunchOptions,
        RegisterForAppDetails: registerForAppDetails,
      },
    });

    const appId = await addShortcut(item(cmd));

    expect(appId).toBe(55);
    // Confirmed-write path unchanged: SetAppLaunchOptions fired AND the read-back
    // was polled via RegisterForAppDetails.
    expect(setLaunchOptions).toHaveBeenCalledWith(55, cmd);
    expect(registerForAppDetails).toHaveBeenCalledWith(55, expect.any(Function));
  });
});
