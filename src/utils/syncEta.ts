/**
 * Live sync-ETA estimator — the run-scoped "~9 min left" countdown shown while a
 * sync is applying shortcuts. Where ``syncEstimate.ts`` is a STATIC pre-run cost
 * model (item counts → an upper-bound duration), this module measures the REAL
 * apply rate from the progress stream and projects the time left, so the readout
 * reflects the run in front of the user and counts down as it proceeds.
 *
 * The math is pure and unit-tested (``cumulativeProcessed`` / ``windowedRate`` /
 * ``remainingSeconds`` / ``formatEtaCountdown``). A thin run-scoped state layer
 * (``beginEtaRun`` / ``observeApplyProgress`` / ``liveEtaSeconds`` / ``resetEta``)
 * is the seam the ``sync_plan`` listener (index.tsx) and MainPage drive: the plan
 * sets the per-unit weights + total, and MainPage feeds one sample per applying
 * progress frame and reads back the current estimate.
 *
 * Approximation, by design: the plan's per-unit ``rom_count`` is the RAW
 * pre-collapse file count, while the applying stage's ``current`` is the
 * post-collapse EMITTED shortcut count (sibling groups collapse to one shortcut).
 * Mixing the two slightly overstates a grouped unit's completed weight, but the
 * measured rate absorbs most of the skew and the countdown stays close enough to
 * be useful. It is an estimate, never a guarantee.
 */

export interface EtaSample {
  readonly tMs: number;
  readonly processed: number;
}

// Sampling cadence + window. Samples are throttled so the rate is measured over
// seconds rather than the ~50ms per-item apply cadence; the sliding window bounds
// the rate to recent progress, so a mid-run slowdown is reflected rather than
// averaged away by the whole run.
const SAMPLE_MIN_INTERVAL_MS = 1000;
const WINDOW_MS = 30_000;
// Readiness gate: the live countdown replaces the static "up to ~X" seed only
// once the window spans enough real time to trust the slope. ~5s of applying
// (≥2 throttled samples) is well inside the "measured within ~20s" target while
// keeping the first estimate stable rather than jittery.
const READY_MIN_SAMPLES = 2;
const READY_MIN_SPAN_MS = 5_000;

/**
 * Cumulative items processed so far: every completed unit's weight plus the count
 * within the running unit. ``step`` is the 1-based unit index the applying frames
 * carry (``unit_index + 1``); the running unit is ``step - 1`` (0-based), so units
 * ``[0, step-1)`` are complete. Negative inputs are clamped to zero.
 */
export function cumulativeProcessed(unitWeights: readonly number[], step: number, current: number): number {
  const completedUnits = Math.max(0, step - 1);
  let sum = 0;
  for (let i = 0; i < completedUnits && i < unitWeights.length; i++) {
    sum += Math.max(0, unitWeights[i] ?? 0);
  }
  return sum + Math.max(0, current);
}

/**
 * Items/second across the sample window, or ``null`` when the window is too thin
 * or shows no forward progress: fewer than two samples, a non-positive time span,
 * or a non-increasing processed count. Uses the plain oldest→newest slope —
 * robust and tuning-free.
 */
export function windowedRate(samples: readonly EtaSample[]): number | null {
  if (samples.length < 2) return null;
  const first = samples[0];
  const last = samples[samples.length - 1];
  if (first === undefined || last === undefined) return null;
  const spanMs = last.tMs - first.tMs;
  const delta = last.processed - first.processed;
  if (spanMs <= 0 || delta <= 0) return null;
  return delta / (spanMs / 1000);
}

/** Remaining seconds to process ``totalRoms - processed`` items at *rate* items/sec; clamped to ``≥ 0``. */
export function remainingSeconds(totalRoms: number, processed: number, rate: number): number {
  if (rate <= 0) return 0;
  return Math.max(0, (totalRoms - processed) / rate);
}

/**
 * Render *seconds* as a live countdown: ``"< 1 min left"`` under a minute, else
 * minutes ROUNDED UP (``"~9 min left"``), rolling into hours past 60 minutes
 * (``"~1 h 10 min left"``, or ``"~2 h left"`` on the hour). Rounding up keeps the
 * readout honest — a countdown should never promise less time than it expects.
 */
export function formatEtaCountdown(seconds: number): string {
  if (seconds < 60) return "< 1 min left";
  const totalMinutes = Math.ceil(seconds / 60);
  if (totalMinutes < 60) return `~${totalMinutes} min left`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes > 0 ? `~${hours} h ${minutes} min left` : `~${hours} h left`;
}

// ── Run-scoped state layer ────────────────────────────────────────────────

interface EtaRunState {
  runId: string;
  unitWeights: number[];
  totalRoms: number;
  samples: EtaSample[];
  lastSampleMs: number;
}

let _run: EtaRunState | null = null;

/**
 * Begin measuring a fresh run, discarding any prior samples — a new run's rate
 * must never inherit the previous run's slope. Called once by the ``sync_plan``
 * listener with the plan's per-unit weights (``rom_count`` in plan order) and the
 * planned ROM total.
 */
export function beginEtaRun(runId: string, unitWeights: number[], totalRoms: number): void {
  _run = { runId, unitWeights: [...unitWeights], totalRoms, samples: [], lastSampleMs: 0 };
}

/** Drop all ETA state — call at a terminal stage or when no run is in flight. */
export function resetEta(): void {
  _run = null;
}

/**
 * Record one applying-stage sample. Throttled to ``SAMPLE_MIN_INTERVAL_MS`` so
 * the rate is measured over seconds; samples older than ``WINDOW_MS`` are dropped
 * (but the two most recent always survive, so a long quiet gap re-measures rather
 * than erasing the estimate). A no-op when no run is being measured. Feed ONLY
 * applying frames — fetch frames carry page/cover counters, not item progress.
 */
export function observeApplyProgress(step: number, current: number, tMs: number): void {
  if (_run === null) return;
  if (_run.samples.length > 0 && tMs - _run.lastSampleMs < SAMPLE_MIN_INTERVAL_MS) return;
  const processed = cumulativeProcessed(_run.unitWeights, step, current);
  _run.samples.push({ tMs, processed });
  _run.lastSampleMs = tMs;
  const cutoff = tMs - WINDOW_MS;
  const recent = _run.samples.filter((s) => s.tMs >= cutoff);
  _run.samples = recent.length >= 2 ? recent : _run.samples.slice(-2);
}

/**
 * The live remaining-seconds estimate, or ``null`` when the run isn't ready to
 * replace the static seed yet: no run, too few samples, too short a span, or a
 * flat/backward slope. MainPage shows the static "up to ~X" until this returns a
 * number, then switches to the "~X left" countdown.
 */
export function liveEtaSeconds(): number | null {
  if (_run === null || _run.samples.length < READY_MIN_SAMPLES) return null;
  const first = _run.samples[0];
  const last = _run.samples[_run.samples.length - 1];
  if (first === undefined || last === undefined) return null;
  if (last.tMs - first.tMs < READY_MIN_SPAN_MS) return null;
  const rate = windowedRate(_run.samples);
  if (rate === null) return null;
  return remainingSeconds(_run.totalRoms, last.processed, rate);
}
