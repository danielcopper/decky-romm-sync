import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import * as gameDetailPatch from "../patches/gameDetailPatch";
import * as launchGate from "./launchGate";
import * as sessionManager from "./sessionManager";
import * as syncConflictModal from "../components/SyncConflictModal";
import * as offlineDriftModal from "../components/OfflineDriftModal";
import * as fallbackLaunchModal from "../components/FallbackLaunchModal";
import * as coreChangeModal from "../components/CoreChangeModal";
import * as steamShortcuts from "./steamShortcuts";
import { registerLaunchInterceptor, unregisterLaunchInterceptor } from "./launchInterceptor";
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
  logInfo: vi.fn(),
  logError: vi.fn(),
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
}));

vi.mock("./migrationStore", () => ({
  getMigrationState: vi.fn(() => ({ pending: false })),
  setMigrationStatus: vi.fn(),
}));

vi.mock("./saveSortMigrationStore", () => ({
  setSaveSortMigrationStatus: vi.fn(),
}));

vi.mock("../components/SyncConflictModal", () => ({
  handleConflicts: vi.fn(),
}));

vi.mock("../components/OfflineDriftModal", () => ({
  showOfflineDriftModal: vi.fn(),
}));

vi.mock("../components/FallbackLaunchModal", () => ({
  showFallbackLaunchModal: vi.fn(),
}));

