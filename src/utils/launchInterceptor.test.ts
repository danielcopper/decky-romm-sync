import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import * as gameDetailPatch from "../patches/gameDetailPatch";
import * as launchGate from "./launchGate";
import * as sessionManager from "./sessionManager";
import * as runningApps from "./runningApps";
import * as steamShortcuts from "./steamShortcuts";
import { registerLaunchInterceptor, unregisterLaunchInterceptor, type LaunchPrompts } from "./launchInterceptor";
import { mountPruneLeasePlugin, releaseAllPruneLeases } from "./pruneLease";
import type { GateVerdict, LaunchGateOps } from "./launchGate";
import type { SyncConflict } from "../types";

// The interceptor pulls in `../patches/gameDetailPatch` which transitively
// imports `@decky/ui`/`react`. Mock just the surface we touch to keep the test
// focused on the watcher branches.
vi.mock("../patches/gameDetailPatch", () => ({
  isRomMAppId: vi.fn(),
}));

vi.mock("../api/backend", () => ({
  refreshMigrationState: vi.fn(),
  getInstalledRom: vi.fn(),
  getCachedGameDetail: vi.fn(),
  isSaveTrackingConfigured: vi.fn(),
  getSaveSetupInfo: vi.fn(),
  confirmSlotChoice: vi.fn(),
  checkCoreChange: vi.fn(),
  probeReachability: vi.fn(),
  preLaunchSync: vi.fn(),
  checkLocalDrift: vi.fn(),
  // The shared reconcile helper (real module) pulls the single-ROM command here
  // before each watcher relaunch (#1152).
  getRomRelaunchOptions: vi.fn(),
  releaseOrphanedPruneLeases: vi.fn(() => Promise.resolve({ success: true, released: 0 })),
  releasePruneConflictLease: vi.fn(),
  renewPruneConflictLease: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
  // Not called by launchInterceptor itself — the real connectionState store it
  // reports reachability into logs each transition through it.
  debugLog: vi.fn(),
}));

// The reconcile helper confirm-sets the resolved command onto the shortcut.
// Mock just that surface so the watcher relaunch re-confirm is observable
// without touching SteamClient's shortcut APIs.
vi.mock("./steamShortcuts", () => ({
  setLaunchOptionsConfirmed: vi.fn().mockResolvedValue(true),
}));

// Keep the real skip-set (markLaunchSkipped / consumeLaunchSkip) so the
// skip-FIRST behavior is exercised end-to-end; replace runLaunchGate with a spy
// each verdict test drives.
vi.mock("./launchGate", async (importActual) => {
  const actual = await importActual<typeof import("./launchGate")>();
  return { ...actual, runLaunchGate: vi.fn() };
});

vi.mock("./sessionManager", () => ({
  getAppIdRomIdMapSnapshot: vi.fn(() => ({ "1234": 42 })),
  isSessionActive: vi.fn(() => false),
}));

vi.mock("./runningApps", () => ({
  isAppRunning: vi.fn(() => false),
}));

vi.mock("./migrationStore", () => ({
  getMigrationState: vi.fn(() => ({ pending: false })),
  setMigrationStatus: vi.fn(),
}));

vi.mock("./saveSortMigrationStore", () => ({
  setSaveSortMigrationStatus: vi.fn(),
}));

/**
 * The prompts the interceptor asks its caller for. Stubbing them here is the
 * whole point of the injection: a gate branch is reachable without standing up
 * a modal, and the interceptor stays free of any import into `components/`.
 */
const prompts = {
  confirmCoreChange: vi.fn<LaunchPrompts["confirmCoreChange"]>(),
  resolveConflicts: vi.fn<LaunchPrompts["resolveConflicts"]>(),
  askOfflineDrift: vi.fn<LaunchPrompts["askOfflineDrift"]>(),
  confirmFallbackLaunch: vi.fn<LaunchPrompts["confirmFallbackLaunch"]>(),
};

/** Register with the stub prompts — every test drives the interceptor through this. */
const register = (): void => registerLaunchInterceptor(prompts);

type GameActionHandler = (gameActionId: number, appIdStr: string, action: string, launchSource: number) => void;

