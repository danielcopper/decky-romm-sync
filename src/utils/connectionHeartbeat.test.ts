import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as backend from "../api/backend";
import { setRommConnectionState, getRommConnectionState } from "./connectionState";
import { registerConnectionHeartbeat, CONNECTION_HEARTBEAT_INTERVAL_MS } from "./connectionHeartbeat";

vi.mock("../api/backend", () => ({
  probeReachability: vi.fn(),
  debugLog: vi.fn().mockResolvedValue(undefined),
}));

describe("connection heartbeat (#1345)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(backend.probeReachability).mockReset();
    setRommConnectionState("checking");
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("probes while connected and flips the store offline on a failed probe", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    setRommConnectionState("connected");
    const stop = registerConnectionHeartbeat();
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS);
    expect(getRommConnectionState()).toBe("offline");
    stop();
  });

  it("flips the store back to connected when a probe succeeds while offline", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    setRommConnectionState("offline");
    const stop = registerConnectionHeartbeat();
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS);
    expect(getRommConnectionState()).toBe("connected");
    stop();
  });

  it("keeps probing on both sides of the state (detection AND recovery)", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    setRommConnectionState("connected");
    const stop = registerConnectionHeartbeat();
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS);
    expect(getRommConnectionState()).toBe("offline");

    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS);
    expect(getRommConnectionState()).toBe("connected");
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(2);
    stop();
  });

  it("a rejected probe call is no verdict — the store keeps its state", async () => {
    vi.mocked(backend.probeReachability).mockRejectedValue(new Error("bridge down"));
    setRommConnectionState("connected");
    const stop = registerConnectionHeartbeat();
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS);
    expect(getRommConnectionState()).toBe("connected");
    stop();
  });

  it("keeps a single shared timer across multiple registrations", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    const stopA = registerConnectionHeartbeat();
    const stopB = registerConnectionHeartbeat();

    // One tick → exactly ONE probe despite two registered pages (no stacking).
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(1);

    // First page unmounts — the other still holds the timer alive.
    stopA();
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(2);

    // Last page unmounts — the timer stops, no more probes.
    stopB();
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS * 2);
    expect(vi.mocked(backend.probeReachability)).toHaveBeenCalledTimes(2);
  });

  it("no polling without a mounted page", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    const stop = registerConnectionHeartbeat();
    stop();
    await vi.advanceTimersByTimeAsync(CONNECTION_HEARTBEAT_INTERVAL_MS * 3);
    expect(vi.mocked(backend.probeReachability)).not.toHaveBeenCalled();
  });
});
