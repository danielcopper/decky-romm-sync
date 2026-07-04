import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as backend from "../api/backend";
import { initSessionManager, destroySessionManager, ADOPTION_POLL_MAX_MS } from "./sessionManager";

// sessionManager talks to the backend callable surface and the migration
// stores. Mock both so the test observes only what `handleGameStop` forwards
// to `finalizeGameSession`.
vi.mock("../api/backend", () => ({
  recordSessionStart: vi.fn().mockResolvedValue({ success: true }),
  getAppIdRomIdMap: vi.fn(),
  finalizeGameSession: vi.fn(),
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  debugLog: vi.fn(),
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

// #1148: adoptOrphanedSession now polls the running-app surfaces for up to 15s
// before deciding. When no running app is present, initSessionManager only settles
// once that poll times out — drain it (using the real source-side constant, so a
// change to the poll window can't silently desync this fast-forward) so the
// immediate-read tests still resolve.
async function initDrainingAdoptionPoll(): Promise<void> {
  const init = initSessionManager();
  await vi.advanceTimersByTimeAsync(ADOPTION_POLL_MAX_MS);
  await init;
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
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    vi.setSystemTime(120_000); // 2 min of play, no suspend
    await stopGame(lifetime);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 0);
  });

  it("subtracts a single suspend cycle", async () => {
    await initDrainingAdoptionPoll();
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
    await initDrainingAdoptionPoll();
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
    await initDrainingAdoptionPoll();
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
    await initDrainingAdoptionPoll();
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

  it("stamps suspendedAt once across repeated suspend-progress events", async () => {
    // The renamed suspend hook is a PROGRESS callback that can fire several times
    // per cycle. A repeated fire must NOT re-stamp the pause start, or the
    // subtracted span shrinks (here: 60s→90s = 30s, not 70s→90s = 20s).
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();
    const suspend = captureSuspendCb();
    const resume = captureResumeCb();

    await startGame(lifetime);
    vi.setSystemTime(60_000);
    suspend(); // first progress fire — stamps
    vi.setSystemTime(70_000);
    suspend(); // repeated progress fire — must be ignored
    vi.setSystemTime(90_000);
    resume();
    vi.setSystemTime(120_000);
    await stopGame(lifetime);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 30);
  });

  it("folds a resume once across repeated resume-progress events", async () => {
    // Repeated resume-progress fires in the same cycle must fold only once — the
    // second fire finds no open suspend and is a no-op (not another 60→100 fold).
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();
    const suspend = captureSuspendCb();
    const resume = captureResumeCb();

    await startGame(lifetime);
    vi.setSystemTime(60_000);
    suspend();
    vi.setSystemTime(90_000);
    resume(); // folds 30s, clears the open suspend
    vi.setSystemTime(100_000);
    resume(); // repeated progress fire — must be a no-op
    vi.setSystemTime(120_000);
    await stopGame(lifetime);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 30);
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

    // Nothing ever runs → the poll times out and the breadcrumb is orphan-cleared.
    await initDrainingAdoptionPoll();

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
    await initDrainingAdoptionPoll();
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
    // No game at first load → the adoption poll times out before init settles.
    await initDrainingAdoptionPoll();
    const lifetime1 = captureLifetimeCb();

    // Start after the poll window (times are absolute; the poll drained to 15s).
    vi.setSystemTime(20_000);
    await startGame(lifetime1);
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);

    // Plugin reload: destroy wipes in-memory state but leaves the breadcrumb.
    vi.setSystemTime(120_000);
    destroySessionManager();
    expect(readCrumb()).not.toBeNull();

    // Re-init while the game is still running — the poll sees MainRunningApp
    // immediately and adopts via the surviving breadcrumb.
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

    // Nothing running → the poll times out, then the orphaned-breadcrumb clear
    // hits the throwing removeItem, which must be swallowed.
    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(ADOPTION_POLL_MAX_MS);
    await expect(init).resolves.toBeUndefined();

    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("clear session breadcrumb"));
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
  });

  it("tolerates a throwing running-app getter without erroring adoption", async () => {
    // Steam's running-app accessor faulting must not crash init. The defensive
    // reader catches the throw PER-SOURCE (noting it in diagnostics) so adoption
    // proceeds against the other sources instead of erroring out (#1148 round 2) —
    // the pre-fix single-source read let the throw escape to the adoption .catch.
    vi.stubGlobal("Router", {
      get MainRunningApp(): unknown {
        throw new Error("running-app read failed");
      },
    });

    // Nothing else running → the poll times out and init resolves cleanly.
    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(ADOPTION_POLL_MAX_MS);
    await expect(init).resolves.toBeUndefined();

    // No adoption error surfaced; the timed-out round logged what every candidate
    // reported, including the throwing source.
    expect(backend.logError).not.toHaveBeenCalledWith(expect.stringContaining("Session adoption error"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("threw:"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("no running app"));
  });

  // #1054 follow-up: after a full plugin_loader restart Router.MainRunningApp is
  // null for several seconds while the game is still running, so a one-shot read
  // wrongly orphaned a live session. Adoption now polls MainRunningApp.
  it("adopts a matching breadcrumb once MainRunningApp appears mid-poll, no orphan log", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 3_000 });
    vi.stubGlobal("Router", { MainRunningApp: null });

    const init = initSessionManager();
    // Four polls into the loader-restart window, MainRunningApp is still null.
    await vi.advanceTimersByTimeAsync(2_000);
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    // Steam finally populates the running app; the next poll sees it.
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });
    await vi.advanceTimersByTimeAsync(500);
    await init;

    // Case (a): the matching breadcrumb is adopted without a re-stamp, and the
    // session was never logged as orphaned.
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("orphaned"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("running app appeared"));

    // The adopted session finalizes on stop, carrying the breadcrumb's pausedMs.
    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 3);
  });

  it("orphan-clears the breadcrumb after the poll times out with nothing running", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 0 });
    vi.stubGlobal("Router", { MainRunningApp: null });

    await initDrainingAdoptionPoll();

    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("no running app"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("orphaned"));
  });

  it("stays silent when the poll times out with no breadcrumb", async () => {
    vi.stubGlobal("Router", { MainRunningApp: null });

    await initDrainingAdoptionPoll();

    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    // No breadcrumb to orphan — the timeout is a no-op beyond the poll log.
    expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("orphaned"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("no running app"));
  });

  it("queues a racing stop behind the poll so it finalizes after adoption", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 1_000, pausedMs: 2_000 });
    vi.stubGlobal("Router", { MainRunningApp: null });

    const init = initSessionManager();
    // Let init register the lifetime hook and enter the poll.
    await vi.advanceTimersByTimeAsync(500);
    const lifetime = captureLifetimeCb();

    // A stop arrives WHILE the poll is still running. It queues on the lifecycle
    // chain behind the in-flight adoption task rather than interleaving with it.
    lifetime({ bRunning: false, unAppID: APP_ID });

    // The game becomes visible; the poll resolves and adoption (a) runs first.
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });
    await vi.advanceTimersByTimeAsync(500);
    await init;
    await vi.advanceTimersByTimeAsync(0); // flush the queued stop task

    // Ordering proof: the stop finalized the ADOPTED session (carrying the
    // breadcrumb's 2000ms paused → 2s). Had the stop run before adoption,
    // activeRomId would still be null and handleGameStop a no-op (no finalize).
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 2);
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
  });

  it("aborts an in-flight adoption poll when destroy tears the manager down", async () => {
    // Nothing running yet → the poll is mid-flight when destroy fires. A game that
    // appears AFTER teardown must not be adopted: the aborted poll writes no
    // breadcrumb, records no session, and takes no adoption action (#1148 LOW-1).
    vi.stubGlobal("Router", { MainRunningApp: null });

    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(2_000); // poll running, nothing found yet
    destroySessionManager(); // tears down mid-poll → bumps the epoch
    // A running game appears after teardown; the still-pending poll would adopt it
    // (case a′ → recordSessionStart + breadcrumb) if it did not check the epoch.
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });
    await vi.advanceTimersByTimeAsync(500);
    await init;

    expect(backend.debugLog).toHaveBeenCalledWith("adoption: cancelled by destroy");
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(readCrumb()).toBeNull(); // no breadcrumb written by the aborted adoption
    expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("Adopted"));
    expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("running app appeared"));
  });
});

