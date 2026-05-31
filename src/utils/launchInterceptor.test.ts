import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { toaster } from "@decky/api";
import * as backend from "../api/backend";
import * as gameDetailPatch from "../patches/gameDetailPatch";
import * as migrationStore from "./migrationStore";
import { registerLaunchInterceptor, unregisterLaunchInterceptor } from "./launchInterceptor";
import type { LaunchVerdict } from "../types";

// The interceptor pulls in `../patches/gameDetailPatch` which transitively
// imports from `@decky/ui` and `react`. The global `@decky/ui` mock in
// test-setup.ts covers most of it, but `routerHook`/`afterPatch` etc. are
// pulled by the patch module's top-level imports. To keep this test focused
// on the interceptor branches, mock the gameDetailPatch surface we touch.
vi.mock("../patches/gameDetailPatch", () => ({
  isRomMAppId: vi.fn(),
}));

vi.mock("../api/backend", () => ({
  evaluateLaunch: vi.fn(),
  refreshMigrationState: vi.fn(),
  logInfo: vi.fn(),
  logError: vi.fn(),
}));

vi.mock("./migrationStore", () => ({
  getMigrationState: vi.fn(),
  setMigrationStatus: vi.fn(),
}));

vi.mock("./saveSortMigrationStore", () => ({
  setSaveSortMigrationStatus: vi.fn(),
}));

type GameActionHandler = (
  gameActionId: number,
  appIdStr: string,
  action: string,
  launchSource: number,
) => Promise<void>;

const captureHandler = (): GameActionHandler => {
  const calls = vi.mocked(SteamClient.Apps.RegisterForGameActionStart).mock.calls;
  const handler = calls[calls.length - 1]?.[0];
  if (!handler) throw new Error("RegisterForGameActionStart was not called");
  return handler as GameActionHandler;
};

const makeVerdict = (overrides: Partial<LaunchVerdict> = {}): LaunchVerdict => ({
  action: "allow",
  reason: null,
  toast_title: null,
  toast_body: null,
  ...overrides,
});

describe("launchInterceptor", () => {
  let unregisterMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();

    unregisterMock = vi.fn();
    vi.stubGlobal("SteamClient", {
      Apps: {
        RegisterForGameActionStart: vi.fn(() => ({ unregister: unregisterMock })),
        CancelGameAction: vi.fn(),
      },
    });

    vi.mocked(gameDetailPatch.isRomMAppId).mockReturnValue(true);
    vi.mocked(migrationStore.getMigrationState).mockReturnValue({
      pending: false,
      reason: null,
    } as unknown as ReturnType<typeof migrationStore.getMigrationState>);
    vi.mocked(backend.refreshMigrationState).mockResolvedValue({
      retrodeck: { pending: false, reason: null },
      save_sort: { pending: false, reason: null },
    } as unknown as Awaited<ReturnType<typeof backend.refreshMigrationState>>);
  });

  afterEach(() => {
    unregisterLaunchInterceptor();
  });

  describe("warn action", () => {
    it("toasts but does NOT cancel the launch when verdict.action is 'warn'", async () => {
      vi.mocked(backend.evaluateLaunch).mockResolvedValueOnce(
        makeVerdict({
          action: "warn",
          reason: "save_status_failed",
          toast_title: "Save check failed",
          toast_body: "Could not verify save state; launching anyway.",
        }),
      );

      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(42, "1234", "LaunchApp", 0);

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(toaster.toast).toHaveBeenCalledOnce();
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "Save check failed",
        body: "Could not verify save state; launching anyway.",
      });
    });

    it("does not toast on 'warn' when toast fields are null", async () => {
      vi.mocked(backend.evaluateLaunch).mockResolvedValueOnce(
        makeVerdict({ action: "warn", reason: "save_status_failed" }),
      );

      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(42, "1234", "LaunchApp", 0);

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(toaster.toast).not.toHaveBeenCalled();
    });
  });

  describe("block action (regression)", () => {
    it("cancels the game action and toasts when verdict.action is 'block'", async () => {
      vi.mocked(backend.evaluateLaunch).mockResolvedValueOnce(
        makeVerdict({
          action: "block",
          reason: "not_installed",
          toast_title: "Not installed",
          toast_body: "Download the ROM first.",
        }),
      );

      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(99, "1234", "LaunchApp", 0);

      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(99);
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "Not installed",
        body: "Download the ROM first.",
      });
    });

    it("cancels without toast when 'block' has no toast fields", async () => {
      vi.mocked(backend.evaluateLaunch).mockResolvedValueOnce(
        makeVerdict({ action: "block", reason: "save_conflict" }),
      );

      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(7, "1234", "LaunchApp", 0);

      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(7);
      expect(toaster.toast).not.toHaveBeenCalled();
    });
  });

  describe("allow action (regression)", () => {
    it("neither cancels nor toasts when verdict.action is 'allow'", async () => {
      vi.mocked(backend.evaluateLaunch).mockResolvedValueOnce(makeVerdict({ action: "allow" }));

      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(1, "1234", "LaunchApp", 0);

      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
      expect(toaster.toast).not.toHaveBeenCalled();
    });
  });

  describe("short-circuit guards", () => {
    it("ignores non-LaunchApp actions", async () => {
      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(1, "1234", "QuitApp", 0);

      expect(backend.evaluateLaunch).not.toHaveBeenCalled();
      expect(SteamClient.Apps.CancelGameAction).not.toHaveBeenCalled();
    });

    it("ignores non-RomM app IDs", async () => {
      vi.mocked(gameDetailPatch.isRomMAppId).mockReturnValue(false);
      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(1, "9999", "LaunchApp", 0);

      expect(backend.evaluateLaunch).not.toHaveBeenCalled();
    });

    it("blocks launch when a RetroDECK migration is pending", async () => {
      vi.mocked(migrationStore.getMigrationState).mockReturnValue({
        pending: true,
        reason: null,
      } as unknown as ReturnType<typeof migrationStore.getMigrationState>);

      registerLaunchInterceptor();
      const handler = captureHandler();
      await handler(55, "1234", "LaunchApp", 0);

      expect(SteamClient.Apps.CancelGameAction).toHaveBeenCalledWith(55);
      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
      });
      expect(backend.evaluateLaunch).not.toHaveBeenCalled();
    });
  });
});
