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
});

describe("reconfirmLaunchOptions", () => {
  const RELAUNCH_COMMAND = 'flatpak run net.retrodeck.retrodeck "/roms/gba/pokemon.gba"';

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(steamShortcuts.setLaunchOptionsConfirmed).mockResolvedValue(true);
  });

  it("confirm-sets the resolved command onto the appId when an item is returned", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({ app_id: 100, launch_options: RELAUNCH_COMMAND });

    await reconfirmLaunchOptions(42, 100, "CustomPlayButton");

    expect(vi.mocked(backend.getRomRelaunchOptions)).toHaveBeenCalledWith(42);
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).toHaveBeenCalledWith(100, RELAUNCH_COMMAND);
    expect(vi.mocked(backend.logError)).not.toHaveBeenCalled();
  });

  it("skips the confirm-set on a null item but does not throw or log", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue(null);

    await expect(reconfirmLaunchOptions(42, 100, "Watcher")).resolves.toBeUndefined();

    expect(vi.mocked(backend.getRomRelaunchOptions)).toHaveBeenCalledWith(42);
    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.logError)).not.toHaveBeenCalled();
  });

  it("logs a non-vacuous error with the context prefix when the fetch rejects; never throws", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockRejectedValue(new Error("offline"));

    await expect(reconfirmLaunchOptions(42, 100, "Watcher")).resolves.toBeUndefined();

    expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    const msg = vi.mocked(backend.logError).mock.calls[0]![0];
    expect(msg).toContain("Watcher: launch_options re-confirm failed");
    expect(msg).toContain("offline");
  });

  it("carries the caller's context prefix (CustomPlayButton) into the failure log", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockRejectedValue(new Error("boom"));

    await reconfirmLaunchOptions(42, 100, "CustomPlayButton");

    expect(vi.mocked(backend.logError)).toHaveBeenCalledWith(
      expect.stringContaining("CustomPlayButton: launch_options re-confirm failed"),
    );
  });

  it("a hung fetch falls through after the 3s timeout: no set, logged, resolves (never hangs the caller)", async () => {
    vi.useFakeTimers();
    try {
      // Never resolves — simulates a wedged backend / hung callable bridge.
      vi.mocked(backend.getRomRelaunchOptions).mockReturnValue(new Promise<never>(() => {}));

      const pending = reconfirmLaunchOptions(42, 100, "CustomPlayButton");
      // Advancing past the 3s race fires the timeout reject without a real wait.
      await vi.advanceTimersByTimeAsync(3000);
      await expect(pending).resolves.toBeUndefined();

      expect(vi.mocked(steamShortcuts.setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.logError)).toHaveBeenCalledWith(
        expect.stringContaining("CustomPlayButton: launch_options re-confirm failed"),
      );
    } finally {
      vi.useRealTimers();
    }
  });
});
