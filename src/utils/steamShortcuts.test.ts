import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as backend from "../api/backend";
import { getExistingRomMShortcuts, setLaunchOptionsConfirmed } from "./steamShortcuts";

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
