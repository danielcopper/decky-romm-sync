import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
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

const IDLE_FINALIZE = {
  total_seconds: null,
  sync: {
    offline: false,
    success: true,
    synced: 0,
    uploaded: 0,
    downloaded: 0,
    conflicts: [],
    failure_toast: null,
    conflicts_toast: null,
  },
  migration: null,
};

function captureLifetimeCb(): LifetimeCb {
  const calls = vi.mocked(SteamClient.GameSessions.RegisterForAppLifetimeNotifications).mock.calls;
  const cb = calls[calls.length - 1]?.[0];
  if (!cb) throw new Error("RegisterForAppLifetimeNotifications was not called");
  return cb as LifetimeCb;
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

// The session manager registers only the lifecycle hook — suspend/resume
// tracking was removed (#1148: the Steam hooks never fired on-device; playtime
// now excludes suspend via the backend monotonic clock). Re-stub after the
// global afterEach's vi.unstubAllGlobals wipes SteamClient.
function stubLifecycleSteamClient(): void {
  vi.stubGlobal("SteamClient", {
    GameSessions: {
      RegisterForAppLifetimeNotifications: vi.fn(() => ({ unregister: vi.fn() })),
    },
  });
}

describe("sessionManager lifecycle forwarding", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    stubLifecycleSteamClient();
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ [String(APP_ID)]: ROM_ID });
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({ ...IDLE_FINALIZE });
  });

  afterEach(() => {
    destroySessionManager();
    vi.useRealTimers();
  });

  it("records the start and finalizes the session on stop", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    vi.setSystemTime(120_000); // 2 min of play
    await stopGame(lifetime);

    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // Suspend accounting is a backend concern now — the frontend forwards no
    // suspend duration.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("updates the playtime display when finalize returns a total", async () => {
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({ ...IDLE_FINALIZE, total_seconds: 42 });
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    await stopGame(lifetime);

    // appStore mutation stays frontend — the appId is resolved from the map.
    expect(updatePlaytimeDisplay).toHaveBeenCalledWith(APP_ID, 42);
  });

  it("does not track a non-RomM app", async () => {
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({}); // APP_ID unmapped
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    await stopGame(lifetime);

    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
  });

  it("dispatches romm_data_changed on stop even when the post-exit sync failed (#1334)", async () => {
    // A failed post-exit sync must still refresh open surfaces so the panel
    // drops any stale green "synced" for a file now pending upload.
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({
      total_seconds: null,
      sync: {
        offline: false,
        success: false,
        synced: 0,
        uploaded: 0,
        downloaded: 0,
        conflicts: [],
        failure_toast: "Access denied — your account lacks permissions for this action",
        conflicts_toast: null,
      },
      migration: null,
    });
    const dataChanged: unknown[] = [];
    const listener = (e: Event) => dataChanged.push((e as CustomEvent).detail);
    globalThis.addEventListener("romm_data_changed", listener);
    try {
      await initDrainingAdoptionPoll();
      const lifetime = captureLifetimeCb();

      await startGame(lifetime);
      await stopGame(lifetime);

      expect(dataChanged).toContainEqual({ type: "save_sync", rom_id: ROM_ID });
    } finally {
      globalThis.removeEventListener("romm_data_changed", listener);
    }
  });
});

