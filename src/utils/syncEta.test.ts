import { describe, it, expect, beforeEach } from "vitest";
import {
  cumulativeProcessed,
  windowedRate,
  remainingSeconds,
  formatEtaCountdown,
  beginEtaRun,
  observeApplyProgress,
  liveEtaSeconds,
  resetEta,
  type EtaSample,
} from "./syncEta";

describe("cumulativeProcessed", () => {
  it("sums completed units' weights plus the within-unit count", () => {
    // step=3 (1-based) → units 0 and 1 complete (100 + 200), plus current 50.
    expect(cumulativeProcessed([100, 200, 300], 3, 50)).toBe(350);
  });

  it("on the first unit (step 1) counts only the within-unit progress", () => {
    expect(cumulativeProcessed([100, 200], 1, 42)).toBe(42);
  });

  it("clamps negative weights and current to zero", () => {
    expect(cumulativeProcessed([-100, 200], 2, -5)).toBe(0);
    expect(cumulativeProcessed([50, -10], 3, 7)).toBe(57);
  });

  it("ignores a step beyond the known units (never reads past the array)", () => {
    expect(cumulativeProcessed([100, 200], 9, 5)).toBe(305);
  });

  it("handles an empty plan (weights unknown) as just the within-unit count", () => {
    expect(cumulativeProcessed([], 4, 12)).toBe(12);
  });
});

describe("windowedRate", () => {
  it("computes items/sec from the oldest→newest slope", () => {
    const samples: EtaSample[] = [
      { tMs: 0, processed: 100 },
      { tMs: 6000, processed: 700 },
    ];
    // (700-100) / 6s = 100/s.
    expect(windowedRate(samples)).toBe(100);
  });

  it("returns null with fewer than two samples", () => {
    expect(windowedRate([])).toBeNull();
    expect(windowedRate([{ tMs: 0, processed: 5 }])).toBeNull();
  });

  it("returns null when no time has elapsed (avoids divide-by-zero)", () => {
    expect(
      windowedRate([
        { tMs: 1000, processed: 5 },
        { tMs: 1000, processed: 9 },
      ]),
    ).toBeNull();
  });

  it("returns null when progress did not advance (flat or backward slope)", () => {
    expect(
      windowedRate([
        { tMs: 0, processed: 500 },
        { tMs: 5000, processed: 500 },
      ]),
    ).toBeNull();
  });
});

describe("remainingSeconds", () => {
  it("divides remaining items by the rate", () => {
    // (54700 - 700) / 100 = 540s.
    expect(remainingSeconds(54700, 700, 100)).toBe(540);
  });

  it("clamps to zero when processed meets or exceeds the total", () => {
    expect(remainingSeconds(100, 120, 5)).toBe(0);
  });

  it("returns zero (not Infinity) for a non-positive rate", () => {
    expect(remainingSeconds(100, 0, 0)).toBe(0);
    expect(remainingSeconds(100, 0, -1)).toBe(0);
  });
});

describe("formatEtaCountdown", () => {
  it("shows '< 1 min left' under a minute", () => {
    expect(formatEtaCountdown(0)).toBe("< 1 min left");
    expect(formatEtaCountdown(59)).toBe("< 1 min left");
  });

  it("rounds UP to the next whole minute", () => {
    expect(formatEtaCountdown(60)).toBe("~1 min left");
    expect(formatEtaCountdown(61)).toBe("~2 min left");
    expect(formatEtaCountdown(540)).toBe("~9 min left");
    expect(formatEtaCountdown(541)).toBe("~10 min left");
  });

  it("rolls into hours past 60 minutes", () => {
    expect(formatEtaCountdown(3600)).toBe("~1 h left");
    expect(formatEtaCountdown(4200)).toBe("~1 h 10 min left");
    expect(formatEtaCountdown(7200)).toBe("~2 h left");
  });
});

describe("run-scoped estimator", () => {
  beforeEach(() => resetEta());

  it("returns null before a run is begun", () => {
    expect(liveEtaSeconds()).toBeNull();
  });

  it("stays null until enough samples span the readiness window, then goes live", () => {
    beginEtaRun("run-1", [54700], 54700);
    observeApplyProgress(1, 100, 0);
    // One sample only — not ready.
    expect(liveEtaSeconds()).toBeNull();
    // A second sample too soon (< span threshold) — still not ready.
    observeApplyProgress(1, 200, 1000);
    expect(liveEtaSeconds()).toBeNull();
    // A sample spanning ≥5s → rate measured, live estimate available.
    observeApplyProgress(1, 700, 6000);
    // window [0:100, 1000:200, 6000:700]: (700-100)/6s = 100/s;
    // remaining = (54700-700)/100 = 540s.
    expect(liveEtaSeconds()).toBe(540);
  });

  it("throttles rapid samples so a burst does not distort the slope", () => {
    beginEtaRun("run-1", [10000], 10000);
    observeApplyProgress(1, 0, 0);
    // These three all fall inside the 1s throttle window → dropped.
    observeApplyProgress(1, 50, 100);
    observeApplyProgress(1, 90, 300);
    observeApplyProgress(1, 120, 900);
    // Only the t=0 sample was kept, so still not enough to be ready.
    expect(liveEtaSeconds()).toBeNull();
    observeApplyProgress(1, 600, 6000);
    // window [0:0, 6000:600]: 100/s; remaining = (10000-600)/100 = 94s.
    expect(liveEtaSeconds()).toBe(94);
  });

  it("resetEta clears the run so the estimate falls back to null", () => {
    beginEtaRun("run-1", [1000], 1000);
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    expect(liveEtaSeconds()).not.toBeNull();
    resetEta();
    expect(liveEtaSeconds()).toBeNull();
  });

  it("beginEtaRun discards a prior run's samples (no cross-run bleed)", () => {
    beginEtaRun("run-1", [1000], 1000);
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    expect(liveEtaSeconds()).not.toBeNull();
    // A fresh run starts — the old slope must not carry over.
    beginEtaRun("run-2", [2000], 2000);
    expect(liveEtaSeconds()).toBeNull();
  });

  it("credits completed units' weights across a unit boundary", () => {
    beginEtaRun("run-1", [1000, 2000], 3000);
    // Unit 1 nearly done.
    observeApplyProgress(1, 900, 0);
    // Unit 2 started (step 2): unit 1's full weight (1000) now counts.
    observeApplyProgress(2, 500, 6000);
    // window [0:900, 6000:1500]: (1500-900)/6 = 100/s;
    // remaining = (3000-1500)/100 = 15s.
    expect(liveEtaSeconds()).toBe(15);
  });

  it("observeApplyProgress is a no-op when no run is being measured", () => {
    // No beginEtaRun — must not throw and must stay null.
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    expect(liveEtaSeconds()).toBeNull();
  });
});
