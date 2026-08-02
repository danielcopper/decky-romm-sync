import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import {
  initSessionManager,
  destroySessionManager,
  isSessionActive,
  planAdoption,
  ADOPTION_POLL_MAX_MS,
} from "./sessionManager";

// sessionManager talks to the backend callable surface and the migration
// stores. Mock both so the test observes only what `handleGameStop` forwards
// to `finalizeGameSession`.
vi.mock("../api/backend", () => ({
  recordSessionStart: vi.fn().mockResolvedValue({ success: true }),
  getAppIdRomIdMap: vi.fn(),
  finalizeGameSession: vi.fn(),
  logInfo: vi.fn(),
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
// A second RomM shortcut — the concurrent-games case from #1621.
const OTHER_APP_ID = 200;
const OTHER_ROM_ID = 9;
// An app the plugin does not own: a regular Steam game or a foreign shortcut.
const UNRELATED_APP_ID = 4242;

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

/** Drive a start notification for a specific app through the lifecycle chain. */
async function startApp(cb: LifetimeCb, appId: number): Promise<void> {
  cb({ bRunning: true, unAppID: appId });
  // The start path runs straight off the notification — no timer to advance past,
  // only the chain's microtasks to flush.
  await vi.advanceTimersByTimeAsync(0);
}

/** Drive a stop notification for a specific app and flush the chain. */
async function stopApp(cb: LifetimeCb, appId: number): Promise<void> {
  cb({ bRunning: false, unAppID: appId });
  await vi.advanceTimersByTimeAsync(0);
}

/** Drive a game-start notification through the serialized lifecycle chain. */
async function startGame(cb: LifetimeCb): Promise<void> {
  await startApp(cb, APP_ID);
}

/** Drive a game-stop notification and flush the chain. */
async function stopGame(cb: LifetimeCb): Promise<void> {
  await stopApp(cb, APP_ID);
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

// Liveness comes from `SteamUIStore.RunningApps` — the one running-app surface
// (#1588). It is a membership set, so a single-entry list seeds a running game
// and an empty list seeds "the store reports nothing", which is exactly what the
// post-loader-restart adoption window looks like on-device.
function stubRunningApp(appid: number, displayName = "Game"): void {
  vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid, display_name: displayName }] });
}

function stubRunningApps(apps: { appid: number; display_name: string }[]): void {
  vi.stubGlobal("SteamUIStore", { RunningApps: apps });
}

function stubNothingRunning(): void {
  vi.stubGlobal("SteamUIStore", { RunningApps: [] });
}

// The durable attestation: one versioned localStorage row holding EVERY open
// session. `seedSessions` writes the current (v2) shape; a few tests seed a
// literal v1 row instead, so the in-place upgrade stays covered where it
// actually happens. `readSessions` is the shape assertion most tests want (the
// attested list, or null for "no row"); `readCrumb` exposes the raw row for the
// version itself.
const BREADCRUMB_KEY = "decky-romm-sync:active-session";

function seedSessions(sessions: { appId: number; romId: number; startMs: number }[]): void {
  localStorage.setItem(BREADCRUMB_KEY, JSON.stringify({ v: 2, sessions }));
}

/** A row in the pre-#1624 (v1) shape — one session inline, no `sessions` list. */
function seedV1Session(session: { appId: number; romId: number; startMs: number }): void {
  localStorage.setItem(BREADCRUMB_KEY, JSON.stringify({ v: 1, ...session }));
}

function readCrumb(): unknown {
  const raw = localStorage.getItem(BREADCRUMB_KEY);
  return raw === null ? null : JSON.parse(raw);
}