describe("sessionManager post-exit save-sync toast (#1481)", () => {
  // The directional completion toast is rendered frontend-side from the transfer
  // counts via `saveSyncToastBody`; the offline/failure body stays backend-owned
  // (`failure_toast`). These pin both surfaces onto the finalize payload.
  type SyncOverride = Partial<Awaited<ReturnType<typeof backend.finalizeGameSession>>["sync"]>;

  const finalizeWithSync = (override: SyncOverride) =>
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({
      ...IDLE_FINALIZE,
      sync: { ...IDLE_FINALIZE.sync, ...override },
    });

  const runStop = async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();
    await startGame(lifetime);
    await stopGame(lifetime);
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    stubLifecycleSteamClient();
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ [String(APP_ID)]: ROM_ID });
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({ ...IDLE_FINALIZE });
  });

  afterEach(() => {
    destroySessionManager();
    vi.useRealTimers();
  });

  it("renders the upload-only directional toast from the counts", async () => {
    finalizeWithSync({ success: true, uploaded: 2, downloaded: 0 });
    await runStop();
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "RomM Save Sync", body: "Saves uploaded to RomM" }),
    );
  });

  it("renders the both-directions directional toast from the counts", async () => {
    finalizeWithSync({ success: true, uploaded: 1, downloaded: 2 });
    await runStop();
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: "Saves synced with RomM (1 up, 2 down)" }),
    );
  });

  it("renders the backend-owned failure_toast when the sync failed", async () => {
    finalizeWithSync({ success: false, uploaded: 0, downloaded: 0, failure_toast: "Failed to sync saves after exit" });
    await runStop();
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ title: "RomM Save Sync", body: "Failed to sync saves after exit" }),
    );
  });

  it("fires no toast when nothing moved and there was no failure (zero-case)", async () => {
    // IDLE_FINALIZE is success=true with zero counts and no failure_toast.
    finalizeWithSync({ success: true, uploaded: 0, downloaded: 0, failure_toast: null });
    await runStop();
    expect(vi.mocked(toaster.toast)).not.toHaveBeenCalled();
  });

  it("does not render the failure_toast on a successful transfer (mutual exclusivity)", async () => {
    // A success result carries failure_toast=null by construction; even if a
    // stray body slipped through, the directional path wins on success.
    finalizeWithSync({ success: true, uploaded: 3, downloaded: 0, failure_toast: null });
    await runStop();
    const bodies = vi.mocked(toaster.toast).mock.calls.map((c) => c[0].body);
    expect(bodies).toContain("Saves uploaded to RomM");
    expect(bodies).not.toContain("Failed to sync saves after exit");
  });

  it("fires the additive conflicts toast alongside the directional toast", async () => {
    finalizeWithSync({
      success: true,
      uploaded: 1,
      downloaded: 0,
      conflicts_toast: "2 save conflicts need resolution",
    });
    await runStop();
    const bodies = vi.mocked(toaster.toast).mock.calls.map((c) => c[0].body);
    expect(bodies).toContain("Saves uploaded to RomM");
    expect(bodies).toContain("2 save conflicts need resolution");
  });
});

