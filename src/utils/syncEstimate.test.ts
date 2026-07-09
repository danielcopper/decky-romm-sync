import { describe, it, expect } from "vitest";
import { NEW_ITEM_SEC, UPDATED_ITEM_SEC, estimateApplySeconds, formatDuration } from "./syncEstimate";

describe("estimateApplySeconds", () => {
  it("prices new and updated items with their respective per-item costs", () => {
    expect(estimateApplySeconds(10, 0)).toBe(10 * NEW_ITEM_SEC);
    expect(estimateApplySeconds(0, 10)).toBe(10 * UPDATED_ITEM_SEC);
    expect(estimateApplySeconds(4, 6)).toBe(4 * NEW_ITEM_SEC + 6 * UPDATED_ITEM_SEC);
  });

  it("returns 0 for zero counts", () => {
    expect(estimateApplySeconds(0, 0)).toBe(0);
  });

  it("clamps negative counts to zero (never a negative estimate)", () => {
    expect(estimateApplySeconds(-5, -3)).toBe(0);
    expect(estimateApplySeconds(-5, 2)).toBe(2 * UPDATED_ITEM_SEC);
  });

  it("prices a new item higher than an updated one (AddShortcut settle wait)", () => {
    expect(NEW_ITEM_SEC).toBeGreaterThan(UPDATED_ITEM_SEC);
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
    expect(formatDuration(60)).toBe("~1 min");
    expect(formatDuration(240)).toBe("~4 min");
    // 3540s = 59 min, the last sub-hour bucket.
    expect(formatDuration(3540)).toBe("~59 min");
  });

  it("rounds to the nearest minute in the minutes range", () => {
    // 90s = 1.5 min → rounds to 2.
    expect(formatDuration(90)).toBe("~2 min");
    // 104s ≈ 1.73 min → rounds to 2.
    expect(formatDuration(104)).toBe("~2 min");
  });

  it("rolls up to '~1 h' exactly on the hour", () => {
    expect(formatDuration(3600)).toBe("~1 h");
    // 3570s rounds to 60 min → "~1 h", not "~60 min".
    expect(formatDuration(3570)).toBe("~1 h");
  });

  it("shows '~H h M min' beyond an hour with a remainder", () => {
    expect(formatDuration(4200)).toBe("~1 h 10 min");
    expect(formatDuration(9000)).toBe("~2 h 30 min");
  });

  it("omits the minutes part on a whole-hour value", () => {
    expect(formatDuration(7200)).toBe("~2 h");
  });
});
