import { describe, it, expect, beforeEach, vi } from "vitest";
import { batchConfirmLaunchOptions, reconfirmLaunchOptions } from "./launchOptionsReconcile";
import * as steamShortcuts from "./steamShortcuts";
import * as backend from "../api/backend";

vi.mock("./steamShortcuts");
vi.mock("../api/backend");

function items(n: number): { app_id: number; launch_options: string }[] {
  return Array.from({ length: n }, (_, i) => ({ app_id: i + 1, launch_options: `cmd ${i + 1}` }));
}

describe("batchConfirmLaunchOptions", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockResolvedValue(true);
    vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  });

  it("no-ops on an empty list (no confirm, no log)", async () => {
    await batchConfirmLaunchOptions([], "startup_reconcile");
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.logError)).not.toHaveBeenCalled();
  });

  it("no-ops on a non-array input (defensive guard)", async () => {
    await batchConfirmLaunchOptions(undefined as unknown as { app_id: number; launch_options: string }[], "ctx");
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.logError)).not.toHaveBeenCalled();
  });

  it("confirms every item across batches (12 items -> two batches of 10 + 2)", async () => {
    await batchConfirmLaunchOptions(items(12), "startup_reconcile");
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).toHaveBeenCalledTimes(12);
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).toHaveBeenNthCalledWith(1, 1, "cmd 1");
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).toHaveBeenNthCalledWith(12, 12, "cmd 12");
    expect(vi.mocked(backend.logError)).not.toHaveBeenCalled();
  });

  it("logs a non-vacuous error with the appId + context on a false confirm; still processes the rest", async () => {
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockImplementation((appId: number) =>
      Promise.resolve(appId !== 2),
    );
    await batchConfirmLaunchOptions(items(3), "startup_reconcile");
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).toHaveBeenCalledTimes(3);
    expect(vi.mocked(backend.logError)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.logError)).toHaveBeenCalledWith(
      "startup_reconcile: failed to confirm launch options for appId 2",
    );
  });

  it("logs a non-vacuous error with the appId + context when a confirm throws; still processes the rest", async () => {
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockImplementation((appId: number) =>
      appId === 2 ? Promise.reject(new Error("boom")) : Promise.resolve(true),
    );
    await batchConfirmLaunchOptions(items(3), "migration_relaunch_options");
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).toHaveBeenCalledTimes(3);
    expect(vi.mocked(backend.logError)).toHaveBeenCalledTimes(1);
    const msg = vi.mocked(backend.logError).mock.calls[0]![0];
    expect(msg).toContain("migration_relaunch_options: failed to set launch options for appId 2");
    expect(msg).toContain("boom");
  });

  it("does not begin a later Steam batch after cancellation", async () => {
    let settle!: () => void;
    const firstBatch = new Promise<boolean>((resolve) => {
      settle = () => resolve(true);
    });
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockReturnValue(firstBatch);
    const controller = new AbortController();
    const applying = batchConfirmLaunchOptions(items(12), "setSystemCore", controller.signal);
    await Promise.resolve();
    expect(steamShortcuts.setLaunchOptionsConfirmed).toHaveBeenCalledTimes(10);

    controller.abort();
    settle();
    await applying;

    expect(steamShortcuts.setLaunchOptionsConfirmed).toHaveBeenCalledTimes(10);
  });
});

describe("reconfirmLaunchOptions", () => {
  const RELAUNCH_COMMAND = 'flatpak run net.retrodeck.retrodeck "/roms/gba/pokemon.gba"';

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockResolvedValue(true);
  });

  it("confirm-sets the resolved command onto the appId when an item is returned", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
      success: true,
      app_id: 100,
      launch_options: RELAUNCH_COMMAND,
      prune_lease_token: "launch-lease",
    });

    await expect(reconfirmLaunchOptions(42, 100, "CustomPlayButton")).resolves.toEqual({ status: "ready" });

    expect(vi.mocked(backend.getRomRelaunchOptions)).toHaveBeenCalledWith(42);
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).toHaveBeenCalledWith(100, RELAUNCH_COMMAND);
    expect(vi.mocked(backend.logError)).not.toHaveBeenCalled();
  });

  it("skips the confirm-set on a null item but does not throw or log", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue(null);

    await expect(reconfirmLaunchOptions(42, 100, "Watcher")).resolves.toEqual({ status: "ready" });

    expect(vi.mocked(backend.getRomRelaunchOptions)).toHaveBeenCalledWith(42);
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.logError)).not.toHaveBeenCalled();
  });

  it("logs a non-vacuous error with the context prefix when the fetch rejects; never throws", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockRejectedValue(new Error("offline"));

    await expect(reconfirmLaunchOptions(42, 100, "Watcher")).resolves.toEqual({
      status: "best_effort_failure",
    });

    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    const msg = vi.mocked(backend.logError).mock.calls[0]![0];
    expect(msg).toContain("Watcher: launch_options re-confirm failed");
    expect(msg).toContain("offline");
  });

  it("carries the caller's context prefix (CustomPlayButton) into the failure log", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockRejectedValue(new Error("boom"));

    await expect(reconfirmLaunchOptions(42, 100, "CustomPlayButton")).resolves.toEqual({
      status: "best_effort_failure",
    });

    expect(vi.mocked(backend.logError)).toHaveBeenCalledWith(
      expect.stringContaining("CustomPlayButton: launch_options re-confirm failed"),
    );
  });

  it("keeps a Steam write failure best-effort and releases its lease", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
      success: true,
      app_id: 100,
      launch_options: RELAUNCH_COMMAND,
      prune_lease_token: "write-failure-lease",
    });
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockRejectedValue(new Error("steam unavailable"));

    await expect(reconfirmLaunchOptions(42, 100, "Watcher")).resolves.toEqual({
      status: "best_effort_failure",
    });

    expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("write-failure-lease");
    expect(backend.logError).toHaveBeenCalledWith(
      expect.stringContaining("Watcher: launch_options re-confirm failed (launching anyway)"),
    );
  });

  it("a hung fetch returns a distinct timeout without starting a Steam write", async () => {
    vi.useFakeTimers();
    try {
      // Never resolves — simulates a wedged backend / hung callable bridge.
      vi.mocked(backend.getRomRelaunchOptions).mockReturnValue(new Promise<never>(() => {}));

      const pending = reconfirmLaunchOptions(42, 100, "CustomPlayButton");
      // Advancing past the 3s race fires the timeout reject without a real wait.
      await vi.advanceTimersByTimeAsync(3000);
      await expect(pending).resolves.toEqual({ status: "timeout" });

      expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.logError)).toHaveBeenCalledWith(
        expect.stringContaining("CustomPlayButton: launch_options re-confirm timed out"),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("observes a lease-bearing response that arrives after timeout and releases it without a Steam write", async () => {
    vi.useFakeTimers();
    try {
      let resolveFetch!: (value: Awaited<ReturnType<typeof backend.getRomRelaunchOptions>>) => void;
      vi.mocked(backend.getRomRelaunchOptions).mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveFetch = resolve;
          }),
      );

      const pending = reconfirmLaunchOptions(42, 100, "Watcher");
      await vi.advanceTimersByTimeAsync(3000);
      await expect(pending).resolves.toEqual({ status: "timeout" });
      expect(backend.releasePruneConflictLease).not.toHaveBeenCalled();

      resolveFetch({
        success: true,
        app_id: 100,
        launch_options: RELAUNCH_COMMAND,
        prune_lease_token: "late-timeout-lease",
      });
      await vi.advanceTimersByTimeAsync(0);

      expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("late-timeout-lease");
      expect(steamShortcuts.setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