describe("sessionManager reload adoption", () => {
  const BREADCRUMB_KEY = "decky-romm-sync:active-session";

  const seedBreadcrumb = (crumb: { v: number; appId: number; romId: number; startMs: number }) =>
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
    stubLifecycleSteamClient();
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({ [String(APP_ID)]: ROM_ID });
    vi.mocked(backend.recordSessionStart).mockResolvedValue({ success: true });
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({ ...IDLE_FINALIZE });
  });

  afterEach(() => {
    destroySessionManager();
    vi.useRealTimers();
  });

  it("adopts a matching breadcrumb without re-stamping the marker, then finalizes on stop", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    // Case (a): the durable marker is preserved — no re-stamp.
    expect(backend.recordSessionStart).not.toHaveBeenCalled();

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);

    // The original rom is finalized on stop.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
  });

  it("adopts a running game with no breadcrumb and re-stamps the marker", async () => {
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    // Case (a′): re-stamp exactly once, then attest with a fresh breadcrumb so
    // a later reload adopts via case (a) instead of re-stamping again.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID });
  });

  it("adopts and re-stamps when the breadcrumb names a different app than the running game", async () => {
    // Stale breadcrumb for a different app; the running game is authoritative.
    seedBreadcrumb({ v: 1, appId: 555, romId: 99, startMs: 5_000 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // The stale breadcrumb is overwritten with a fresh one for the live game.
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID });
  });

  it("clears the breadcrumb and does not adopt when nothing is running", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000 });
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
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("does not adopt when a non-RomM app is running", async () => {
    vi.stubGlobal("Router", { MainRunningApp: { appid: 999, display_name: "Other" } });

    await initSessionManager();

    expect(backend.recordSessionStart).not.toHaveBeenCalled();

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
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

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("treats a wrong-version breadcrumb as unusable and re-stamps (a′)", async () => {
    // A breadcrumb from a future schema (v2) fails isSessionBreadcrumb.
    seedBreadcrumb({ v: 2, appId: APP_ID, romId: ROM_ID, startMs: 5_000 });
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // Overwritten with a fresh v1 breadcrumb.
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID });
  });

  it("treats a non-object breadcrumb JSON as unusable and re-stamps (a′)", async () => {
    // Valid JSON but not an object — isSessionBreadcrumb rejects it.
    localStorage.setItem(BREADCRUMB_KEY, JSON.stringify(42));
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID });
  });

  it("survives a rejected recordSessionStart during adoption", async () => {
    vi.mocked(backend.recordSessionStart).mockRejectedValueOnce(new Error("network down"));
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    // (a′) awaits recordSessionStart; its rejection is caught, not surfaced.
    await expect(initSessionManager()).resolves.toBeUndefined();
    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("record session start on adoption"));

    // The breadcrumb is written before the failing call, and the session is
    // still adopted — a subsequent stop finalizes the original rom.
    expect(readCrumb()).toMatchObject({ v: 1, appId: APP_ID, romId: ROM_ID });
    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("clears a live breadcrumb when a non-RomM app is in the foreground", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000 });
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
      getItem: () => JSON.stringify({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000 }),
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
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000 });
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

    // The adopted session finalizes on stop.
    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("orphan-clears the breadcrumb after the poll times out with nothing running", async () => {
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000 });
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
    seedBreadcrumb({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 1_000 });
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

    // Ordering proof: the stop finalized the ADOPTED session. Had the stop run
    // before adoption, activeRomId would still be null and handleGameStop a
    // no-op (no finalize).
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
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
// #1313: the state-aware Resume button reacts to session start/stop without
// polling by listening for the romm_session_changed DOM event. These pin that
// sessionManager dispatches it (running:true on start + reload-adoption,
// running:false on stop) with the appId+romId the button matches on.
describe("sessionManager session-changed dispatch (#1313)", () => {
  const BREADCRUMB_KEY = "decky-romm-sync:active-session";
  let sessionEvents: { running: boolean; appId: number; romId: number }[];
  let sessionListener: (e: WindowEventMap["romm_session_changed"]) => void;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    localStorage.clear();
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
        uploaded: 0,
        downloaded: 0,
        conflicts: [],
        failure_toast: null,
        conflicts_toast: null,
      },
      migration: null,
    });
    sessionEvents = [];
    sessionListener = (e) => sessionEvents.push(e.detail);
    globalThis.addEventListener("romm_session_changed", sessionListener);
  });

  afterEach(() => {
    globalThis.removeEventListener("romm_session_changed", sessionListener);
    destroySessionManager();
    vi.useRealTimers();
  });

  it("dispatches running:true on game start and running:false on stop", async () => {
    vi.stubGlobal("Router", { MainRunningApp: null });
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    expect(sessionEvents).toContainEqual({ running: true, appId: APP_ID, romId: ROM_ID });

    sessionEvents.length = 0;
    vi.setSystemTime(60_000);
    await stopGame(lifetime);
    expect(sessionEvents).toContainEqual({ running: false, appId: APP_ID, romId: ROM_ID });
  });

  it("dispatches running:true when adopting a running session via a matching breadcrumb", async () => {
    localStorage.setItem(
      BREADCRUMB_KEY,
      JSON.stringify({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 0 }),
    );
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(sessionEvents).toContainEqual({ running: true, appId: APP_ID, romId: ROM_ID });
  });

  it("dispatches running:true when adopting a running session with no breadcrumb", async () => {
    vi.stubGlobal("Router", { MainRunningApp: { appid: APP_ID, display_name: "Game" } });

    await initSessionManager();

    expect(sessionEvents).toContainEqual({ running: true, appId: APP_ID, romId: ROM_ID });
  });

  it("does not dispatch a session event when nothing is running at init", async () => {
    vi.stubGlobal("Router", { MainRunningApp: null });
    await initDrainingAdoptionPoll();
    expect(sessionEvents).toEqual([]);
  });
});
