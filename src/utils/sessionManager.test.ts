import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as backend from "../api/backend";
import { initSessionManager, destroySessionManager } from "./sessionManager";

// sessionManager talks to the backend callable surface and the migration
// stores. Mock both so the test observes only what `handleGameStop` forwards
// to `finalizeGameSession`.
vi.mock("../api/backend", () => ({
  recordSessionStart: vi.fn().mockResolvedValue({ success: true }),
  getAppIdRomIdMap: vi.fn(),
  finalizeGameSession: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

vi.mock("./migrationStore", () => ({ setMigrationStatus: vi.fn() }));
vi.mock("./saveSortMigrationStore", () => ({ setSaveSortMigrationStatus: vi.fn() }));
vi.mock("../patches/metadataPatches", () => ({ updatePlaytimeDisplay: vi.fn() }));

type LifetimeUpdate = { bRunning: boolean; unAppID: number };
type LifetimeCb = (update: LifetimeUpdate) => void;

// The map binds Steam app id 100 → RomM rom id 7.
const APP_ID = 100;
const ROM_ID = 7;

function captureLifetimeCb(): LifetimeCb {
  const calls = vi.mocked(SteamClient.GameSessions.RegisterForAppLifetimeNotifications).mock.calls;
  const cb = calls[calls.length - 1]?.[0];
  if (!cb) throw new Error("RegisterForAppLifetimeNotifications was not called");
  return cb as LifetimeCb;
}

function captureSuspendCb(): () => void {
  const calls = vi.mocked(SteamClient.System.RegisterForOnSuspendRequest).mock.calls;
  const cb = calls[calls.length - 1]?.[0];
  if (!cb) throw new Error("RegisterForOnSuspendRequest was not called");
  return cb as () => void;
}

function captureResumeCb(): () => void {
  const calls = vi.mocked(SteamClient.System.RegisterForOnResumeFromSuspend).mock.calls;
  const cb = calls[calls.length - 1]?.[0];
  if (!cb) throw new Error("RegisterForOnResumeFromSuspend was not called");
  return cb as () => void;
}

/** Drive a game-start notification through the serialized lifecycle chain. */
async function startGame(cb: LifetimeCb): Promise<void> {
  cb({ bRunning: true, unAppID: APP_ID });
  // handleGameStart is gated behind a delay(500) inside the lifecycle chain.
  await vi.advanceTimersByTimeAsync(500);
}

/** Drive a game-stop notification and flush the chain. */
async function stopGame(cb: LifetimeCb): Promise<void> {
  cb({ bRunning: false, unAppID: APP_ID });
  await vi.advanceTimersByTimeAsync(0);
}

describe("sessionManager suspend accumulator", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    // The global afterEach in test-setup.ts calls vi.unstubAllGlobals(), which
    // wipes the SteamClient stub between tests. Re-stub the lifecycle/suspend
    // surface this module registers against.
    vi.stubGlobal("SteamClient", {
      GameSessions: {
        RegisterForAppLifetimeNotifications: vi.fn(() => ({ unregister: vi.fn() })),
      },
      System: {
        RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
      },
    });
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ [String(APP_ID)]: ROM_ID });
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({
      total_seconds: null,
      sync: {
        offline: false,
        success: true,
        synced: 0,
        conflicts: [],
        toast_title: null,
        toast_body: null,
        conflicts_toast: null,
      },
      migration: null,
    });
  });

  afterEach(() => {
    destroySessionManager();
    vi.useRealTimers();
  });

  it("forwards 0 suspended seconds when the device never suspended", async () => {
    await initSessionManager();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    vi.setSystemTime(120_000); // 2 min of play, no suspend
    await stopGame(lifetime);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 0);
  });

  it("subtracts a single suspend cycle", async () => {
    await initSessionManager();
    const lifetime = captureLifetimeCb();
    const suspend = captureSuspendCb();
    const resume = captureResumeCb();

    await startGame(lifetime);
    vi.setSystemTime(60_000); // play 60s
    suspend();
    vi.setSystemTime(90_000); // suspended for 30s
    resume();
    vi.setSystemTime(120_000); // play another 30s
    await stopGame(lifetime);

    // 30s suspended → rounded to 30.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 30);
  });

  it("accumulates across multiple suspend cycles", async () => {
    await initSessionManager();
    const lifetime = captureLifetimeCb();
    const suspend = captureSuspendCb();
    const resume = captureResumeCb();

    await startGame(lifetime);
    // Cycle 1: suspend for 10s.
    vi.setSystemTime(10_000);
    suspend();
    vi.setSystemTime(20_000);
    resume();
    // Cycle 2: suspend for 25s.
    vi.setSystemTime(30_000);
    suspend();
    vi.setSystemTime(55_000);
    resume();
    vi.setSystemTime(60_000);
    await stopGame(lifetime);

    // 10s + 25s = 35s.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 35);
  });

  it("folds an in-flight suspend at stop (stopped while suspended)", async () => {
    await initSessionManager();
    const lifetime = captureLifetimeCb();
    const suspend = captureSuspendCb();

    await startGame(lifetime);
    vi.setSystemTime(40_000); // play 40s
    suspend();
    vi.setSystemTime(100_000); // still suspended 60s at stop, no resume
    await stopGame(lifetime);

    // In-flight suspend (60s) is folded in even without a resume event.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 60);
  });

  it("resets the accumulator on the next session start", async () => {
    await initSessionManager();
    const lifetime = captureLifetimeCb();
    const suspend = captureSuspendCb();
    const resume = captureResumeCb();

    // Session 1 accrues 30s of suspend.
    await startGame(lifetime);
    vi.setSystemTime(10_000);
    suspend();
    vi.setSystemTime(40_000);
    resume();
    vi.setSystemTime(50_000);
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenLastCalledWith(ROM_ID, 30);

    // Session 2 has no suspend — the accumulator must have reset to 0.
    await startGame(lifetime);
    vi.setSystemTime(80_000);
    await stopGame(lifetime);

    expect(backend.finalizeGameSession).toHaveBeenLastCalledWith(ROM_ID, 0);
  });
});

