import { describe, it, expect } from "vitest";
import {
  NEW_ITEM_SEC,
  UPDATED_ITEM_SEC,
  COVER_DOWNLOAD_SEC,
  FETCH_ALLOWANCE_SEC,
  estimateApplySeconds,
  estimatePlanSeconds,
  formatDuration,
} from "./syncEstimate";
import type { SyncPlanUnit } from "../types/sync";

/** A platform plan unit; every estimate rider is opt-in per test. */
function unit(overrides: Partial<SyncPlanUnit> = {}): SyncPlanUnit {
  return { type: "platform", id: 1, name: "Platform", slug: "platform", rom_count: 0, ...overrides };
}

describe("estimateApplySeconds", () => {
  it("prices new and updated items with their per-item costs plus the fixed-overhead allowance", () => {
    // A create carries its own cover download; an update never does.
    expect(estimateApplySeconds(10, 0)).toBeCloseTo(10 * NEW_ITEM_SEC + 10 * COVER_DOWNLOAD_SEC + FETCH_ALLOWANCE_SEC);
    expect(estimateApplySeconds(0, 10)).toBeCloseTo(10 * UPDATED_ITEM_SEC + FETCH_ALLOWANCE_SEC);
    expect(estimateApplySeconds(4, 6)).toBeCloseTo(
      4 * NEW_ITEM_SEC + 6 * UPDATED_ITEM_SEC + 4 * COVER_DOWNLOAD_SEC + FETCH_ALLOWANCE_SEC,
    );
  });

  it("prices cover refreshes as their own term, so a cover-only run is no longer a flat allowance", () => {
    expect(estimateApplySeconds(0, 0, 140)).toBeCloseTo(140 * COVER_DOWNLOAD_SEC + FETCH_ALLOWANCE_SEC);
    // The refresh term is additive to the creates' own downloads.
    expect(estimateApplySeconds(10, 0, 5)).toBeCloseTo(
      10 * NEW_ITEM_SEC + 15 * COVER_DOWNLOAD_SEC + FETCH_ALLOWANCE_SEC,
    );
  });

  it("defaults the cover count to zero, so an older backend's preview prices as before", () => {
    expect(estimateApplySeconds(7, 3)).toBe(estimateApplySeconds(7, 3, 0));
  });

  it("returns the flat allowance for zero counts (fixed overhead still runs)", () => {
    expect(estimateApplySeconds(0, 0)).toBe(FETCH_ALLOWANCE_SEC);
  });

  it("clamps negative counts to zero, but the allowance still applies (never below the allowance)", () => {
    expect(estimateApplySeconds(-5, -3, -2)).toBe(FETCH_ALLOWANCE_SEC);
    expect(estimateApplySeconds(-5, 2)).toBeCloseTo(2 * UPDATED_ITEM_SEC + FETCH_ALLOWANCE_SEC);
  });

  it("prices a new item higher than an updated one (create is dearer than update)", () => {
    expect(NEW_ITEM_SEC).toBeGreaterThan(UPDATED_ITEM_SEC);
  });

  // Calibration guards for the model's OUTPUT at representative run shapes, so a
  // future constant tweak cannot silently drift the readout away from the
  // on-device rates the constants were fitted to (2026-07, #1511).
  it("prices representative create-heavy and update-heavy runs into sane buckets", () => {
    // A create-heavy run: each item pays the create walk plus a cover download.
    expect(formatDuration(estimateApplySeconds(700, 0))).toBe("7 min");
    // An update-heavy run (e.g. a Force Full Sync): cheap Set* walks, no covers.
    expect(formatDuration(estimateApplySeconds(0, 1300))).toBe("4 min");
  });
});

