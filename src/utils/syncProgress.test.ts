import { describe, it, expect, vi, afterEach } from "vitest";
import {
  setSyncProgress,
  updateSyncProgress,
  onSyncProgressChange,
  getSyncProgress,
  withinUnitFraction,
  FETCH_SHARE,
  COVERS_SHARE,
  APPLY_SHARE,
} from "./syncProgress";
import type { SyncProgress } from "../types";

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

// The pure within-unit sub-slice model (#1407). The running unit's width is
// split into fetch → covers → apply bands, each filling by its own current/total
// within a strictly-higher band than the phase before, so the bar never jumps
// backwards at a fetch→covers→apply boundary even though each phase restarts
// current/total from zero.
describe("withinUnitFraction sub-slice model (#1407)", () => {
  const frame = (p: Partial<SyncProgress>): SyncProgress => ({ running: true, ...p });

  it("the three shares sum to the full unit width", () => {
    expect(FETCH_SHARE + COVERS_SHARE + APPLY_SHARE).toBeCloseTo(1, 10);
  });

  it("fetch sub-stage fills only the fetch share", () => {
    expect(withinUnitFraction(frame({ stage: "fetching", subStage: "fetch", current: 3, total: 10 }))).toBeCloseTo(
      FETCH_SHARE * 0.3,
      10,
    );
  });

  it("covers sub-stage starts at the fetch ceiling and fills the covers share", () => {
    expect(withinUnitFraction(frame({ stage: "fetching", subStage: "covers", current: 1, total: 4 }))).toBeCloseTo(
      FETCH_SHARE + COVERS_SHARE * 0.25,
      10,
    );
  });

  it("applying starts at the fetch+covers ceiling and fills the apply share", () => {
    expect(withinUnitFraction(frame({ stage: "applying", current: 1, total: 2 }))).toBeCloseTo(
      FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * 0.5,
      10,
    );
  });

  it("applying ignores a stale merged subStage (keyed on the stage alone)", () => {
    // A frontend apply frame merges over a prior covers frame, so it can still
    // carry subStage "covers"; the apply band must win regardless.
    expect(withinUnitFraction(frame({ stage: "applying", subStage: "covers", current: 1, total: 1 }))).toBeCloseTo(
      1,
      10,
    );
  });

  it("a full unit's apply completes exactly at 1.0", () => {
    expect(withinUnitFraction(frame({ stage: "applying", current: 10, total: 10 }))).toBeCloseTo(1, 10);
  });

  it("fetching with no sub-stage rests at the unit floor (0) — legacy/anchor behaviour", () => {
    expect(withinUnitFraction(frame({ stage: "fetching", current: 30, total: 62 }))).toBe(0);
    expect(withinUnitFraction(frame({ stage: "fetching", current: 0, total: 0 }))).toBe(0);
  });

  it("a falsy current/total yields the phase floor, never a divide", () => {
    // covers with total 0 → the fetch ceiling (its own share contributes 0).
    expect(withinUnitFraction(frame({ stage: "fetching", subStage: "covers", current: 0, total: 0 }))).toBeCloseTo(
      FETCH_SHARE,
      10,
    );
    // applying with total 0 → the fetch+covers ceiling.
    expect(withinUnitFraction(frame({ stage: "applying", current: 0, total: 0 }))).toBeCloseTo(
      FETCH_SHARE + COVERS_SHARE,
      10,
    );
    // fetch with total 0 → 0.
    expect(withinUnitFraction(frame({ stage: "fetching", subStage: "fetch", current: 0, total: 0 }))).toBe(0);
  });

  it("clamps an overshooting current/total to the phase ceiling", () => {
    expect(withinUnitFraction(frame({ stage: "applying", current: 99, total: 10 }))).toBeCloseTo(1, 10);
  });

  it("non-unit stages (discovering/finalizing) contribute no within-unit fill", () => {
    expect(withinUnitFraction(frame({ stage: "discovering", current: 1, total: 2 }))).toBe(0);
    expect(withinUnitFraction(frame({ stage: "finalizing", current: 1, total: 2 }))).toBe(0);
  });

  it("returns 0 for a null/undefined progress", () => {
    expect(withinUnitFraction(null)).toBe(0);
    expect(withinUnitFraction(undefined)).toBe(0);
  });

  it("is non-decreasing across a fetch → covers → apply frame sequence", () => {
    const frames: SyncProgress[] = [
      frame({ stage: "fetching", current: 0, total: 0 }),
      ...Array.from({ length: 7 }, (_, i) => frame({ stage: "fetching", subStage: "fetch", current: i + 1, total: 7 })),
      ...Array.from({ length: 100 }, (_, i) =>
        frame({ stage: "fetching", subStage: "covers", current: i + 1, total: 100 }),
      ),
      ...Array.from({ length: 50 }, (_, i) => frame({ stage: "applying", current: i + 1, total: 50 })),
    ];
    let previous = -1;
    for (const f of frames) {
      const value = withinUnitFraction(f);
      expect(value).toBeGreaterThanOrEqual(previous);
      previous = value;
    }
    expect(previous).toBeCloseTo(1, 10);
  });
});