describe("sessionManager reload adoption", () => {
  const BREADCRUMB_KEY = "decky-romm-sync:active-session";

  const seedBreadcrumb = (crumb: { v: number; appId: number; romId: number; startMs: number; pausedMs: number }) =>
    localStorage.setItem(BREADCRUMB_KEY, JSON.stringify(crumb));

  const readCrumb = (): unknown => {
    const raw = localStorage.getItem(BREADCRUMB_KEY);
    return raw === null ? null : JSON.parse(raw);
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    localStorage.clear();
    // See the suspend-accumulator beforeEach — the global afterEach wipes the
    // SteamClient stub, so re-stub the surface this module registers against.
    vi.stubGlobal("SteamClient", {
      GameSessions: {
        RegisterForAppLifetimeNotifications: vi.fn(() => ({ unregister: vi.fn() })),
      },
      System: {
        RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
      },
    });
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ [String(APP_ID)]: ROM_ID });
    vi.mocked(backend.recordSessionStart).mockResolvedValue({ success: true });
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({
      total_seconds: null,
      sync: {
        offline: false,
        success: true,
        synced: 0,
        conflicts: [],
        toast_title: null,
        toast_body: null,
        conflicts_toast: null,
      },
      migration: null,
    });
  });

  afterEach(() => {
    destroySessionManager();
    vi.useRealTimers();
  });

  it("adopts a matching breadcrumb without re-stamping the marker, then finalizes on stop", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 3_000 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    // Case (a): the durable marker is preserved — no re-stamp.
    expect(backend.recordSessionStart).not.toHaveBeenCalled();

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);

    // Carried pausedMs (3000ms → 3s) folds in; the original rom is finalized.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 3);
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
  });

  it("adopts a running game with no breadcrumb and re-stamps the marker", async () => {
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    // Case (a′): re-stamp exactly once, then attest with a fresh breadcrumb so
    // a later reload adopts via case (a) instead of re-stamping again.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID, pausedMs: 0 });
  });

  it("adopts and re-stamps when the breadcrumb names a different app than the running game", async () => {
    // Stale breadcrumb for a different app; the running game is authoritative.
    seedBreadcrumb({ v: 1, appId: 555, romId: 99, startMs: 5_000, pausedMs: 4_000 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // The stale breadcrumb is overwritten with a fresh one for the live game.
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID, pausedMs: 0 });
  });

  it("clears the breadcrumb and does not adopt when nothing is running", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 0 });
    vi.stubGlobal("Router", { MainRunningApp: null });

    await initSessionManager();

    // Case (b): orphaned — breadcrumb dropped, no adoption, no fabricated end.
    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();

    const lifetime = captureLifetimeCb();
    // A stop with no adopted session is a no-op — nothing to finalize.
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();

    // The next real start → stop still works normally.
    await startGame(lifetime);
    vi.setSystemTime(30_000);
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 0);
  });

  it("does not adopt when a non-RomM app is running", async () => {
    vi.stubGlobal("Router", { MainRunningApp: { appid: 999, display_name: "Other" } });

    await initSessionManager();

    expect(backend.recordSessionStart).not.toHaveBeenCalled();

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
  });

  it("carries the breadcrumb pausedMs forward and keeps accumulating after adoption", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 1_000, pausedMs: 5_000 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();
    const lifetime = captureLifetimeCb();
    const suspend = captureSuspendCb();
    const resume = captureResumeCb();

    // 5s of pre-reload suspend carried in; add a 10s suspend cycle after adopt.
    vi.setSystemTime(20_000);
    suspend();
    vi.setSystemTime(30_000);
    resume();
    vi.setSystemTime(40_000);
    await stopGame(lifetime);

    // 5000ms carried + 10000ms new = 15s.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 15);
  });

  it("leaves no breadcrumb after a normal start then stop", async () => {
    vi.stubGlobal("Router", { MainRunningApp: null });
    await initSessionManager();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    // Breadcrumb written during start.
    expect(readCrumb()).not.toBeNull();

    vi.setSystemTime(60_000);
    await stopGame(lifetime);
    // Cleared on stop — no residue.
    expect(readCrumb()).toBeNull();
  });

  it("degrades to re-stamping when localStorage throws", async () => {
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });
    // happy-dom's localStorage is a Proxy — swap the whole global (restored by
    // the global afterEach's vi.unstubAllGlobals) rather than spying a method.
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("storage blocked");
      },
      setItem: () => {
        throw new Error("storage blocked");
      },
      removeItem: vi.fn(),
      clear: vi.fn(),
    });

    await expect(initSessionManager()).resolves.toBeUndefined();

    // Both read and write throw → no usable breadcrumb, best-effort write
    // swallowed → case (a′): marker re-stamped once, no crash.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
  });

  it("survives a full reload: start, destroy, re-init, stop finalizes the original rom", async () => {
    vi.stubGlobal("Router", { MainRunningApp: null });
    await initSessionManager();
    const lifetime1 = captureLifetimeCb();

    // Start at a non-zero time so the suspend guard (sessionStartTime) stays truthy.
    vi.setSystemTime(1_000);
    await startGame(lifetime1);
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);

    // Plugin reload: destroy wipes in-memory state but leaves the breadcrumb.
    vi.setSystemTime(120_000);
    destroySessionManager();
    expect(readCrumb()).not.toBeNull();

    // Re-init while the game is still running — adopt via the surviving breadcrumb.
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });
    await initSessionManager();
    // Case (a): durable marker preserved — no second record_session_start.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);

    const lifetime2 = captureLifetimeCb();
    vi.setSystemTime(180_000);
    await stopGame(lifetime2);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 0);
  });

  it("treats a wrong-version breadcrumb as unusable and re-stamps (a′)", async () => {
    // A breadcrumb from a future schema (v2) fails isSessionBreadcrumb.
    seedBreadcrumb({ v: 2, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 9_000 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // Overwritten with a fresh v1 breadcrumb; the stale pausedMs=9000 is discarded.
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID, pausedMs: 0 });
  });

  it("treats a non-object breadcrumb JSON as unusable and re-stamps (a′)", async () => {
    // Valid JSON but not an object — isSessionBreadcrumb rejects it.
    localStorage.setItem(BREADCRUMB_KEY, JSON.stringify(42));
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID, pausedMs: 0 });
  });

  it("survives a rejected recordSessionStart during adoption", async () => {
    vi.mocked(backend.recordSessionStart).mockRejectedValueOnce(new Error("network down"));
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    // (a′) awaits recordSessionStart; its rejection is caught, not surfaced.
    await expect(initSessionManager()).resolves.toBeUndefined();
    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("record session start on adoption"));

    // The breadcrumb is written before the failing call, and the session is
    // still adopted — a subsequent stop finalizes the original rom.
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID, pausedMs: 0 });
    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 0);
  });

  it("clears a live breadcrumb when a non-RomM app is in the foreground", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 0 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: 999, display_name: "Other" } });

    await initSessionManager();

    // The RomM game is not the foreground app → its session is orphaned, its
    // breadcrumb dropped, and nothing is adopted or finalized.
    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
  });

  it("swallows a localStorage failure while clearing an orphaned breadcrumb", async () => {
    // Live breadcrumb present, nothing running (case b) → clear is attempted,
    // but removeItem throws. The failure must be contained, not surfaced.
    vi.stubGlobal("localStorage", {
      getItem: () => JSON.stringify({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 0 }),
      setItem: vi.fn(),
      removeItem: () => {
        throw new Error("storage blocked");
      },
      clear: vi.fn(),
    });
    vi.stubGlobal("Router", { MainRunningApp: null });

    await expect(initSessionManager()).resolves.toBeUndefined();

    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("clear session breadcrumb"));
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
  });

  it("contains an unexpected throw in the adoption path", async () => {
    // Steam's running-app accessor faulting must not crash init — the lifecycle
    // chain's .catch logs and lets initialization complete.
    vi.stubGlobal("Router", {
      MainRunningApp: {
        get appid(): number {
          throw new Error("running-app read failed");
        },
        display_name: "Game",
      },
    });

    await expect(initSessionManager()).resolves.toBeUndefined();

    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("Session adoption error"));
  });
});