function readSessions(): unknown[] | null {
  const crumb = readCrumb();
  if (crumb === null) return null;
  const sessions = (crumb as { sessions?: unknown }).sessions;
  return Array.isArray(sessions) ? sessions : null;
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

// The reconcile matrix as a pure decision, independent of the poll, the epoch,
// localStorage and the backend. The behavioural tests below drive the same
// matrix end-to-end; these pin each cell of it directly, including the
// combinations that are awkward to stage through the running-app store.
describe("planAdoption — the reload reconcile matrix", () => {
  // The map binds APP_ID → ROM_ID and OTHER_APP_ID → OTHER_ROM_ID; anything else
  // is a foreign app the plugin does not own.
  const resolveRomId = (appId: number): number | null =>
    ({ [APP_ID]: ROM_ID, [OTHER_APP_ID]: OTHER_ROM_ID })[appId] ?? null;
  const NOW = 9_000;
  const plan = (crumbs: { appId: number; romId: number; startMs: number }[], running: number[], tracked?: number[]) =>
    planAdoption(crumbs, running, new Set(tracked ?? []), resolveRomId, NOW);

  it("decides nothing from nothing", () => {
    expect(plan([], [])).toEqual({ adopted: [], restamped: [], orphans: [] });
  });

  it("adopts an attested session whose app is running, keeping its attested rom and start", () => {
    // The attested romId is trusted over the resolver: the open marker belongs to
    // the rom the START opened, and the binding is not stable over time.
    const crumb = { appId: APP_ID, romId: OTHER_ROM_ID, startMs: 5_000 };

    expect(plan([crumb], [APP_ID])).toEqual({ adopted: [crumb], restamped: [], orphans: [] });
  });

  it("orphans an attested session whose app is not running", () => {
    const crumb = { appId: APP_ID, romId: ROM_ID, startMs: 5_000 };

    expect(plan([crumb], [])).toEqual({ adopted: [], restamped: [], orphans: [crumb] });
  });

  it("re-stamps a running app of ours that nothing attests", () => {
    expect(plan([], [APP_ID])).toEqual({
      adopted: [],
      restamped: [{ appId: APP_ID, romId: ROM_ID, startMs: NOW }],
      orphans: [],
    });
  });

  it("ignores a foreign running app entirely, and never orphans over it", () => {
    // The foreign app is at the HEAD, ahead of the attested one — the ordering
    // that used to make adoption drop a live session.
    const crumb = { appId: APP_ID, romId: ROM_ID, startMs: 5_000 };

    expect(plan([crumb], [UNRELATED_APP_ID, APP_ID])).toEqual({
      adopted: [crumb],
      restamped: [],
      orphans: [],
    });
  });

  it("splits a mixed set: one adopted, one orphaned, one re-stamped", () => {
    const live = { appId: APP_ID, romId: ROM_ID, startMs: 5_000 };
    const gone = { appId: 555, romId: 99, startMs: 7_000 };

    expect(plan([live, gone], [APP_ID, OTHER_APP_ID])).toEqual({
      adopted: [live],
      restamped: [{ appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: NOW }],
      orphans: [gone],
    });
  });

  it("skips an app that already holds an open session, in both passes (#1589)", () => {
    // Neither re-adopted (which would reset its start) nor orphaned (which would
    // drop a live attestation) — the running session is simply left alone.
    const crumb = { appId: APP_ID, romId: ROM_ID, startMs: 5_000 };

    expect(plan([crumb], [APP_ID, OTHER_APP_ID], [APP_ID, OTHER_APP_ID])).toEqual({
      adopted: [],
      restamped: [],
      orphans: [],
    });
  });
});

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

  // #1624: the start path takes the app id from the notification itself. The
  // running-app store must not be consulted — see the reader's docstring.
  it("opens the session for the app the notification names, not the running-app head", async () => {
    // A DIFFERENT app sits at the head of the store. `RunningApps` is ordered
    // most-recently-foregrounded and appends fresh arrivals at the tail, so the
    // head is never the app that just started; trusting it would open a session
    // on the wrong app and leave this one's stop finalizing nothing.
    vi.stubGlobal("SteamUIStore", { RunningApps: [{ appid: OTHER_APP_ID, display_name: "Other" }] });
    await initSessionManager();
    const lifetime = captureLifetimeCb();

    await startApp(lifetime, APP_ID);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);

    vi.setSystemTime(60_000);
    await stopApp(lifetime, APP_ID);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("records the start without stalling the lifecycle chain on a timer", async () => {
    // The 500ms wait existed only so the running-app head could populate. With
    // that read gone it stalls every queued lifecycle event behind it — advancing
    // no time at all must already have recorded the start.
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    lifetime({ bRunning: true, unAppID: APP_ID });
    await vi.advanceTimersByTimeAsync(0);

    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
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

// The liveness predicate both launch surfaces read synchronously — the Play
// button to seed and self-heal its Resume overlay, the interceptor to skip its
// cancel-then-gate funnel for an already-running game. It must discriminate by
// rom: answering "yes" for a rom that is not the live one would skip the
// pre-launch sync for a game that is not running.
describe("sessionManager isSessionActive", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    localStorage.clear();
    stubLifecycleSteamClient();
    stubNothingRunning();
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({
      [String(APP_ID)]: ROM_ID,
      [String(OTHER_APP_ID)]: OTHER_ROM_ID,
    });
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({ ...IDLE_FINALIZE });
  });

  afterEach(() => {
    destroySessionManager();
    vi.useRealTimers();
  });

  it("is false with no session open", async () => {
    await initDrainingAdoptionPoll();

    expect(isSessionActive(ROM_ID)).toBe(false);
  });

  it("is true for the live rom and false for any other", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startApp(lifetime, APP_ID);

    expect(isSessionActive(ROM_ID)).toBe(true);
    expect(isSessionActive(OTHER_ROM_ID)).toBe(false);
  });

  it("goes false again once the session's own app stops", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startApp(lifetime, APP_ID);
    await stopApp(lifetime, APP_ID);

    expect(isSessionActive(ROM_ID)).toBe(false);
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
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubRunningApp(APP_ID);

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
    stubRunningApp(APP_ID);

    await initSessionManager();

    // Case (a′): re-stamp exactly once, then attest with a fresh breadcrumb so
    // a later reload adopts via case (a) instead of re-stamping again.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    expect(readSessions()).toEqual([expect.objectContaining({ appId: APP_ID, romId: ROM_ID })]);
  });

  it("adopts and re-stamps the running game while orphaning a breadcrumb for a different app", async () => {
    // Stale breadcrumb for an app that is not running, in the pre-#1624 shape.
    // Its app never surfaces, so the poll spends its whole budget waiting for it.
    seedV1Session({ appId: 555, romId: 99, startMs: 5_000 });
    stubRunningApp(APP_ID);

    await initDrainingAdoptionPoll();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // The running game is adopted on its own terms; the stale attestation is
    // orphaned rather than transplanted onto it.
    expect(readSessions()).toEqual([expect.objectContaining({ appId: APP_ID, romId: ROM_ID })]);
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("orphaned"));
  });

  it("clears the breadcrumb and does not adopt when nothing is running", async () => {
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubNothingRunning();

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
    stubRunningApp(999, "Other");

    await initSessionManager();

    expect(backend.recordSessionStart).not.toHaveBeenCalled();

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
  });

  it("leaves no breadcrumb after a normal start then stop", async () => {
    stubNothingRunning();
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
    stubRunningApp(APP_ID);
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
    stubNothingRunning();
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

    // Re-init while the game is still running — the poll sees the running app
    // on its first round and adopts via the surviving breadcrumb.
    stubRunningApp(APP_ID);
    await initSessionManager();
    // Case (a): durable marker preserved — no second record_session_start.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);

    const lifetime2 = captureLifetimeCb();
    vi.setSystemTime(180_000);
    await stopGame(lifetime2);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  // #1624: the durable row is versioned, and the version is branched on BEFORE
  // any field is read. A row written by the PREVIOUS release must upgrade in
  // place — treating it as unusable would silently discard the pre-upgrade span
  // of a session that is still running, on the very first reload after an update.
  it("upgrades a v1 breadcrumb in place: adopted via (a), marker untouched, row rewritten as v2", async () => {
    seedV1Session({ appId: APP_ID, romId: ROM_ID, startMs: 5_000 });
    stubRunningApp(APP_ID);

    await initSessionManager();

    // The pre-upgrade start survives: no re-stamp, and the rewritten row carries
    // the ORIGINAL startMs rather than "now".
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(readCrumb()).toEqual({ v: 2, sessions: [{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }] });

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("keeps the sound entries of a v2 row when one entry is malformed", async () => {
    // Per-entry tolerance: a single corrupt entry must not void its siblings.
    localStorage.setItem(
      BREADCRUMB_KEY,
      JSON.stringify({
        v: 2,
        sessions: [
          { appId: "not-a-number", romId: ROM_ID },
          { appId: APP_ID, romId: ROM_ID, startMs: 5_000 },
        ],
      }),
    );
    stubRunningApp(APP_ID);

    await initSessionManager();

    // The good entry still attests the running session — adopted via (a).
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(readSessions()).toEqual([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
  });

  it("treats a future-version breadcrumb as unusable and re-stamps (a′)", async () => {
    // A row from a schema this build knows nothing about: its fields cannot be
    // trusted even if they happen to look right.
    localStorage.setItem(BREADCRUMB_KEY, JSON.stringify({ v: 3, sessions: [{ appId: APP_ID, romId: ROM_ID }] }));
    stubRunningApp(APP_ID);

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    // Overwritten with a fresh row in the current shape.
    expect(readCrumb()).toEqual({ v: 2, sessions: [{ appId: APP_ID, romId: ROM_ID, startMs: 0 }] });
  });

  it("treats a v2 row whose sessions are missing or not a list as unusable and re-stamps (a′)", async () => {
    localStorage.setItem(BREADCRUMB_KEY, JSON.stringify({ v: 2 }));
    stubRunningApp(APP_ID);

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);

    destroySessionManager();
    vi.clearAllMocks();
    localStorage.setItem(BREADCRUMB_KEY, JSON.stringify({ v: 2, sessions: { appId: APP_ID } }));

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
  });

  it("treats a non-object breadcrumb JSON as unusable and re-stamps (a′)", async () => {
    // Valid JSON but not an object — no version to branch on.
    localStorage.setItem(BREADCRUMB_KEY, JSON.stringify(42));
    stubRunningApp(APP_ID);

    await initSessionManager();

    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(readSessions()).toEqual([expect.objectContaining({ appId: APP_ID, romId: ROM_ID })]);
  });

  it("survives a rejected recordSessionStart during adoption", async () => {
    vi.mocked(backend.recordSessionStart).mockRejectedValueOnce(new Error("network down"));
    stubRunningApp(APP_ID);

    // (a′) awaits recordSessionStart; its rejection is caught, not surfaced.
    await expect(initSessionManager()).resolves.toBeUndefined();
    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("record session start on adoption"));

    // The breadcrumb is written before the failing call, and the session is
    // still adopted — a subsequent stop finalizes the original rom.
    expect(readSessions()).toEqual([expect.objectContaining({ appId: APP_ID, romId: ROM_ID })]);
    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  // #1624: liveness is a MEMBERSHIP question. Adoption used to read only the head
  // of the running-app list, so a foreign app in the foreground orphaned a RomM
  // game that was still running right behind it — losing its playtime and its
  // post-exit save sync.
  it("adopts a running game listed behind a foreground foreign app", async () => {
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubRunningApps([
      { appid: 999, display_name: "Other" },
      { appid: APP_ID, display_name: "Game" },
    ]);

    await initSessionManager();

    // Attested and running → adopted as attested, marker untouched, row kept.
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(readSessions()).toEqual([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("orphaned"));

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  // #1624: adoption reconciles the whole attested SET against the whole running
  // set, so any number of concurrent games recovers from one reload.
  it("restores two attested sessions when both games are still running", async () => {
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({
      [String(APP_ID)]: ROM_ID,
      [String(OTHER_APP_ID)]: OTHER_ROM_ID,
    });
    seedSessions([
      { appId: APP_ID, romId: ROM_ID, startMs: 5_000 },
      { appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 7_000 },
    ]);
    stubRunningApps([
      { appid: APP_ID, display_name: "Game" },
      { appid: OTHER_APP_ID, display_name: "Other Game" },
    ]);
    const sessionEvents: unknown[] = [];
    const listener = (e: WindowEventMap["romm_session_changed"]) => sessionEvents.push(e.detail);
    globalThis.addEventListener("romm_session_changed", listener);

    try {
      await initSessionManager();

      // Both attested spans survive: neither marker is re-stamped, both rows are
      // kept verbatim, and both surfaces are told their game is live.
      expect(backend.recordSessionStart).not.toHaveBeenCalled();
      expect(readSessions()).toEqual([
        { appId: APP_ID, romId: ROM_ID, startMs: 5_000 },
        { appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 7_000 },
      ]);
      expect(sessionEvents).toEqual([
        { running: true, appId: APP_ID, romId: ROM_ID },
        { running: true, appId: OTHER_APP_ID, romId: OTHER_ROM_ID },
      ]);

      // Each adopted session finalizes on its own app's exit.
      const lifetime = captureLifetimeCb();
      await stopApp(lifetime, OTHER_APP_ID);
      expect(backend.finalizeGameSession).toHaveBeenCalledTimes(1);
      expect(backend.finalizeGameSession).toHaveBeenCalledWith(OTHER_ROM_ID);
      await stopApp(lifetime, APP_ID);
      expect(backend.finalizeGameSession).toHaveBeenCalledTimes(2);
      expect(backend.finalizeGameSession).toHaveBeenLastCalledWith(ROM_ID);
    } finally {
      globalThis.removeEventListener("romm_session_changed", listener);
    }
  });

  it("waits for a late-surfacing attested app instead of orphaning it", async () => {
    // The store omits apps whose overview has not loaded yet, so a reading that
    // already lists one concurrent game can still be missing its sibling.
    // Settling on the first non-empty reading would orphan the straggler.
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({
      [String(APP_ID)]: ROM_ID,
      [String(OTHER_APP_ID)]: OTHER_ROM_ID,
    });
    seedSessions([
      { appId: APP_ID, romId: ROM_ID, startMs: 5_000 },
      { appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 7_000 },
    ]);
    stubRunningApp(APP_ID);

    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(1_000); // rounds pass with only one app listed
    stubRunningApps([
      { appid: APP_ID, display_name: "Game" },
      { appid: OTHER_APP_ID, display_name: "Other Game" },
    ]);
    await vi.advanceTimersByTimeAsync(500);
    await init;

    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("orphaned"));
    expect(readSessions()).toEqual([
      { appId: APP_ID, romId: ROM_ID, startMs: 5_000 },
      { appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 7_000 },
    ]);
  });

  it("adopts the attested session that is still running and orphans the one that is not", async () => {
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({
      [String(APP_ID)]: ROM_ID,
      [String(OTHER_APP_ID)]: OTHER_ROM_ID,
    });
    seedSessions([
      { appId: APP_ID, romId: ROM_ID, startMs: 5_000 },
      { appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 7_000 },
    ]);
    stubRunningApp(APP_ID);

    // The second game's app never surfaces — the poll waits out its budget for it.
    await initDrainingAdoptionPoll();

    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(readSessions()).toEqual([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining(`orphaned`));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining(`romId=${OTHER_ROM_ID}`));

    // The survivor still finalizes; the orphan's app is no longer tracked.
    const lifetime = captureLifetimeCb();
    await stopApp(lifetime, OTHER_APP_ID);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
    await stopApp(lifetime, APP_ID);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("adopts an attested game as-is alongside an unattested one it re-stamps", async () => {
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({
      [String(APP_ID)]: ROM_ID,
      [String(OTHER_APP_ID)]: OTHER_ROM_ID,
    });
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    vi.setSystemTime(9_000);
    stubRunningApps([
      { appid: APP_ID, display_name: "Game" },
      { appid: OTHER_APP_ID, display_name: "Other Game" },
    ]);

    await initSessionManager();

    // Exactly one re-stamp — the attested game keeps its span, the unattested one
    // gets a truthful lower bound from now.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(OTHER_ROM_ID);
    expect(readSessions()).toEqual([
      { appId: APP_ID, romId: ROM_ID, startMs: 5_000 },
      { appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 9_000 },
    ]);
  });

  it("adopts the romId the breadcrumb attests, not the one the map now resolves", async () => {
    // The appId → romId binding is 1:1 at any instant but not stable over time —
    // a version switch moves the shortcut to a different rom row. The durable
    // marker belongs to the rom the START opened, so re-stamping the map's
    // CURRENT rom would open a second marker and leave the original dangling.
    seedSessions([{ appId: APP_ID, romId: OTHER_ROM_ID, startMs: 5_000 }]);
    stubRunningApp(APP_ID); // the map binds APP_ID to ROM_ID, not OTHER_ROM_ID

    await initSessionManager();

    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(readSessions()).toEqual([{ appId: APP_ID, romId: OTHER_ROM_ID, startMs: 5_000 }]);

    const lifetime = captureLifetimeCb();
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(OTHER_ROM_ID);
  });

  it("orphans an attested session when only a foreign app is running", async () => {
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubRunningApp(999, "Other");

    // The attested app never surfaces, so the poll waits out its budget before
    // concluding the session ended while the plugin was down.
    await initDrainingAdoptionPoll();

    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("orphaned"));
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
    stubNothingRunning();

    // Nothing running → the poll times out, then the orphaned-breadcrumb clear
    // hits the throwing removeItem, which must be swallowed.
    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(ADOPTION_POLL_MAX_MS);
    await expect(init).resolves.toBeUndefined();

    expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("clear session breadcrumb"));
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
  });

  it("tolerates a throwing running-app getter without erroring adoption", async () => {
    // Steam's running-app accessor faulting must not crash init. The reader
    // catches the throw and notes it in the diagnostics, so adoption reaches its
    // normal timeout verdict instead of erroring out (#1148 round 2) — the
    // pre-fix unguarded read let the throw escape to the adoption .catch.
    vi.stubGlobal("SteamUIStore", {
      get RunningApps(): unknown {
        throw new Error("running-app read failed");
      },
    });

    // Nothing readable → the poll times out and init resolves cleanly.
    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(ADOPTION_POLL_MAX_MS);
    await expect(init).resolves.toBeUndefined();

    // No adoption error surfaced; the timed-out round logged what the store
    // reported, i.e. that it threw.
    expect(backend.logError).not.toHaveBeenCalledWith(expect.stringContaining("Session adoption error"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("threw:"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("no running app"));
  });

  it("tolerates an entirely absent running-app store without erroring adoption", async () => {
    // SteamUIStore intentionally not stubbed — on a build/timing where the
    // global is genuinely absent, a bare read would throw ReferenceError out of
    // the poll. Adoption must still reach its ordinary timeout verdict.
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);

    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(ADOPTION_POLL_MAX_MS);
    await expect(init).resolves.toBeUndefined();

    expect(backend.logError).not.toHaveBeenCalledWith(expect.stringContaining("Session adoption error"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("no-store"));
    // Case (b): nothing attested as running → the breadcrumb is orphan-cleared.
    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
  });

  // #1054 follow-up: after a full plugin_loader restart the store reports an
  // EMPTY running-app list for several seconds while the game is still running,
  // so a one-shot read wrongly orphaned a live session. Adoption now polls.
  it("adopts a matching breadcrumb once the store reports the app mid-poll, no orphan log", async () => {
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubNothingRunning();

    const init = initSessionManager();
    // Four polls into the loader-restart window, the store is still empty.
    await vi.advanceTimersByTimeAsync(2_000);
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    // Steam finally populates the running app; the next poll sees it.
    stubRunningApp(APP_ID);
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
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubNothingRunning();

    await initDrainingAdoptionPoll();

    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("no running app"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("orphaned"));
  });

  it("stays silent when the poll times out with no breadcrumb", async () => {
    stubNothingRunning();

    await initDrainingAdoptionPoll();

    expect(readCrumb()).toBeNull();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    // No breadcrumb to orphan — the timeout is a no-op beyond the poll log.
    expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("orphaned"));
    expect(backend.logInfo).toHaveBeenCalledWith(expect.stringContaining("no running app"));
  });

  it("queues a racing stop behind the poll so it finalizes after adoption", async () => {
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 1_000 }]);
    stubNothingRunning();

    const init = initSessionManager();
    // Let init register the lifetime hook and enter the poll.
    await vi.advanceTimersByTimeAsync(500);
    const lifetime = captureLifetimeCb();

    // A stop arrives WHILE the poll is still running. It queues on the lifecycle
    // chain behind the in-flight adoption task rather than interleaving with it.
    lifetime({ bRunning: false, unAppID: APP_ID });

    // The game becomes visible; the poll resolves and adoption (a) runs first.
    stubRunningApp(APP_ID);
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
    stubNothingRunning();

    const init = initSessionManager();
    await vi.advanceTimersByTimeAsync(2_000); // poll running, nothing found yet
    destroySessionManager(); // tears down mid-poll → bumps the epoch
    // A running game appears after teardown; the still-pending poll would adopt it
    // (case a′ → recordSessionStart + breadcrumb) if it did not check the epoch.
    stubRunningApp(APP_ID);
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
    stubNothingRunning();
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
    // A v1 row, extra keys and all — the lift ignores what it does not know.
    localStorage.setItem(
      BREADCRUMB_KEY,
      JSON.stringify({ v: 1, appId: APP_ID, romId: ROM_ID, startMs: 5_000, pausedMs: 0 }),
    );
    stubRunningApp(APP_ID);

    await initSessionManager();

    expect(sessionEvents).toContainEqual({ running: true, appId: APP_ID, romId: ROM_ID });
  });

  it("dispatches running:true when adopting a running session with no breadcrumb", async () => {
    stubRunningApp(APP_ID);

    await initSessionManager();

    expect(sessionEvents).toContainEqual({ running: true, appId: APP_ID, romId: ROM_ID });
  });

  it("does not dispatch a session event when nothing is running at init", async () => {
    stubNothingRunning();
    await initDrainingAdoptionPoll();
    expect(sessionEvents).toEqual([]);
  });
});

// #1621: RegisterForAppLifetimeNotifications fires for EVERY app Steam tracks,
// so the stop path must finalize only the app that opened the active session.
// Finalizing on a foreign app's exit recorded the wrong playtime AND ran the
// post-exit save sync against a game still holding its save file open.
describe("sessionManager stop scoping (#1621)", () => {
  // finalizeGameSession is the single callable behind BOTH the playtime write and
  // the post-exit save sync, so "not called" is the direct assertion that neither
  // ran. total_seconds makes the playtime display write observable too.
  const FINALIZE_WITH_PLAYTIME = { ...IDLE_FINALIZE, total_seconds: 99 };

  let dataChanged: unknown[];
  let dataListener: (e: Event) => void;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.setSystemTime(0);
    localStorage.clear();
    stubLifecycleSteamClient();
    // Two RomM shortcuts in the map; the unrelated app is deliberately absent.
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({
      [String(APP_ID)]: ROM_ID,
      [String(OTHER_APP_ID)]: OTHER_ROM_ID,
    });
    vi.mocked(backend.recordSessionStart).mockResolvedValue({ success: true });
    vi.mocked(backend.finalizeGameSession).mockResolvedValue({ ...FINALIZE_WITH_PLAYTIME });
    // Nothing in the running-app store, so reload-adoption is inert here and each
    // start is addressable per app via its notification's unAppID.
    stubNothingRunning();
    dataChanged = [];
    dataListener = (e: Event) => dataChanged.push((e as CustomEvent).detail);
    globalThis.addEventListener("romm_data_changed", dataListener);
  });

  afterEach(() => {
    globalThis.removeEventListener("romm_data_changed", dataListener);
    destroySessionManager();
    vi.useRealTimers();
  });

  it("finalizes when the active session's own app stops", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    vi.setSystemTime(60_000);
    await stopApp(lifetime, APP_ID);

    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
    expect(updatePlaytimeDisplay).toHaveBeenCalledWith(APP_ID, 99);
    expect(dataChanged).toContainEqual({ type: "save_sync", rom_id: ROM_ID });
  });

  it("drops the session and rewrites the row before awaiting the finalize", async () => {
    // Ordering invariant: the stop is OBSERVED the moment it arrives, so the
    // entry and its attestation must be gone before the long finalize await —
    // not after it. Persisting afterwards would leave the row live for the whole
    // post-exit sync, and a reload landing in that window would adopt a session
    // whose stop was already seen, re-opening its marker over a closed span.
    let releaseFinalize!: (result: typeof FINALIZE_WITH_PLAYTIME) => void;
    vi.mocked(backend.finalizeGameSession).mockImplementation(
      () => new Promise((resolve) => (releaseFinalize = resolve)),
    );

    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();
    vi.setSystemTime(20_000);
    await startGame(lifetime);
    expect(readSessions()).not.toBeNull();

    vi.setSystemTime(60_000);
    await stopApp(lifetime, APP_ID);

    // The finalize is in flight and has NOT resolved yet.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
    expect(updatePlaytimeDisplay).not.toHaveBeenCalled();
    // Already forgotten, both in memory and on disk.
    expect(isSessionActive(ROM_ID)).toBe(false);
    expect(readCrumb()).toBeNull();

    releaseFinalize({ ...FINALIZE_WITH_PLAYTIME });
    await vi.advanceTimersByTimeAsync(0);
    expect(updatePlaytimeDisplay).toHaveBeenCalledWith(APP_ID, 99);
  });

  it("ignores a stop for an unrelated app and leaves the session open", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    // Times are absolute and the adoption poll already drained to 15s.
    vi.setSystemTime(20_000);
    await startGame(lifetime);
    vi.setSystemTime(30_000);
    await stopApp(lifetime, UNRELATED_APP_ID);

    // No playtime write and no post-exit sync — finalizeGameSession performs both.
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
    expect(updatePlaytimeDisplay).not.toHaveBeenCalled();
    expect(dataChanged).toEqual([]);
    expect(backend.debugLog).toHaveBeenCalledWith(expect.stringContaining("Session stop ignored"));

    // The session is still open: its breadcrumb survives and its own app's stop
    // still finalizes it, at the real end time.
    expect(readSessions()).toEqual([{ appId: APP_ID, romId: ROM_ID, startMs: 20_000 }]);
    vi.setSystemTime(60_000);
    await stopApp(lifetime, APP_ID);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
    expect(updatePlaytimeDisplay).toHaveBeenCalledWith(APP_ID, 99);
  });

  it("ignores a stop for a second RomM game that opened no session", async () => {
    // The stopping app maps to a real rom, so a fresh map lookup would happily
    // resolve one — only its absence from the session map rejects it.
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);
    await stopApp(lifetime, OTHER_APP_ID);

    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
    expect(dataChanged).toEqual([]);
  });

  // #1624: a second RomM game used to displace the first, which then recorded no
  // playtime and ran no post-exit save sync at all — its stop found a slot that
  // no longer held it. Both now stand on their own.
  it("keeps both sessions when a second RomM game starts, finalizing each on its own app's exit", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    // Times are absolute and the adoption poll already drained to 15s.
    vi.setSystemTime(20_000);
    await startGame(lifetime);
    vi.setSystemTime(30_000);
    await startApp(lifetime, OTHER_APP_ID);

    // Two open markers, two attested sessions, nothing finalized yet.
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(OTHER_ROM_ID);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();
    expect(readSessions()).toEqual([
      { appId: APP_ID, romId: ROM_ID, startMs: 20_000 },
      { appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 30_000 },
    ]);

    // The first game exits: only its rom is finalized, and the sibling session
    // survives — both in memory and in the rewritten row.
    vi.setSystemTime(60_000);
    await stopApp(lifetime, APP_ID);
    expect(backend.finalizeGameSession).toHaveBeenCalledTimes(1);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
    expect(updatePlaytimeDisplay).toHaveBeenCalledWith(APP_ID, 99);
    expect(readSessions()).toEqual([{ appId: OTHER_APP_ID, romId: OTHER_ROM_ID, startMs: 30_000 }]);

    // The second game exits: its own rom is finalized, at its own end time.
    vi.setSystemTime(90_000);
    await stopApp(lifetime, OTHER_APP_ID);
    expect(backend.finalizeGameSession).toHaveBeenCalledTimes(2);
    expect(backend.finalizeGameSession).toHaveBeenLastCalledWith(OTHER_ROM_ID);
    expect(updatePlaytimeDisplay).toHaveBeenCalledWith(OTHER_APP_ID, 99);
    expect(readCrumb()).toBeNull();
  });

  it("finalizes the live session even when the app map went stale mid-session", async () => {
    // The reason the stop compares the RECORDED appId instead of re-resolving the
    // stopping app through the cached map: the map can go stale while a game runs
    // (a sync that drops or re-keys the shortcut). A fresh lookup would then
    // resolve nothing for the very app that opened the session and drop it —
    // losing its playtime AND skipping its post-exit sync, which turns a
    // mis-attribution bug into a data-loss one. The recorded appId cannot go
    // stale, so the live session still finalizes.
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    await startGame(lifetime);

    // The map empties, and an unrelated app's start refreshes the cache to it —
    // that start is a no-op for the session (nothing maps to the app).
    vi.mocked(backend.getAppIdRomIdMap).mockResolvedValue({});
    await startApp(lifetime, UNRELATED_APP_ID);
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(backend.recordSessionStart).toHaveBeenCalledWith(ROM_ID);

    vi.setSystemTime(60_000);
    await stopApp(lifetime, APP_ID);

    // The session finalizes on the stale map: playtime folded, display updated,
    // post-exit sync run, breadcrumb cleared.
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
    expect(updatePlaytimeDisplay).toHaveBeenCalledWith(APP_ID, 99);
    expect(dataChanged).toContainEqual({ type: "save_sync", rom_id: ROM_ID });
    expect(readCrumb()).toBeNull();
  });

  // #1589: `record_session_start` RE-OPENS the durable marker instead of
  // extending it, so a second start for a live session discards the span already
  // played. The observed symptom was a launch inside the plugin's startup window
  // being stamped twice, ~1.3s apart, and measured short by the gap.
  it("does not re-open a live session when the same game reports a second start", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();

    vi.setSystemTime(20_000);
    await startGame(lifetime);
    vi.setSystemTime(50_000);
    await startGame(lifetime);

    // One marker, still stamped at the FIRST start — the 30s in between survives.
    expect(backend.recordSessionStart).toHaveBeenCalledTimes(1);
    expect(readSessions()).toEqual([{ appId: APP_ID, romId: ROM_ID, startMs: 20_000 }]);
    expect(backend.debugLog).toHaveBeenCalledWith(expect.stringContaining("Session start ignored"));
  });

  it("re-announces the live session on a duplicate start so a stale surface self-heals", async () => {
    await initDrainingAdoptionPoll();
    const lifetime = captureLifetimeCb();
    await startGame(lifetime);

    const sessionEvents: unknown[] = [];
    const listener = (e: WindowEventMap["romm_session_changed"]) => sessionEvents.push(e.detail);
    globalThis.addEventListener("romm_session_changed", listener);
    try {
      await startGame(lifetime);
      expect(sessionEvents).toEqual([{ running: true, appId: APP_ID, romId: ROM_ID }]);
    } finally {
      globalThis.removeEventListener("romm_session_changed", listener);
    }
  });

  it("does not re-stamp the marker when the real start notification follows an adoption", async () => {
    // The #1589 device ordering: adoption opens the session at init, then the
    // launch's own notification arrives a second later.
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubRunningApp(APP_ID);

    await initSessionManager();
    const lifetime = captureLifetimeCb();
    expect(backend.recordSessionStart).not.toHaveBeenCalled();

    vi.setSystemTime(20_000);
    await startGame(lifetime);

    // The adopted span is kept and the marker is left alone.
    expect(backend.recordSessionStart).not.toHaveBeenCalled();
    expect(readSessions()).toEqual([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);

    // The session is still the adopted one — its stop finalizes normally.
    vi.setSystemTime(60_000);
    await stopGame(lifetime);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("scopes the stop after adopting a session from a matching breadcrumb", async () => {
    seedSessions([{ appId: APP_ID, romId: ROM_ID, startMs: 5_000 }]);
    stubRunningApp(APP_ID);

    await initSessionManager();
    const lifetime = captureLifetimeCb();

    // The adopted session carries its appId, so a foreign stop is still ignored.
    await stopApp(lifetime, UNRELATED_APP_ID);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();

    await stopApp(lifetime, APP_ID);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });

  it("scopes the stop after adopting a running game with no breadcrumb", async () => {
    stubRunningApp(APP_ID);

    await initSessionManager();
    const lifetime = captureLifetimeCb();

    await stopApp(lifetime, UNRELATED_APP_ID);
    expect(backend.finalizeGameSession).not.toHaveBeenCalled();

    await stopApp(lifetime, APP_ID);
    expect(backend.finalizeGameSession).toHaveBeenCalledWith(ROM_ID);
  });
});