describe("estimatePlanSeconds", () => {
  it("prices already-bound rows as updates and the remainder as creates", () => {
    const seconds = estimatePlanSeconds([unit({ rom_count: 100, collapsed_count: 100, bound_count: 40 })]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(60, 40));
  });

  it("prices every item as a create when the backend omits bound_count (older backend)", () => {
    const seconds = estimatePlanSeconds([unit({ rom_count: 100, collapsed_count: 100 })]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(100, 0));
  });

  it("skips predicted-skip units entirely, however large", () => {
    const seconds = estimatePlanSeconds([
      unit({ rom_count: 5000, collapsed_count: 5000, bound_count: 5000, predicted_skip: true }),
      unit({ id: 2, slug: "b", rom_count: 10, collapsed_count: 10, bound_count: 4 }),
    ]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(6, 4));
  });

  it("weighs a unit by rom_count when no collapsed count is known", () => {
    const seconds = estimatePlanSeconds([unit({ rom_count: 30, bound_count: 10 })]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(20, 10));
  });

  it("clamps bound rows to the unit's item total (pre-collapse rows can exceed it)", () => {
    const seconds = estimatePlanSeconds([unit({ rom_count: 10, collapsed_count: 10, bound_count: 40 })]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(0, 10));
  });

  it("sums across units and reads the flat allowance for an empty plan", () => {
    expect(estimatePlanSeconds([])).toBe(FETCH_ALLOWANCE_SEC);
    const seconds = estimatePlanSeconds([
      unit({ rom_count: 10, collapsed_count: 10, bound_count: 10 }),
      unit({ id: 2, slug: "b", rom_count: 20, collapsed_count: 20 }),
    ]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(20, 10));
  });

  it("prices a collection unit's bound members as updates, same as a platform's", () => {
    // Collections carry no collapsed_count, so items come from rom_count. A
    // stamped collection whose members are already bound is an all-updates unit;
    // pricing it as creates over-read it ~4x for collection-heavy libraries.
    const collection: SyncPlanUnit = {
      type: "collection",
      id: "7",
      name: "Faves",
      slug: "",
      rom_count: 300,
      collection_kind: "user",
      bound_count: 300,
    };
    expect(estimatePlanSeconds([collection])).toBeCloseTo(estimateApplySeconds(0, 300));
    // Guard the regression directly: the all-creates reading is far dearer.
    expect(estimatePlanSeconds([collection])).toBeLessThan(estimateApplySeconds(300, 0));
  });

  it("prices an unstamped or franchise collection as all creates (bound_count absent)", () => {
    const collection: SyncPlanUnit = {
      type: "collection",
      id: "fr-1",
      name: "Zelda",
      slug: "zelda",
      rom_count: 40,
      collection_kind: "franchise",
    };
    expect(estimatePlanSeconds([collection])).toBeCloseTo(estimateApplySeconds(40, 0));
  });

  // The seed shape #1511 was opened for: a fully-mirrored library re-syncing.
  // Every row is bound, so the whole plan is cheap updates — pricing it as fresh
  // creates (0.36 + 0.15 each) is the ceiling the over-read came from.
  it("reads minutes, not a fresh-import ceiling, for an all-bound re-sync", () => {
    const seconds = estimatePlanSeconds([unit({ rom_count: 1000, collapsed_count: 1000, bound_count: 1000 })]);
    expect(formatDuration(seconds)).toBe("3 min");
  });
});

describe("formatDuration", () => {
  it("shows '< 1 min' for anything under a minute", () => {
    expect(formatDuration(0)).toBe("< 1 min");
    expect(formatDuration(1)).toBe("< 1 min");
    expect(formatDuration(59)).toBe("< 1 min");
    expect(formatDuration(59.9)).toBe("< 1 min");
  });

  it("shows '~N min' from one minute up to an hour", () => {
    expect(formatDuration(60)).toBe("1 min");
    expect(formatDuration(240)).toBe("4 min");
    // 3540s = 59 min, the last sub-hour bucket.
    expect(formatDuration(3540)).toBe("59 min");
  });

  it("rounds to the nearest minute in the minutes range", () => {
    // 90s = 1.5 min → rounds to 2.
    expect(formatDuration(90)).toBe("2 min");
    // 104s ≈ 1.73 min → rounds to 2.
    expect(formatDuration(104)).toBe("2 min");
  });

  it("rolls up to '1 h' exactly on the hour", () => {
    expect(formatDuration(3600)).toBe("1 h");
    // 3570s rounds to 60 min → "1 h", not "60 min".
    expect(formatDuration(3570)).toBe("1 h");
  });

  it("shows '~H h M min' beyond an hour with a remainder", () => {
    expect(formatDuration(4200)).toBe("1 h 10 min");
    expect(formatDuration(9000)).toBe("2 h 30 min");
  });

  it("omits the minutes part on a whole-hour value", () => {
    expect(formatDuration(7200)).toBe("2 h");
  });
});