vi.mock("../components/CoreChangeModal", () => ({
  showCoreChangeModal: vi.fn(),
}));

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
    });
    // Skip-set empty by default — a marked appId is set per-test.
    vi.mocked(sessionManager.getAppIdRomIdMapSnapshot).mockReturnValue({ "1234": 42 });
    vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
    // The shared relaunch re-confirm (#1152) runs on every relaunch; default it
    // to a resolved command + a clean confirm-set so the existing verdict tests
    // exercise the happy path without per-test wiring.
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({ app_id: 1234, launch_options: "flatpak run x" });
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockResolvedValue(true);
  });

  afterEach(() => {
    unregisterLaunchInterceptor();
  });

  describe("entry guards", () => {
    it("ignores non-LaunchApp actions — no cancel, no gate", async () => {
      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(1, "1234", "QuitApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
    });

    it("ignores non-RomM app IDs — no cancel, no gate", async () => {
      vi.mocked(gameDetailPatch.isRomMAppId).mockReturnValue(false);
      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(1, "9999", "LaunchApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
    });

    it("skips a marked appId WITHOUT cancelling or gating", async () => {
      // Pre-mark appId 1234 via the real skip-set.
      launchGate.markLaunchSkipped(1234);
      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(99, "1234", "LaunchApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
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

      registerLaunchInterceptor();
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
      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(77);
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "ROM not downloaded. Open the plugin to download it first.",
      });
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("relaunches without gating when the appId is unknown to the session map", async () => {
      vi.mocked(sessionManager.getAppIdRomIdMapSnapshot).mockReturnValue({});
      registerLaunchInterceptor();
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

      registerLaunchInterceptor();
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

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "ROM not downloaded. Open the plugin to download it first.",
      });
      expect(launchGate.runLaunchGate).not.toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });
  });

  describe("verdict handling", () => {
    it("allow → relaunches via RunGame and marks the appId skipped", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
      registerLaunchInterceptor();
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
      vi.mocked(syncConflictModal.handleConflicts).mockResolvedValue("resolved");
      const dataChanged = vi.fn();
      globalThis.addEventListener("romm_data_changed", dataChanged);

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(syncConflictModal.handleConflicts).toHaveBeenCalledWith(conflicts);
      expect(dataChanged).toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
      globalThis.removeEventListener("romm_data_changed", dataChanged);
    });

    it("conflict → cancelled → no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "conflict", conflicts: [conflict()] });
      vi.mocked(syncConflictModal.handleConflicts).mockResolvedValue("cancel");

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(syncConflictModal.handleConflicts).toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("offline_drift → OfflineDriftModal shown; start_anyway → relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "offline_drift" });
      vi.mocked(offlineDriftModal.showOfflineDriftModal).mockResolvedValue("start_anyway");

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(offlineDriftModal.showOfflineDriftModal).toHaveBeenCalled();
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("offline_drift → cancel → no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "offline_drift" });
      vi.mocked(offlineDriftModal.showOfflineDriftModal).mockResolvedValue("cancel");

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(offlineDriftModal.showOfflineDriftModal).toHaveBeenCalled();
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("offline_drift → retry → re-runs the gate; now allow → relaunch", async () => {
      // First gate pass → offline_drift; user retries. Second gate pass → allow.
      vi.mocked(launchGate.runLaunchGate)
        .mockResolvedValueOnce({ decision: "offline_drift" })
        .mockResolvedValue({ decision: "allow" });
      vi.mocked(offlineDriftModal.showOfflineDriftModal).mockResolvedValueOnce("retry");

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      // Non-vacuous: the gate RE-RAN (called twice) on retry, and the now-allow
      // verdict relaunched.
      expect(vi.mocked(launchGate.runLaunchGate).mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(offlineDriftModal.showOfflineDriftModal).toHaveBeenCalledTimes(1);
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("offline_drift → retry → still offline_drift → re-shows modal; cancel → no relaunch", async () => {
      // Both gate passes → offline_drift. User retries once, then cancels.
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "offline_drift" });
      vi.mocked(offlineDriftModal.showOfflineDriftModal).mockResolvedValueOnce("retry").mockResolvedValueOnce("cancel");

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(vi.mocked(launchGate.runLaunchGate).mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(offlineDriftModal.showOfflineDriftModal).toHaveBeenCalledTimes(2);
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("sync_failed → fallback confirm; OK → relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "sync_failed", message: "no device" });
      vi.mocked(fallbackLaunchModal.showFallbackLaunchModal).mockResolvedValue(true);

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(fallbackLaunchModal.showFallbackLaunchModal).toHaveBeenCalledWith("no device");
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("sync_failed → cancel → no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "sync_failed", message: "no device" });
      vi.mocked(fallbackLaunchModal.showFallbackLaunchModal).mockResolvedValue(false);

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("migration_pending block → migration toast, no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "block", reason: "migration_pending" });

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
      });
      expect(runGameMock()).not.toHaveBeenCalled();
    });

    it("abort → no toast, no relaunch", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "abort" });

      registerLaunchInterceptor();
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
  // `reconfirmLaunchOptions` helper. Best-effort: a null/rejected/hung re-confirm
  // still relaunches (the launch must never be trapped).
  // ---------------------------------------------------------------------------
  describe("relaunch launch_options re-confirm (#1152)", () => {
    const RELAUNCH_COMMAND = 'flatpak run net.retrodeck.retrodeck "/roms/snes/g.rom"';

    it("allow → re-confirms (getRomRelaunchOptions → setLaunchOptionsConfirmed) BEFORE RunGame", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "allow" });
      vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({ app_id: 1234, launch_options: RELAUNCH_COMMAND });

      registerLaunchInterceptor();
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
      vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({ app_id: 1234, launch_options: RELAUNCH_COMMAND });

      registerLaunchInterceptor();
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

      registerLaunchInterceptor();
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

      registerLaunchInterceptor();
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

    it("conflict resolved → re-confirms then relaunches (shared path covers every relaunch branch)", async () => {
      vi.mocked(launchGate.runLaunchGate).mockResolvedValue({ decision: "conflict", conflicts: [conflict()] });
      vi.mocked(syncConflictModal.handleConflicts).mockResolvedValue("resolved");
      vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({ app_id: 1234, launch_options: RELAUNCH_COMMAND });

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(steamShortcuts.setLaunchOptionsConfirmed).toHaveBeenCalledWith(1234, RELAUNCH_COMMAND);
      expect(runGameMock()).toHaveBeenCalledWith("gid-7", "", -1, 100);
    });

    it("unknown appId relaunches WITHOUT a re-confirm (no romId to resolve)", async () => {
      vi.mocked(sessionManager.getAppIdRomIdMapSnapshot).mockReturnValue({});

      registerLaunchInterceptor();
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

      registerLaunchInterceptor();
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

      registerLaunchInterceptor();
      const handler = captureHandler();
      handler(77, "1234", "LaunchApp", 0);
      await flush();

      expect(backend.confirmSlotChoice).toHaveBeenCalledWith(42, "slot1", false, null);
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
      registerLaunchInterceptor();
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
      vi.mocked(coreChangeModal.showCoreChangeModal).mockResolvedValueOnce(true);
      expect(await ops.checkCoreChange()).toBe(true);
      expect(coreChangeModal.showCoreChangeModal).toHaveBeenCalledWith("Old", "New");
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