const captureHandler = (): GameActionHandler => {
  const calls = vi.mocked(SteamClient.Apps.RegisterForGameActionStart).mock.calls;
  const handler = calls[calls.length - 1]?.[0];
  if (!handler) throw new Error("RegisterForGameActionStart was not called");
  return handler as GameActionHandler;
};

const conflict = (overrides: Partial<SyncConflict> = {}): SyncConflict => ({
  type: "sync_conflict",
  rom_id: 42,
  filename: "save.srm",
  server_save_id: 7,
  server_updated_at: "2026-01-01T00:00:00Z",
  server_size: 1024,
  local_path: "/local/save.srm",
  local_hash: "abc",
  local_mtime: "2026-01-01T00:00:00Z",
  local_size: 1024,
  created_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

// Let the detached async body settle (microtasks).
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

const runGameMock = () => vi.mocked(SteamClient.Apps.RunGame);

describe("launchInterceptor — full funnel watcher", () => {
  let unregisterMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    // Drain any skip-set leak from a prior test's relaunch (the real skip-set is
    // module-level state) so a relaunch in one test never silently skips the next.
    launchGate.consumeLaunchSkip(1234);

    unregisterMock = vi.fn();
    vi.stubGlobal("SteamClient", {
      Apps: {
        RegisterForGameActionStart: vi.fn(() => ({ unregister: unregisterMock })),
        CancelGameAction: vi.fn(),
        RunGame: vi.fn(),
      },
    });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-7" })),
    });

    vi.mocked(gameDetailPatch.isRomMAppId).mockReturnValue(true);
    vi.mocked(backend.refreshMigrationState).mockResolvedValue({
      retrodeck: { pending: false },
      save_sort: { pending: false },
    } as unknown as Awaited<ReturnType<typeof backend.refreshMigrationState>>);
    // Default: installed ROM so the funnel runs.
    vi.mocked(backend.getInstalledRom).mockResolvedValue({
      rom_id: 42,
      file_name: "g.rom",
      file_path: "/p/g.rom",
      system: "snes",
      platform_slug: "snes",
      installed_at: "2026-01-01T00:00:00Z",
      launchable: true,
    });
    // Skip-set empty by default — a marked appId is set per-test.
    vi.mocked(sessionManager.getAppIdRomIdMapSnapshot).mockReturnValue({ "1234": 42 });
    // Default: no live session and nothing running, so the already-running guard
    // is inert and the existing funnel tests run unchanged. Overridden per-test.
    vi.mocked(sessionManager.isSessionActive).mockReturnValue(false);
    vi.mocked(runningApps.isAppRunning).mockReturnValue(false);
    vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
    // The shared relaunch re-confirm (#1152) runs on every relaunch; default it
    // to a resolved command + a clean confirm-set so the existing verdict tests
    // exercise the happy path without per-test wiring.
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
      success: true,
      app_id: 1234,
      launch_options: "flatpak run x",
      prune_lease_token: "launch-lease",
    });
    vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
    vi.mocked(backend.renewPruneConflictLease).mockResolvedValue({ success: true, message: "renewed" });
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockResolvedValue(true);
  });

  afterEach(() => {
    unregisterLaunchInterceptor();
  });

  describe("entry guards", () => {
    it("ignores non-LaunchApp actions — no cancel, no gate", async () => {
      register();
      const handler = captureHandler();
      handler(1, "1234", "QuitApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
    });

    it("ignores non-RomM app IDs — no cancel, no gate", async () => {
      vi.mocked(gameDetailPatch.isRomMAppId).mockReturnValue(false);
      register();
      const handler = captureHandler();
      handler(1, "9999", "LaunchApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
    });

    it("skips a marked appId WITHOUT cancelling or gating", async () => {
      // Pre-mark appId 1234 via the real skip-set.
      launchGate.markLaunchSkipped(1234);
      register();
      const handler = captureHandler();
      handler(99, "1234", "LaunchApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
    });
  });

  // #1148 round 2: a Play press on an ALREADY-RUNNING game still fires
  // GameActionStart. Intercepting it cancels the launch and runs the pre-launch
  // sync MID-SESSION (uploading the save while the emulator holds the file) —
  // pure damage, since Steam blocks the relaunch as "already running" anyway. The
  // guard skips the whole funnel when the appId is the live session OR any running
  // -app source reports it running.
  describe("already-running guard", () => {
    it("skips the funnel (no cancel, no gate, no sync) when the appId is the live session", async () => {
      // Our own session state says rom 42 (appId 1234) is live.
      vi.mocked(sessionManager.isSessionActive).mockReturnValue(true);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      // The liveness question is asked about the rom the PRESSED app resolves to
      // through the map snapshot — not about whatever rom happens to be live.
      expect(sessionManager.isSessionActive).toHaveBeenCalledWith(42);
      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
      expect(backend.preLaunchSync).not.toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
      expect(backend.logInfo).toHaveBeenCalledWith(
        expect.stringContaining("appId=1234 already running — skipping pre-launch sync"),
      );
    });

    it("skips the funnel when a running-app source reports the appId running", async () => {
      // No live session in our state, but Steam's running-app surfaces show it.
      vi.mocked(sessionManager.isSessionActive).mockReturnValue(false);
      vi.mocked(runningApps.isAppRunning).mockReturnValue(true);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(runningApps.isAppRunning).toHaveBeenCalledWith(1234);
      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
      expect(backend.preLaunchSync).not.toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
      expect(backend.logInfo).toHaveBeenCalledWith(
        expect.stringContaining("already running — skipping pre-launch sync"),
      );
    });

    it("does NOT skip when the pressed rom has no live session — normal funnel runs", async () => {
      // Another game may well be live; what matters is that the PRESSED rom (42)
      // is not → the guard is inert and the normal cancel+gate funnel proceeds.
      vi.mocked(sessionManager.isSessionActive).mockReturnValue(false);
      vi.mocked(runningApps.isAppRunning).mockReturnValue(false);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(sessionManager.isSessionActive).toHaveBeenCalledWith(42);
      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(77);
      expect(launchGate.runLaunchGate).toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      expect(backend.logInfo).not.toHaveBeenCalledWith(expect.stringContaining("already running"));
    });

    it("does NOT skip when nothing is running — normal funnel runs", async () => {
      // Defaults: no live session, isAppRunning false → guard inert, funnel runs.
      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(77);
      expect(launchGate.runLaunchGate).toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });
  });

  describe("cancel-first", () => {
    it("calls CancelGameAction synchronously before any gate await", () => {
      // Make the gate hang so we can prove the cancel already happened
      // before any async funnel work.
      let resolveGate!: (v: GateVerdict) => void;
      vi.mocked(launchGate.runLaunchGate).mockReturnValue(
        new Promise<GateVerdict>((r) => {
          resolveGate = r;
        }),
      );

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);

      // Synchronously — no await yet — the cancel must already be in.
      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(77);
      resolveGate({ decision: "allow" });
    });
  });

  describe("installed check", () => {
    it("toasts and does NOT relaunch when the ROM is not installed", async () => {
      vi.mocked(backend.getInstalledRom).mockResolvedValue(null);
      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(77);
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "Tender",
        body: "ROM not downloaded. Open the plugin to download it first.",
      });
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("relaunches without gating when the appId is unknown to the session map", async () => {
      vi.mocked(sessionManager.getAppIdRomIdMapSnapshot).mockReturnValue({});
      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("getInstalledRom throws + cached installed=true → funnel proceeds (not hard-blocked)", async () => {
      vi.mocked(backend.getInstalledRom).mockRejectedValue(new Error("net"));
      vi.mocked(backend.getCachedGameDetail).mockResolvedValue({ found: true, installed: true });
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      // Transient install-check error fell back to the cached truth → gate ran.
      expect(launchGate.runLaunchGate).toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      expect(toaster.toast).not.toHaveBeenCalled();
    });

    it("getInstalledRom throws + cached installed=false → hard-blocked (toast, no RunGame)", async () => {
      vi.mocked(backend.getInstalledRom).mockRejectedValue(new Error("net"));
      vi.mocked(backend.getCachedGameDetail).mockResolvedValue({ found: true, installed: false });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(toaster.toast).toHaveBeenCalledWith({
        title: "Tender",
        body: "ROM not downloaded. Open the plugin to download it first.",
      });
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });
  });

  describe("verdict handling", () => {
    it("allow → relaunches via RunGame and marks the appId skipped", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      // markLaunchSkipped fired before RunGame → a re-fire of the same appId is skipped.
      expect(launchGate.consumeLaunchSkip(1234)).toBe(true);
    });

    it("conflict → SyncConflictModal shown; resolved → relaunch + romm_data_changed", async () => {
      const conflicts = [conflict()];
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "conflict", conflicts });
      prompts.resolveConflicts.mockResolvedValue("resolved");
      const dataChanged = vi.fn();
      globalThis.addEventListener("romm_data_changed", dataChanged);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(prompts.resolveConflicts).toHaveBeenCalledWith(conflicts);
      expect(dataChanged).toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      globalThis.removeEventListener("romm_data_changed", dataChanged);
    });

    it("conflict → cancelled → no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "conflict", conflicts: [conflict()] });
      prompts.resolveConflicts.mockResolvedValue("cancel");

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(prompts.resolveConflicts).toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("offline_drift → OfflineDriftModal shown; start_anyway → relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "offline_drift" });
      prompts.askOfflineDrift.mockResolvedValue("start_anyway");

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(prompts.askOfflineDrift).toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("offline_drift → cancel → no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "offline_drift" });
      prompts.askOfflineDrift.mockResolvedValue("cancel");

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(prompts.askOfflineDrift).toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("offline_drift → retry → re-runs the gate; now allow → relaunch", async () => {
      // First gate pass → offline_drift; user retries. Second gate pass → allow.
      vi.mocked(launchGate.runLaunchGate)
        .mockResolvedValueOnce({ decision: "offline_drift" })
        .mockResolvedValue({ decision: "allow" });
      prompts.askOfflineDrift.mockResolvedValueOnce("retry");

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      // Non-vacuous: the gate RE-RAN (called twice) on retry, and the now-allow
      // verdict relaunched.
      expect(vi.mocked(launchGate.runLaunchGate).mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(prompts.askOfflineDrift).toHaveBeenCalledTimes(1);
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("offline_drift → retry → still offline_drift → re-shows modal; cancel → no relaunch", async () => {
      // Both gate passes → offline_drift. User retries once, then cancels.
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "offline_drift" });
      prompts.askOfflineDrift.mockResolvedValueOnce("retry").mockResolvedValueOnce("cancel");

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(vi.mocked(launchGate.runLaunchGate).mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(prompts.askOfflineDrift).toHaveBeenCalledTimes(2);
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("sync_failed → fallback confirm; OK → relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "sync_failed", message: "no device" });
      prompts.confirmFallbackLaunch.mockResolvedValue(true);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(prompts.confirmFallbackLaunch).toHaveBeenCalledWith("no device");
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("sync_failed → cancel → no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "sync_failed", message: "no device" });
      prompts.confirmFallbackLaunch.mockResolvedValue(false);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("migration_pending block → migration toast, no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "block", reason: "migration_pending" });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(toaster.toast).toHaveBeenCalledWith({
        title: "Tender",
        body: "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
      });
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("no_launch_target block → explains the download was kept, no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "block", reason: "no_launch_target" });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(toaster.toast).toHaveBeenCalledWith({
        title: "Tender",
        body: "This download has no file the emulator can launch. The files are on disk — see the game page.",
      });
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("abort → no toast, no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "abort" });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(toaster.toast).not.toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });
  });

  // ---------------------------------------------------------------------------
  // #1152 — the watcher's relaunch path re-confirms launch_options just before
  // RunGame, mirroring the Play-button funnel via the shared
  // `reconfirmLaunchOptions` helper. Null/rejected responses remain best-effort;
  // a timeout or stale plugin admission aborts the relaunch.
  // ---------------------------------------------------------------------------
  describe("relaunch launch_options re-confirm (#1152)", () => {
    const RELAUNCH_COMMAND = 'flatpak run net.retrodeck.retrodeck "/roms/snes/g.rom"';

    it("allow → re-confirms (getRomRelaunchOptions → setLaunchOptionsConfirmed) BEFORE RunGame", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
      vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
        success: true,
        app_id: 1234,
        launch_options: RELAUNCH_COMMAND,
        prune_lease_token: "launch-lease",
      });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42);
      expect(steamShortcuts.setLaunchOptionsConfirmed).toHaveBeenCalledWith(1234, RELAUNCH_COMMAND);
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      // Order: getRomRelaunchOptions → setLaunchOptionsConfirmed → RunGame.
      const getOrder = vi.mocked(backend.getRomRelaunchOptions).mock.invocationCallOrder[0]!;
      const setOrder = vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mock.invocationCallOrder[0]!;
      const runOrder = vi.mocked(SteamClient.Apps.RunGame).mock.invocationCallOrder[0]!;
      expect(getOrder).toBeLessThan(setOrder);
      expect(setOrder).toBeLessThan(runOrder);
    });

    it("markLaunchSkipped fires immediately before RunGame (re-confirm doesn't disturb the skip→run order)", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
      vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
        success: true,
        app_id: 1234,
        launch_options: RELAUNCH_COMMAND,
        prune_lease_token: "launch-lease",
      });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      // markLaunchSkipped(1234) ran before RunGame, so the relaunch is exempt:
      // consuming the skip now returns true (the real skip-set carries it).
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      expect(launchGate.consumeLaunchSkip(1234)).toBe(true);
    });

    it("a null item skips setLaunchOptionsConfirmed but STILL relaunches", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
      vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue(null);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42);
      expect(steamShortcuts.setLaunchOptionsConfirmed).not.toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("a rejected re-confirm logs with the Watcher context AND still relaunches (non-vacuous)", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
      vi.mocked(backend.getRomRelaunchOptions).mockRejectedValue(new Error("offline"));

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      // Post-catch: no set, the failure was logged with the helper's Watcher
      // prefix, and the relaunch still fired (best-effort).
      expect(steamShortcuts.setLaunchOptionsConfirmed).not.toHaveBeenCalled();
      expect(backend.logError).toHaveBeenCalledWith(
        expect.stringContaining("Watcher: launch_options re-confirm failed"),
      );
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("a timed-out re-confirm keeps the cancelled watcher launch blocked and says so", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(backend.getRomRelaunchOptions).mockReturnValue(new Promise<never>(() => {}));
        register();
        captureHandler()(77, "1234", "LaunchApp", 0);

        await vi.advanceTimersByTimeAsync(0);
        expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42);
        await vi.advanceTimersByTimeAsync(3000);

        expect(backend.logError).toHaveBeenCalledWith(
          expect.stringContaining("Watcher: launch_options re-confirm timed out"),
        );
        expect(steamShortcuts.setLaunchOptionsConfirmed).not.toHaveBeenCalled();
        expect(runGameMock()).not.toHaveBeenCalled();
        // The watcher owns no UI, so without this toast the press dies silently.
        expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "Launch cancelled — try again" });
      } finally {
        vi.useRealTimers();
      }
    });

    it("a lifecycle-cancelled re-confirm stays silent (no launch-cancelled toast)", async () => {
      let remounted = false;
      vi.mocked(backend.getRomRelaunchOptions).mockReturnValue(new Promise<never>(() => {}));
      try {
        register();
        captureHandler()(77, "1234", "LaunchApp", 0);
        await flush();
        expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42);
        vi.mocked(toaster.toast).mockClear();

        await releaseAllPruneLeases();
        mountPruneLeasePlugin();
        remounted = true;
        await flush();

        // Teardown is not a refused launch — only the timeout branch reports.
        expect(toaster.toast).not.toHaveBeenCalled();
        expect(runGameMock()).not.toHaveBeenCalled();
      } finally {
        if (!remounted) mountPruneLeasePlugin();
      }
    });

    it("plugin teardown while re-confirm is pending releases a late token and never calls RunGame", async () => {
      let resolveFetch!: (value: Awaited<ReturnType<typeof backend.getRomRelaunchOptions>>) => void;
      let remounted = false;
      vi.mocked(backend.getRomRelaunchOptions).mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveFetch = resolve;
          }),
      );

      try {
        register();
        captureHandler()(77, "1234", "LaunchApp", 0);
        await flush();
        expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42);

        await releaseAllPruneLeases();
        mountPruneLeasePlugin();
        remounted = true;
        resolveFetch({
          success: true,
          app_id: 1234,
          launch_options: RELAUNCH_COMMAND,
          prune_lease_token: "late-watcher-launch-lease",
        });
        await flush();

        expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("late-watcher-launch-lease");
        expect(steamShortcuts.setLaunchOptionsConfirmed).not.toHaveBeenCalled();
        expect(runGameMock()).not.toHaveBeenCalled();
      } finally {
        if (!remounted) mountPruneLeasePlugin();
      }
    });

    it("conflict resolved → re-confirms then relaunches (shared path covers every relaunch branch)", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "conflict", conflicts: [conflict()] });
      prompts.resolveConflicts.mockResolvedValue("resolved");
      vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
        success: true,
        app_id: 1234,
        launch_options: RELAUNCH_COMMAND,
        prune_lease_token: "launch-lease",
      });

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(steamShortcuts.setLaunchOptionsConfirmed).toHaveBeenCalledWith(1234, RELAUNCH_COMMAND);
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("unknown appId relaunches WITHOUT a re-confirm (no romId to resolve)", async () => {
      vi.mocked(sessionManager.getAppIdRomIdMapSnapshot).mockReturnValue({});

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(backend.getRomRelaunchOptions).not.toHaveBeenCalled();
      expect(steamShortcuts.setLaunchOptionsConfirmed).not.toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });
  });

  describe("error fallback", () => {
    it("relaunches (never traps) when the gate throws", async () => {
      vi.mocked(launchGate.runLaunchGate).mockRejectedValue(new Error("boom"));

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      // The interceptor's own `.catch(() => allow)` on runLaunchGate maps a
      // throw to the allow verdict → relaunch. RunGame must have fired.
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });
  });

  describe("auto-adopt tracking variant", () => {
    it("does NOT dispatch romm_tab_switch and auto-confirms the default slot", async () => {
      // Drive the REAL gate op: runLaunchGate invokes ensureTrackingConfigured,
      // exercising the watcher's silent auto-adopt path.
      vi.mocked(launchGate.runLaunchGate).mockImplementation(
        async (_appId: number, _romId: number, ops: LaunchGateOps): Promise<GateVerdict> => {
          await ops.ensureTrackingConfigured();
          return { decision: "allow" };
        },
      );
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: false, active_slot: null });
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValue({
        has_local_saves: false,
        local_files: [],
        server_slots: [],
        default_slot: "slot1",
        slot_confirmed: false,
        active_slot: null,
        recommended_action: "auto_confirm_default",
      });
      vi.mocked(backend.confirmSlotChoice).mockResolvedValue({ success: true, message: "" });

      const tabSwitch = vi.fn();
      globalThis.addEventListener("romm_tab_switch", tabSwitch);

      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(backend.confirmSlotChoice).toHaveBeenCalledWith(42, "slot1", false, null, false);
      expect(tabSwitch).not.toHaveBeenCalled();
      // The funnel still proceeds to a relaunch.
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      globalThis.removeEventListener("romm_tab_switch", tabSwitch);
    });
  });

  // ---------------------------------------------------------------------------
  // The watcher's gate ops (the funnel callbacks) — captured via a passthrough
  // runLaunchGate and invoked directly, so each op's happy path AND its
  // error-swallow breadcrumb are exercised. The verdict tests above stub
  // runLaunchGate entirely, so the ops only run here.
  // ---------------------------------------------------------------------------
  describe("watcher gate ops", () => {
    // Capture the ops object the watcher hands to runLaunchGate.
    async function captureOps(): Promise<LaunchGateOps> {
      let captured: LaunchGateOps | undefined;
      vi.mocked(launchGate.runLaunchGate).mockImplementation(
        async (_appId: number, _romId: number, ops: LaunchGateOps): Promise<GateVerdict> => {
          captured = ops;
          return { decision: "allow" };
        },
      );
      register();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();
      if (!captured) throw new Error("ops were not captured");
      return captured;
    }

    it("migrationPending reads the migration store", async () => {
      const ops = await captureOps();
      expect(ops.migrationPending()).toBe(false);
    });

    it("checkReachability: online passes through; a throw logs and treats as offline", async () => {
      const ops = await captureOps();

      vi.mocked(backend.probeReachability).mockResolvedValueOnce({ online: true });
      expect(await ops.checkReachability()).toBe(true);

      vi.mocked(backend.probeReachability).mockRejectedValueOnce(new Error("net"));
      expect(await ops.checkReachability()).toBe(false);
      expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("reachability probe failed"));
    });

    it("checkLocalDrift: drifted passes through; a throw logs and treats as not-drifted", async () => {
      const ops = await captureOps();

      vi.mocked(backend.checkLocalDrift).mockResolvedValueOnce({ drifted: true, rom_id: 42 });
      expect(await ops.checkLocalDrift()).toBe(true);

      vi.mocked(backend.checkLocalDrift).mockRejectedValueOnce(new Error("net"));
      expect(await ops.checkLocalDrift()).toBe(false);
      expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("local-drift check failed"));
    });

    it("checkCoreChange: unchanged proceeds; a throw logs and treats as unchanged; changed shows the modal", async () => {
      const ops = await captureOps();

      vi.mocked(backend.checkCoreChange).mockResolvedValueOnce({ changed: false });
      expect(await ops.checkCoreChange()).toBe(true);

      vi.mocked(backend.checkCoreChange).mockRejectedValueOnce(new Error("net"));
      expect(await ops.checkCoreChange()).toBe(true);
      expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("core-change check failed"));

      vi.mocked(backend.checkCoreChange).mockResolvedValueOnce({
        changed: true,
        old_label: "Old",
        new_label: "New",
      });
      prompts.confirmCoreChange.mockResolvedValueOnce(true);
      expect(await ops.checkCoreChange()).toBe(true);
      expect(prompts.confirmCoreChange).toHaveBeenCalledWith("Old", "New");
    });

    it("preLaunchSync: savefiles_in_content_dir → success; conflicts pass through; a throw → sync_failed outcome", async () => {
      const ops = await captureOps();

      vi.mocked(backend.preLaunchSync).mockResolvedValueOnce({
        success: false,
        message: "skip",
        reason: "savefiles_in_content_dir",
      });
      expect(await ops.preLaunchSync()).toEqual({ success: true, message: "skip" });

      const conflicts = [conflict()];
      vi.mocked(backend.preLaunchSync).mockResolvedValueOnce({ success: false, message: "c", conflicts });
      expect(await ops.preLaunchSync()).toEqual({ success: false, message: "c", conflicts });

      // A throw must NOT fail open — it maps to a failed outcome so the gate
      // surfaces sync_failed instead of silently allowing.
      vi.mocked(backend.preLaunchSync).mockRejectedValueOnce(new Error("boom"));
      expect(await ops.preLaunchSync()).toEqual({
        success: false,
        message: "Couldn't sync saves with RomM server.",
      });
      expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("pre-launch sync failed"));
    });

    it("ensureTrackingConfigured: already-configured proceeds; a tracking-check throw logs and proceeds", async () => {
      const ops = await captureOps();

      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValueOnce({ configured: true, active_slot: "slot1" });
      expect(await ops.ensureTrackingConfigured()).toBe("proceed");

      vi.mocked(backend.isSaveTrackingConfigured).mockRejectedValueOnce(new Error("net"));
      expect(await ops.ensureTrackingConfigured()).toBe("proceed");
      expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("tracking check failed"));
    });

    it("ensureTrackingConfigured: a getSaveSetupInfo throw logs and proceeds unconfigured", async () => {
      const ops = await captureOps();

      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValueOnce({ configured: false, active_slot: null });
      vi.mocked(backend.getSaveSetupInfo).mockRejectedValueOnce(new Error("net"));
      expect(await ops.ensureTrackingConfigured()).toBe("proceed");
      expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("save-setup fetch failed"));
    });

    it("ensureTrackingConfigured: an auto-adopt confirmSlotChoice throw logs and still proceeds", async () => {
      const ops = await captureOps();

      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValueOnce({ configured: false, active_slot: null });
      vi.mocked(backend.getSaveSetupInfo).mockResolvedValueOnce({
        has_local_saves: false,
        local_files: [],
        server_slots: [],
        default_slot: "slot1",
        slot_confirmed: false,
        active_slot: null,
        recommended_action: "auto_confirm_default",
      });
      vi.mocked(backend.confirmSlotChoice).mockRejectedValueOnce(new Error("net"));
      expect(await ops.ensureTrackingConfigured()).toBe("proceed");
      expect(backend.logError).toHaveBeenCalledWith(expect.stringContaining("auto-adopt slot failed"));
    });
  });
});