// #1148: on-device the suspend/resume hooks never fired, so decision C's
// suspend-subtraction shipped dormant. These cover the registration diagnostics
// that a Game-Mode run will surface — the API-missing headline, a throwing
// registration, and the availability probe — plus the happy-path handle report.
describe("sessionManager suspend-hook diagnostics", () => {
  // `system` is intentionally allowed to be undefined so a test can model a
  // SteamClient with no `System` namespace at all (the #1148 crash-proofness case).
  const stubSteamClient = (system: Record<string, unknown> | undefined, user?: Record<string, unknown>) => {
    const client: Record<string, unknown> = {
      GameSessions: {
        RegisterForAppLifetimeNotifications: vi.fn(() => ({ unregister: vi.fn() })),
      },
      System: system,
    };
    if (user) client.User = user;
    vi.stubGlobal("SteamClient", client);
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ [String(APP_ID)]: ROM_ID });
    vi.mocked(backend.recordSessionStart).mockResolvedValue({ success: true });
    // A running (non-RomM) app so the #1148 adoption poll returns on its first read
    // and init settles under real timers — these tests exercise hook registration,
    // not adoption, so the app is inert background (999 is not in the rom map).
    vi.stubGlobal("Router", { MainRunningApp: { appid: 999, display_name: "background" } });
  });

  afterEach(() => {
    destroySessionManager();
  });

  it("warns at headline level when both suspend/resume members are missing, and init still completes", async () => {
    stubSteamClient({}); // System exposes neither Register* member

    await expect(initSessionManager()).resolves.toBeUndefined();

    // Actionable headline names both absent members (booleans false).
    expect(backend.logWarn).toHaveBeenCalledWith(
      "Suspend/resume hooks missing on this build: legacy=false/false user=false/false",
    );
    // Never attempted registration → no throw path was taken.
    expect(backend.logWarn).not.toHaveBeenCalledWith(expect.stringContaining("registration threw"));
    // No hooks were registered, so teardown must not throw on null handles.
    expect(() => destroySessionManager()).not.toThrow();
  });

  it("warns only about the missing member when suspend exists but resume does not", async () => {
    stubSteamClient({ RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: vi.fn() })) });

    await expect(initSessionManager()).resolves.toBeUndefined();

    expect(backend.logWarn).toHaveBeenCalledWith(
      "Suspend/resume hooks missing on this build: legacy=true/false user=false/false",
    );
  });

  it("names the partially-present User.* pair in the missing-hooks headline", async () => {
    // Legacy gone and only ONE User.* successor present → no usable surface, but
    // the headline must show the partial User pair so the gap is visible at the
    // default log level (#1148 LOW-2).
    stubSteamClient(
      {}, // no legacy pair
      { RegisterForPrepareForSystemSuspendProgress: vi.fn(() => ({ unregister: vi.fn() })) }, // resume successor absent
    );

    await expect(initSessionManager()).resolves.toBeUndefined();

    expect(backend.logWarn).toHaveBeenCalledWith(
      "Suspend/resume hooks missing on this build: legacy=false/false user=true/false",
    );
  });

  it("catches and warns when registration throws, and init still completes", async () => {
    stubSteamClient({
      RegisterForOnSuspendRequest: vi.fn(() => {
        throw new Error("boom");
      }),
      RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
    });

    await expect(initSessionManager()).resolves.toBeUndefined();

    // Members present → no "missing" headline; the throw is caught and warned.
    expect(backend.logWarn).not.toHaveBeenCalledWith(expect.stringContaining("hooks missing"));
    expect(backend.logWarn).toHaveBeenCalledWith(expect.stringContaining("Suspend/resume registration threw"));
    expect(backend.logWarn).toHaveBeenCalledWith(expect.stringContaining("boom"));
  });

  it("debug-logs the handle shape on a clean registration", async () => {
    stubSteamClient({
      RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: vi.fn() })),
      RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
    });

    await initSessionManager();

    // No headline warning on a healthy build; the debug tier names the surface
    // used and reports both handles carry an `unregister` function.
    expect(backend.logWarn).not.toHaveBeenCalled();
    expect(backend.debugLog).toHaveBeenCalledWith(
      "Suspend/resume registration [System.RegisterForOnSuspendRequest/RegisterForOnResumeFromSuspend]: suspend={unregister:fn} resume={unregister:fn}",
    );
  });

  it("probes SteamClient for suspend/resume/sleep/wake members and excludes unrelated ones", async () => {
    stubSteamClient(
      {
        RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForOnResumeFromSleep: vi.fn(), // matches /sleep/ — a candidate alternative
        GetSystemInfo: vi.fn(), // unrelated — must be excluded
      },
      {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(), // matches /suspend/ under User
        RegisterForShutdownStart: vi.fn(), // unrelated — must be excluded
      },
    );

    await initSessionManager();

    const probeCall = vi
      .mocked(backend.debugLog)
      .mock.calls.find((c) => String(c[0]).startsWith("SteamClient suspend/resume surface:"));
    expect(probeCall).toBeDefined();
    const surface = String(probeCall?.[0]);
    expect(surface).toContain("System.RegisterForOnSuspendRequest");
    expect(surface).toContain("System.RegisterForOnResumeFromSuspend");
    expect(surface).toContain("System.RegisterForOnResumeFromSleep");
    expect(surface).toContain("User.RegisterForPrepareForSystemSuspendProgress");
    expect(surface).not.toContain("GetSystemInfo");
    expect(surface).not.toContain("RegisterForShutdownStart");
  });

  it("survives an absent SteamClient.System: warns, still runs adoption, no crash", async () => {
    stubSteamClient(undefined); // SteamClient has no `System` namespace at all
    // A running RomM game with no breadcrumb → adoption case (a′) records a
    // session. That only fires if init got PAST the presence read without
    // throwing — the guard this test pins. (On the pre-fix commit the presence
    // read `typeof SteamClient.System.RegisterForOnSuspendRequest` throws a
    // TypeError that escapes init, so none of the asserts below hold.)
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await expect(initSessionManager()).resolves.toBeUndefined();

    // System absent → both members read as false → headline warns.
    expect(backend.logWarn).toHaveBeenCalledWith(
      "Suspend/resume hooks missing on this build: legacy=false/false user=false/false",
    );
    // The surface probe still emits (init reached it, never threw).
    expect(backend.debugLog).toHaveBeenCalledWith("SteamClient suspend/resume surface: (none)");
    // Adoption still ran — init did not abort before it.
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // Nothing registered → teardown must not throw on null handles.
    expect(() => destroySessionManager()).not.toThrow();
  });

  it("describes an undefined registration handle by type, not as a thrown registration", async () => {
    // A build where the member exists but returns undefined instead of a handle.
    stubSteamClient({
      RegisterForOnSuspendRequest: vi.fn(() => undefined),
      RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
    });

    await expect(initSessionManager()).resolves.toBeUndefined();

    // describeHandle must classify the undefined handle by type rather than
    // dereferencing it — otherwise the throw is misattributed as "registration
    // threw" (the pre-fix behavior this test pins).
    expect(backend.logWarn).not.toHaveBeenCalledWith(expect.stringContaining("registration threw"));
    expect(backend.debugLog).toHaveBeenCalledWith(
      "Suspend/resume registration [System.RegisterForOnSuspendRequest/RegisterForOnResumeFromSuspend]: suspend=type=undefined resume={unregister:fn}",
    );
  });

  it("falls back to the User.* surface when the legacy System hooks are absent", async () => {
    // Current SteamOS: System.RegisterForOn* are gone; only the renamed User.*
    // progress hooks remain. Registration must fall through to them.
    stubSteamClient(
      {}, // System exposes neither legacy Register* member
      {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForResumeSuspendedGamesProgress: vi.fn(() => ({ unregister: vi.fn() })),
      },
    );

    await initSessionManager();

    // A working surface was found → no "hooks missing" headline; the debug line
    // names the User surface that was used.
    expect(backend.logWarn).not.toHaveBeenCalledWith(expect.stringContaining("hooks missing"));
    expect(backend.debugLog).toHaveBeenCalledWith(
      "Suspend/resume registration [User.RegisterForPrepareForSystemSuspendProgress/RegisterForResumeSuspendedGamesProgress]: suspend={unregister:fn} resume={unregister:fn}",
    );
  });

  it("prefers the legacy System surface when both surfaces are present", async () => {
    // A build that still exposes the legacy pair keeps using it (it works), even
    // when the User.* successors are also present.
    stubSteamClient(
      {
        RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
      },
      {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForResumeSuspendedGamesProgress: vi.fn(() => ({ unregister: vi.fn() })),
      },
    );

    await initSessionManager();

    expect(backend.logWarn).not.toHaveBeenCalled();
    expect(backend.debugLog).toHaveBeenCalledWith(
      "Suspend/resume registration [System.RegisterForOnSuspendRequest/RegisterForOnResumeFromSuspend]: suspend={unregister:fn} resume={unregister:fn}",
    );
  });

  // #1148 round 2: the renamed User.* members EXIST (typeof function) but throw
  // "Unknown method" when INVOKED — the Steam bridge doesn't back them in our CEF
  // context. A single try/catch that gives up on the first throw is wrong: the
  // registration is a candidate CHAIN that logs the throw, rolls back a
  // half-registered handle, and tries the next surface.
  it("falls through to the next surface when the first surface's registration throws", async () => {
    stubSteamClient(
      {
        RegisterForOnSuspendRequest: vi.fn(() => {
          throw new Error("Unknown method");
        }),
        RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
      },
      {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForResumeSuspendedGamesProgress: vi.fn(() => ({ unregister: vi.fn() })),
      },
    );

    await initSessionManager();

    // Legacy threw on invocation → warned with the surface + exact error, then the
    // User surface registered cleanly.
    expect(backend.logWarn).toHaveBeenCalledWith(
      expect.stringContaining(
        "registration threw on [System.RegisterForOnSuspendRequest/RegisterForOnResumeFromSuspend]",
      ),
    );
    expect(backend.logWarn).toHaveBeenCalledWith(expect.stringContaining("Unknown method"));
    expect(backend.debugLog).toHaveBeenCalledWith(
      "Suspend/resume registration [User.RegisterForPrepareForSystemSuspendProgress/RegisterForResumeSuspendedGamesProgress]: suspend={unregister:fn} resume={unregister:fn}",
    );
    // A working surface was found → neither the members-missing nor the all-failed
    // headline fires.
    expect(backend.logWarn).not.toHaveBeenCalledWith(expect.stringContaining("hooks missing"));
    expect(backend.logWarn).not.toHaveBeenCalledWith(expect.stringContaining("failed on all"));
  });

  it("rolls back a half-registered suspend hook when the surface's resume throws, then uses the next", async () => {
    const legacySuspendUnreg = vi.fn();
    stubSteamClient(
      {
        RegisterForOnSuspendRequest: vi.fn(() => ({ unregister: legacySuspendUnreg })),
        RegisterForOnResumeFromSuspend: vi.fn(() => {
          throw new Error("Unknown method");
        }),
      },
      {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForResumeSuspendedGamesProgress: vi.fn(() => ({ unregister: vi.fn() })),
      },
    );

    await initSessionManager();

    // The legacy suspend registered but its resume threw → the dangling suspend
    // handle is unregistered (no leaked handler on the abandoned surface), and
    // registration falls through to the User surface.
    expect(legacySuspendUnreg).toHaveBeenCalledTimes(1);
    expect(backend.debugLog).toHaveBeenCalledWith(
      "Suspend/resume registration [User.RegisterForPrepareForSystemSuspendProgress/RegisterForResumeSuspendedGamesProgress]: suspend={unregister:fn} resume={unregister:fn}",
    );
  });

  it("swallows a throwing rollback unregister and still uses the next surface", async () => {
    stubSteamClient(
      {
        RegisterForOnSuspendRequest: vi.fn(() => ({
          unregister: () => {
            throw new Error("unregister boom");
          },
        })),
        RegisterForOnResumeFromSuspend: vi.fn(() => {
          throw new Error("Unknown method");
        }),
      },
      {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(() => ({ unregister: vi.fn() })),
        RegisterForResumeSuspendedGamesProgress: vi.fn(() => ({ unregister: vi.fn() })),
      },
    );

    await expect(initSessionManager()).resolves.toBeUndefined();

    // The rollback unregister threw but was swallowed → registration still reached
    // and used the User surface without crashing init.
    expect(backend.debugLog).toHaveBeenCalledWith(
      "Suspend/resume registration [User.RegisterForPrepareForSystemSuspendProgress/RegisterForResumeSuspendedGamesProgress]: suspend={unregister:fn} resume={unregister:fn}",
    );
  });

  it("warns that every candidate surface failed when all registrations throw", async () => {
    stubSteamClient(
      {
        RegisterForOnSuspendRequest: vi.fn(() => {
          throw new Error("legacy boom");
        }),
        RegisterForOnResumeFromSuspend: vi.fn(() => ({ unregister: vi.fn() })),
      },
      {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(() => {
          throw new Error("user boom");
        }),
        RegisterForResumeSuspendedGamesProgress: vi.fn(() => ({ unregister: vi.fn() })),
      },
    );

    await expect(initSessionManager()).resolves.toBeUndefined();

    // Both candidates threw → each is warned individually, then a distinct
    // all-failed headline (NOT the members-missing one — the members were present).
    expect(backend.logWarn).toHaveBeenCalledWith(
      expect.stringContaining(
        "registration threw on [System.RegisterForOnSuspendRequest/RegisterForOnResumeFromSuspend]",
      ),
    );
    expect(backend.logWarn).toHaveBeenCalledWith(
      expect.stringContaining(
        "registration threw on [User.RegisterForPrepareForSystemSuspendProgress/RegisterForResumeSuspendedGamesProgress]",
      ),
    );
    expect(backend.logWarn).toHaveBeenCalledWith(
      expect.stringContaining("registration failed on all 2 candidate surface(s)"),
    );
    expect(backend.logWarn).not.toHaveBeenCalledWith(expect.stringContaining("hooks missing"));
  });
});

