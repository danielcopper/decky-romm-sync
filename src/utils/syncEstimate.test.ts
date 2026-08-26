import { describe, it, expect } from "vitest";
import {
  NEW_ITEM_SEC,
  UPDATED_ITEM_SEC,
  COVER_DOWNLOAD_SEC,
  FETCH_ALLOWANCE_SEC,
  estimateApplySeconds,
  estimatePlanSeconds,
  formatDuration,
  formatTimeRemaining,
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
      collection_kind: "standard",
      bound_count: 300,
    };
    expect(estimatePlanSeconds([collection])).toBeCloseTo(estimateApplySeconds(0, 300));
    // Guard the regression directly: the all-creates reading is far dearer.
    expect(estimatePlanSeconds([collection])).toBeLessThan(estimateApplySeconds(300, 0));
  });

  it("prices an unstamped or virtual collection as all creates (bound_count absent)", () => {
    const collection: SyncPlanUnit = {
      type: "collection",
      id: "fr-1",
      name: "Zelda",
      slug: "zelda",
      rom_count: 40,
      collection_kind: "virtual",
    };
    expect(estimatePlanSeconds([collection])).toBeCloseTo(estimateApplySeconds(40, 0));
  });

  it("takes new_shortcut_count as the create term instead of subtracting the bound rows", () => {
    // The unit weighs 100 pre-collapse, but only 25 shortcuts are actually new.
    const seconds = estimatePlanSeconds([unit({ rom_count: 100, bound_count: 60, new_shortcut_count: 25 })]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(25, 60));
  });

  // The seed shape #1517 was opened for. A Force Full Sync clears the completion
  // stamps, so collapsed_count is absent and the unit weighs its PRE-COLLAPSE
  // rom_count — every sibling duplicate included. Subtracting the bound rows
  // from that weight prices each duplicate as a phantom create plus a cover
  // download, which is where the ~2.5x over-read came from.
  it("prices a Force Full Sync of a sibling-heavy platform entirely at the update rate", () => {
    const forced = unit({ rom_count: 100, bound_count: 60, new_shortcut_count: 0 });

    expect(estimatePlanSeconds([forced])).toBeCloseTo(estimateApplySeconds(0, 60));
    // Guard the regression directly: the subtraction reading is far dearer.
    expect(estimatePlanSeconds([forced])).toBeLessThan(estimateApplySeconds(40, 60));
  });

  // The safety-critical shape: a never-synced platform holding only partial
  // collection-sibling rows (ADR-0021). Its create count covers the server ROMs
  // the mirror knows nothing about, so the seed must NOT collapse to the
  // handful of known rows — reading short is the one direction it may not err in.
  it("does not read short for a never-synced platform holding partial rows", () => {
    const partial = unit({ rom_count: 100, bound_count: 2, new_shortcut_count: 98 });

    expect(estimatePlanSeconds([partial])).toBeCloseTo(estimateApplySeconds(98, 2));
    // A rows-only create count would have priced 1 create + 2 updates here.
    expect(estimatePlanSeconds([partial])).toBeGreaterThan(estimateApplySeconds(1, 2));
    // Still at least as dear as pricing the whole platform at the update rate.
    expect(estimatePlanSeconds([partial])).toBeGreaterThan(estimateApplySeconds(0, 100));
  });

  it("prices a first-ever platform sync as all creates", () => {
    const seconds = estimatePlanSeconds([unit({ rom_count: 40, bound_count: 0, new_shortcut_count: 40 })]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(40, 0));
  });

  it("falls back to the bound-row subtraction when the backend omits new_shortcut_count", () => {
    // Collections never carry the rider, and neither does an older backend.
    const seconds = estimatePlanSeconds([unit({ rom_count: 100, collapsed_count: 100, bound_count: 40 })]);
    expect(seconds).toBeCloseTo(estimateApplySeconds(60, 40));
  });

  it("prices a zero create count as knowledge, not as a missing rider", () => {
    const allBound = unit({ rom_count: 50, bound_count: 50, new_shortcut_count: 0 });
    expect(estimatePlanSeconds([allBound])).toBeCloseTo(estimateApplySeconds(0, 50));
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

describe("formatTimeRemaining", () => {
  it("shows '< 1 min' for anything under a minute", () => {
    expect(formatTimeRemaining(0)).toBe("< 1 min");
    expect(formatTimeRemaining(59)).toBe("< 1 min");
  });

  it("floors to the minute rather than rounding, so it never promises more time than remains", () => {
    // 89s = 1.48 min — formatDuration rounds this to "1 min" too, but 90s
    // (exactly 1.5 min) is where the two part: rounding reads "2 min" for a
    // deadline that is 90 seconds away.
    expect(formatTimeRemaining(89)).toBe("1 min");
    expect(formatTimeRemaining(90)).toBe("1 min");
    expect(formatDuration(90)).toBe("2 min");
    // 3599s is one second short of the hour and must not read "1 h".
    expect(formatTimeRemaining(3599)).toBe("59 min");
    expect(formatDuration(3599)).toBe("1 h");
  });

  it("renders the full TTL and the hour boundary", () => {
    expect(formatTimeRemaining(1800)).toBe("30 min");
    expect(formatTimeRemaining(3600)).toBe("1 h");
    expect(formatTimeRemaining(4200)).toBe("1 h 10 min");
  });
});
