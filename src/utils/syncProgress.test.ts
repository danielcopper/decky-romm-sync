import { describe, it, expect, vi, afterEach } from "vitest";
import { setSyncProgress, updateSyncProgress, onSyncProgressChange, getSyncProgress } from "./syncProgress";

// The store's notify() runs every subscriber inside its own try/catch so a
// throwing listener can neither starve later listeners nor break the emitting
// call site (the syncManager per-item apply loop calls updateSyncProgress inside
// its per-game try block — a propagated throw there would skip that game's
// shortcut creation entirely). The listener array is module-level and persists
// across tests; every test unsubscribes what it subscribes so none leak.
describe("syncProgress store notify hardening", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("a throwing first listener does not starve a later-subscribed listener", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const thrower = vi.fn(() => {
      throw new Error("boom");
    });
    const later = vi.fn();
    const unsub1 = onSyncProgressChange(thrower);
    const unsub2 = onSyncProgressChange(later);
    try {
      setSyncProgress({ running: true, stage: "applying" });
      // The earlier throw was isolated — the later listener still fired...
      expect(thrower).toHaveBeenCalledTimes(1);
      expect(later).toHaveBeenCalledTimes(1);
      // ...and the throw was reported to the console, not propagated.
      expect(errSpy).toHaveBeenCalledWith("[RomM] sync-progress listener threw:", expect.any(Error));
    } finally {
      unsub1();
      unsub2();
    }
  });

  it("updateSyncProgress does not throw when a subscriber throws (emitter is protected)", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const unsub = onSyncProgressChange(() => {
      throw new Error("boom");
    });
    try {
      // The emitting call site (e.g. syncManager's per-item apply loop) must not
      // see the subscriber's throw.
      expect(() => updateSyncProgress({ etaSeconds: 1000 })).not.toThrow();
      // The partial update still landed on the store.
      expect(getSyncProgress().etaSeconds).toBe(1000);
    } finally {
      unsub();
    }
  });

  it("unsubscribe removes the listener so it is no longer notified", () => {
    const fn = vi.fn();
    const unsub = onSyncProgressChange(fn);
    setSyncProgress({ running: false, stage: "" });
    expect(fn).toHaveBeenCalledTimes(1);
    unsub();
    setSyncProgress({ running: false, stage: "" });
    // No further notification after unsubscribe.
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
