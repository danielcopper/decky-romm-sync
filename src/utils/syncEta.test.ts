import { describe, it, expect, beforeEach } from "vitest";
import {
  cumulativeProcessed,
  windowedRate,
  remainingSeconds,
  formatEtaCountdown,
  beginEtaRun,
  observeApplyProgress,
  observeUnitTotal,
  liveEtaSeconds,
  displayedEtaSeconds,
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
    expect(formatEtaCountdown(60)).toBe("1 min left");
    expect(formatEtaCountdown(61)).toBe("2 min left");
    expect(formatEtaCountdown(540)).toBe("9 min left");
    expect(formatEtaCountdown(541)).toBe("10 min left");
  });

  it("rolls into hours past 60 minutes", () => {
    expect(formatEtaCountdown(3600)).toBe("1 h left");
    expect(formatEtaCountdown(4200)).toBe("1 h 10 min left");
    expect(formatEtaCountdown(7200)).toBe("2 h left");
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

  it("observeUnitTotal corrects a trailing unit's weight to its delta, shrinking the countdown (#1383)", () => {
    beginEtaRun("run-1", [1000, 3000], 4000);
    // Unit 2 dispatches with a small delta (50 of its 3000 raw rom_count) — the
    // delta-restricted apply skipped the rest, so unit_total = 50.
    observeUnitTotal(1, 50);
    // Measure unit 1's rate: 100 items/s over a 6s window.
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    // totalRoms is now 1000 + 50 = 1050 (was 4000); processed = 700 within unit 1.
    // remaining = (1050 - 700) / 100 = 3.5s. Without the correction it would read
    // (4000 - 700) / 100 = 33s — the trailing unit's raw weight over-weighting it.
    expect(liveEtaSeconds()).toBe(3.5);
  });

  it("observeUnitTotal is idempotent across a unit's chunks (same unit_total re-called)", () => {
    beginEtaRun("run-1", [1000, 3000], 4000);
    observeUnitTotal(1, 50);
    observeUnitTotal(1, 50); // a later chunk of the same unit carries the same total
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    // Corrected exactly once — not shrunk twice: (1050 - 700) / 100 = 3.5s.
    expect(liveEtaSeconds()).toBe(3.5);
  });

  it("observeUnitTotal is a no-op with no run or an out-of-range index", () => {
    observeUnitTotal(0, 10); // no run — must not throw
    expect(liveEtaSeconds()).toBeNull();
    beginEtaRun("run-1", [1000], 1000);
    observeUnitTotal(5, 10); // out-of-range index — leaves totalRoms untouched
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    // totalRoms still 1000; (1000 - 700) / 100 = 3s.
    expect(liveEtaSeconds()).toBe(3);
  });

  it("resets the segment across a >10s gap so no cross-gap slope is measured (50-min-spike regression)", () => {
    beginEtaRun("run-1", [54700], 54700);
    // A live segment: two samples 6s apart → a measurable rate.
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    expect(liveEtaSeconds()).not.toBeNull();
    // An inter-unit fetch gap > SEGMENT_BREAK_MS, then ONE post-gap sample. The
    // old window kept the last two samples across the gap and paired a pre-gap
    // sample with the post-gap one — a tiny item delta over an ~11s span, an
    // absurd rate that spiked the countdown to tens of minutes. The segment break
    // discards the pre-gap samples, so the lone post-gap sample measures nothing.
    observeApplyProgress(1, 710, 17000);
    // Null (not a huge number) is the whole point of the fix.
    expect(liveEtaSeconds()).toBeNull();
  });

  it("measures only the post-gap slope after a segment break (ignores the pre-gap rate)", () => {
    beginEtaRun("run-1", [100000], 100000);
    // Pre-gap: a WILDLY fast segment (~10000 items/s).
    observeApplyProgress(1, 0, 0);
    observeApplyProgress(1, 60000, 6000);
    // Gap > SEGMENT_BREAK_MS → the fast pre-gap samples are discarded.
    observeApplyProgress(1, 90000, 17000);
    // Post-gap: a slow segment (10 items/s) spanning ≥5s with ≥2 samples.
    observeApplyProgress(1, 90060, 23000);
    // rate = (90060-90000)/6s = 10/s → remaining = (100000-90060)/10 = 994s.
    // A cross-gap slope would have folded in the ~10000/s pre-gap rate and read a
    // few seconds — 994 proves the reset measured the post-gap rate alone.
    expect(liveEtaSeconds()).toBe(994);
  });

  it("does not reset at a gap equal to the segment-break threshold (strict >, boundary)", () => {
    beginEtaRun("run-1", [100000], 100000);
    observeApplyProgress(1, 0, 0);
    observeApplyProgress(1, 600, 6000);
    // A gap of exactly SEGMENT_BREAK_MS (16000-6000 = 10000ms) is NOT > the
    // threshold, so it is treated as ongoing apply work: the window still spans
    // the pre-gap sample and a rate is measured.
    observeApplyProgress(1, 700, 16000);
    expect(liveEtaSeconds()).not.toBeNull();
  });
});

describe("displayedEtaSeconds (sticky countdown deadline)", () => {
  beforeEach(() => resetEta());

  it("returns null when no run is in flight", () => {
    expect(displayedEtaSeconds(0)).toBeNull();
  });

  it("returns null before the first ready measurement (no deadline anchored yet)", () => {
    beginEtaRun("run-1", [54700], 54700);
    observeApplyProgress(1, 100, 0); // one sample → not ready → no deadline
    expect(liveEtaSeconds()).toBeNull();
    expect(displayedEtaSeconds(0)).toBeNull();
    expect(displayedEtaSeconds(1000)).toBeNull();
  });

  it("counts down as now advances against a fixed deadline, clamped at zero", () => {
    beginEtaRun("run-1", [54700], 54700);
    observeApplyProgress(1, 100, 0);
    // Ready at t=6000: rate 100/s, remaining 540s → deadline = 6000 + 540_000.
    observeApplyProgress(1, 700, 6000);
    expect(displayedEtaSeconds(6000)).toBe(540);
    // 60s later, with no new sample, it has ticked down.
    expect(displayedEtaSeconds(66000)).toBe(480);
    // Past the deadline never goes negative.
    expect(displayedEtaSeconds(600000)).toBe(0);
  });

  it("holds the last deadline (sticky) when a segment break re-arms liveEtaSeconds to null", () => {
    beginEtaRun("run-1", [54700], 54700);
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000); // deadline anchored at 6000 + 540_000 = 546_000
    expect(liveEtaSeconds()).not.toBeNull();
    expect(displayedEtaSeconds(6000)).toBe(540);
    // A >10s gap resets the measurement segment → liveEtaSeconds re-arms to null…
    observeApplyProgress(1, 710, 17000);
    expect(liveEtaSeconds()).toBeNull();
    // …but the displayed countdown holds the prior deadline and keeps ticking,
    // instead of snapping back to the static seed. (546_000 - 17_000) / 1000 = 529.
    expect(displayedEtaSeconds(17000)).toBe(529);
  });

  it("returns null after resetEta clears the run (and with it the deadline)", () => {
    beginEtaRun("run-1", [54700], 54700);
    observeApplyProgress(1, 100, 0);
    observeApplyProgress(1, 700, 6000);
    expect(displayedEtaSeconds(6000)).not.toBeNull();
    resetEta();
    expect(displayedEtaSeconds(6000)).toBeNull();
  });
});
