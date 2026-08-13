import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getSettings, testConnection } from "../api/backend";
import {
  ensureConnectionProbe,
  getConnectionProbeState,
  onConnectionProbeChange,
  resetConnectionProbeForTests,
} from "./connectionProbe";

vi.mock("../api/backend", () => ({
  testConnection: vi.fn(),
  getSettings: vi.fn(),
  debugLog: vi.fn(() => Promise.resolve()),
}));

// The full ladder: six attempts at a 5s deadline each, with 2+5+10+15+20s of
// backoff between them, then the liveness ping.
const FULL_LADDER_MS = 6 * 5000 + 2000 + 5000 + 10000 + 15000 + 20000 + 5000;

beforeEach(() => {
  vi.resetAllMocks();
  resetConnectionProbeForTests();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("connectionProbe", () => {
  it("publishes the verdict of a resolved probe", async () => {
    vi.mocked(testConnection).mockResolvedValue({ success: true, message: "" });
    ensureConnectionProbe();
    await vi.advanceTimersByTimeAsync(0);
    expect(getConnectionProbeState()).toEqual({ connected: true, failure: null });
  });

  it("carries a resolved failure's reason and message", async () => {
    vi.mocked(testConnection).mockResolvedValue({
      success: false,
      reason: "config_error",
      message: "Not signed in — sign in to RomM first",
    });
    ensureConnectionProbe();
    await vi.advanceTimersByTimeAsync(0);
    expect(getConnectionProbeState()).toEqual({
      connected: false,
      failure: { reason: "config_error", message: "Not signed in — sign in to RomM first" },
    });
  });

  // The #1730 regression: the probe used to be owned by the QAM panel's effect,
  // so closing the panel cancelled the run and reopening restarted it at attempt
  // 0. Nobody holds the QAM open for the ~87s the ladder needs, so the verdict
  // was unreachable in practice and the row read "Checking…" forever.
  it("runs to a verdict even when every subscriber unsubscribes mid-run", async () => {
    vi.mocked(testConnection).mockRejectedValue(new Error("backend down"));
    vi.mocked(getSettings).mockImplementation(() => new Promise(() => {}));
    // A dead bridge is reported to the console rather than through logError,
    // which is itself a callable and would hang.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});

    const unsubscribe = onConnectionProbeChange(vi.fn());
    ensureConnectionProbe();
    // Close the panel a couple of seconds in — long before any verdict.
    await vi.advanceTimersByTimeAsync(6000);
    unsubscribe();
    expect(getConnectionProbeState().connected).toBeNull();

    await vi.advanceTimersByTimeAsync(FULL_LADDER_MS);
    expect(getConnectionProbeState().connected).toBe("backend_failed");
    expect(consoleError).toHaveBeenCalledWith(
      "[RomM] backend RPC bridge unreachable (get_settings ping failed):",
      expect.anything(),
    );
    consoleError.mockRestore();
  });

  it("hands the stored verdict to a subscriber that arrives after the run ended", async () => {
    vi.mocked(testConnection).mockResolvedValue({ success: true, message: "" });
    ensureConnectionProbe();
    await vi.advanceTimersByTimeAsync(0);

    // A panel mounting now reads the verdict straight out of the store rather
    // than starting from "Checking…".
    expect(getConnectionProbeState().connected).toBe(true);
  });

  it("collapses concurrent asks into the single run in flight", async () => {
    vi.mocked(testConnection).mockImplementation(() => new Promise(() => {}));
    ensureConnectionProbe();
    ensureConnectionProbe();
    ensureConnectionProbe();
    await vi.advanceTimersByTimeAsync(0);
    expect(testConnection).toHaveBeenCalledTimes(1);
  });

  it("re-probes once a run has ended so a recovered backend updates the verdict", async () => {
    vi.mocked(testConnection).mockResolvedValue({ success: false, reason: "server_unreachable", message: "down" });
    ensureConnectionProbe();
    await vi.advanceTimersByTimeAsync(0);
    expect(getConnectionProbeState().connected).toBe(false);

    vi.mocked(testConnection).mockResolvedValue({ success: true, message: "" });
    ensureConnectionProbe();
    await vi.advanceTimersByTimeAsync(0);
    expect(testConnection).toHaveBeenCalledTimes(2);
    expect(getConnectionProbeState()).toEqual({ connected: true, failure: null });
  });

  it("reports a merely-unreachable server as 'not connected' when the RPC bridge still answers", async () => {
    vi.mocked(testConnection).mockRejectedValue(new Error("server hung"));
    vi.mocked(getSettings).mockResolvedValue({} as Awaited<ReturnType<typeof getSettings>>);
    ensureConnectionProbe();
    await vi.advanceTimersByTimeAsync(FULL_LADDER_MS);
    expect(getConnectionProbeState().connected).toBe(false);
  });
});
