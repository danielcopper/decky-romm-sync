import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as backend from "../api/backend";
import { setRommConnectionState, getRommConnectionState } from "./connectionState";
import { registerOfflineRecovery, OFFLINE_RECOVERY_INTERVAL_MS } from "./offlineRecovery";

vi.mock("../api/backend", () => ({
  probeReachability: vi.fn(),
  debugLog: vi.fn().mockResolvedValue(undefined),
}));

describe("offlineRecovery probe (#1345)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(backend.probeReachability).mockReset();
    setRommConnectionState("checking");
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does NOT probe while connected (no polling when online)", async () => {
    setRommConnectionState("connected");
    const stop = registerOfflineRecovery();
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS * 3);
    expect(vi.mocked(backend.probeReachability)).not.toHaveBeenCalled();
    stop();
  });

  it("re-probes every ~30s while offline and a page is registered", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    setRommConnectionState("offline");
    const stop = registerOfflineRecovery();

    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(2);
    stop();
  });

  it("flips the store to connected on a successful probe and stops probing", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    setRommConnectionState("offline");
    const stop = registerOfflineRecovery();

    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS);
    expect(getRommConnectionState()).toBe("connected");
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(1);

    // Now connected → the timer stopped, so no further probes fire.
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS * 2);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(1);
    stop();
  });

  it("keeps a single shared timer across multiple registrations", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    setRommConnectionState("offline");
    const stopA = registerOfflineRecovery();
    const stopB = registerOfflineRecovery();

    // One tick → exactly ONE probe despite two registered pages (no stacking).
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(1);

    // First page unmounts — the other still holds the timer alive.
    stopA();
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(2);

    // Last page unmounts — the timer stops, no more probes.
    stopB();
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS * 2);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(2);
  });

  it("stops probing after the registration is cleaned up", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    setRommConnectionState("offline");
    const stop = registerOfflineRecovery();
    stop();
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS * 3);
    expect(vi.mocked(backend.probeReachability)).not.toHaveBeenCalled();
  });

  it("starts probing when the store flips to offline while a page is registered", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    // Registered while connected → no timer yet.
    setRommConnectionState("connected");
    const stop = registerOfflineRecovery();
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).not.toHaveBeenCalled();

    // Store flips offline (e.g. a failed call) → the timer starts.
    setRommConnectionState("offline");
    await vi.advanceTimersByTimeAsync(OFFLINE_RECOVERY_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(1);
    stop();
  });
});