// #1148: the renamed User.* progress hooks must drive the same suspend accumulator
// as the legacy System.* pair. These exercise registration + subtraction
// end-to-end through the fallback surface, including idempotency under the
// repeated progress fires the User.* callbacks emit per suspend/resume cycle.
describe("sessionManager suspend subtraction via the User.* fallback (#1148)", () => {
  let userSuspend: (() => void) | undefined;
  let userResume: (() => void) | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    userSuspend = undefined;
    userResume = undefined;
    vi.stubGlobal("SteamClient", {
      GameSessions: {
        RegisterForAppLifetimeNotifications: vi.fn(() => ({ unregister: vi.fn() })),
      },
      System: {}, // legacy System.RegisterForOn* removed on current SteamOS
      User: {
        RegisterForPrepareForSystemSuspendProgress: vi.fn((cb: () => void) => {
          userSuspend = cb;
          return { unregister: vi.fn() };
        }),
        RegisterForResumeSuspendedGamesProgress: vi.fn((cb: () => void) => {
          userResume = cb;
          return { unregister: vi.fn() };
        }),
      },
    });
    // No game at load → the adoption poll drains before init settles; startGame
    // then drives the session via unAppID (Router.MainRunningApp stays null).
    vi.stubGlobal("Router", { MainRunningApp: null });
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

  it("captures the User.* callbacks and subtracts a suspend cycle delivered through them", async () => {
    await initDrainingAdoptionPoll();
    expect(userSuspend).toBeTypeOf("function");
    expect(userResume).toBeTypeOf("function");

    const lifetime = captureLifetimeCb();
    await startGame(lifetime);
    vi.setSystemTime(60_000);
    userSuspend!();
    vi.setSystemTime(90_000);
    userResume!();
    vi.setSystemTime(120_000);
    await stopGame(lifetime);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 30);
  });

  it("does not double-count repeated User.* suspend/resume progress fires", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();
    await startGame(lifetime);

    vi.setSystemTime(60_000);
    userSuspend!(); // first suspend-progress — stamps
    vi.setSystemTime(70_000);
    userSuspend!(); // repeated progress — ignored
    vi.setSystemTime(90_000);
    userResume!(); // folds 30s, clears the open suspend
    vi.setSystemTime(100_000);
    userResume!(); // repeated progress — no-op
    vi.setSystemTime(120_000);
    await stopGame(lifetime);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID, 30);
  });

  it("unregisters the User.* handles on destroy", async () => {
    const suspendUnregister = vi.fn();
    const resumeUnregister = vi.fn();
    vi.stubGlobal("SteamClient", {
      GameSessions: { RegisterForAppLifetimeNotifications: vi.fn(() => ({ unregister: vi.fn() })) },
      System: {},
      User: {
        RegisterForPrepareForSystemSuspendProgress: vi.fn(() => ({ unregister: suspendUnregister })),
        RegisterForResumeSuspendedGamesProgress: vi.fn(() => ({ unregister: resumeUnregister })),
      },
    });

    await initDrainingAdoptionPoll();
    destroySessionManager();

    expect(suspendUnregister).toHaveBeenCalledTimes(1);
    expect(resumeUnregister).toHaveBeenCalledTimes(1);
  });
});
