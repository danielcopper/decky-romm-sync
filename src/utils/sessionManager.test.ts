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
