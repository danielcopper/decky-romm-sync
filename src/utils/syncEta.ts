/**
 * Live sync-ETA estimator — the run-scoped "9 min left" countdown shown while a
 * sync is applying shortcuts. Where ``syncEstimate.ts`` is a STATIC pre-run cost
 * model (item counts → an upper-bound duration), this module measures the REAL
 * apply rate from the progress stream and projects the time left, so the readout
 * reflects the run in front of the user and counts down as it proceeds.
 *
 * The math is pure and unit-tested (``cumulativeProcessed`` / ``windowedRate`` /
 * ``remainingSeconds`` / ``formatEtaCountdown``). A thin run-scoped state layer
 * (``beginEtaRun`` / ``observeApplyProgress`` / ``liveEtaSeconds`` /
 * ``displayedEtaSeconds`` / ``resetEta``) is the seam the ``sync_plan`` listener
 * (index.tsx) and MainPage drive: the plan sets the per-unit weights + total, and
 * MainPage feeds one sample per applying progress frame and renders the sticky
 * countdown via ``displayedEtaSeconds`` — the module owns the deadline, so the UI
 * holds no ETA state of its own.
 *
 * Approximation, by design — though a narrow one since the plan went skip-aware
 * (#1382): a unit's seeded weight is 0 when the backend predicts its wholesale
 * incremental skip, else the persisted post-collapse (sibling-group) shortcut
 * count, so seed weights and the applying stage's post-collapse ``current``
 * usually count the same thing. The raw pre-collapse ``rom_count`` remains the
 * fallback only where the backend doesn't know better (never-synced platforms,
 * collections, old backends), and a mis-predicted skip re-corrects the moment
 * the unit dispatches (``observeUnitTotal``). Residual skew is absorbed by the
 * measured rate. It is an estimate, never a guarantee — a mis-prediction can
 * only make the readout long or short, never change what the sync applies.
 */

import { formatApproxDuration } from "./syncEstimate";

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
// Segment boundary. Applying frames arrive every ~1s during real apply work, so a
// silence longer than this is always a unit/fetch boundary, never apply progress.
// Crossing it starts a fresh measurement segment (see observeApplyProgress) so the
// slope is never measured across the gap — a pre-gap sample paired with a post-gap
// one is a tiny item delta over a long span, an absurd rate that briefly spikes the
// countdown before it settles.
const SEGMENT_BREAK_MS = 10_000;
// Readiness gate: the live countdown replaces the static "up to X" seed only
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
 * minutes ROUNDED UP (``"9 min left"``), rolling into hours past 60 minutes
 * (``"1 h 10 min left"``, or ``"2 h left"`` on the hour). Rounding up keeps the
 * readout honest — a countdown should never promise less time than it expects.
 */
export function formatEtaCountdown(seconds: number): string {
  return formatApproxDuration(seconds, Math.ceil, " left");
}

// ── Run-scoped state layer ────────────────────────────────────────────────

interface EtaRunState {
  runId: string;
  unitWeights: number[];
  totalRoms: number;
  samples: EtaSample[];
  lastSampleMs: number;
  // Absolute wall-clock deadline (ms) the displayed countdown targets, re-anchored
  // from the last non-null measurement and held across gaps (see
  // observeApplyProgress / displayedEtaSeconds). ``null`` until the first ready
  // measurement.
  deadlineMs: number | null;
  // How many of the plan's leading units weigh zero, LATCHED at its high-water
  // mark for the run — the width the coarse bar owes them (#1506). Latched
  // because observeUnitTotal can raise a mispredicted skip's weight off zero
  // mid-run, which would shorten the live prefix and march the bar backwards.
  zeroPrefix: number;
}

let _run: EtaRunState | null = null;

/** How many leading units weigh zero (a non-positive weight counts as zero). */
function countZeroPrefix(weights: readonly number[]): number {
  let n = 0;
  while (n < weights.length && Math.max(0, weights[n] ?? 0) <= 0) n++;
  return n;
}

/**
 * Begin measuring a fresh run, discarding any prior samples — a new run's rate
 * must never inherit the previous run's slope. Called once by the ``sync_plan``
 * listener with the plan's per-unit weights in plan order (skip-aware since
 * #1382: 0 for predicted skips, else ``collapsed_count ?? rom_count``) and the
 * planned item total.
 */
export function beginEtaRun(runId: string, unitWeights: number[], totalRoms: number): void {
  _run = {
    runId,
    unitWeights: [...unitWeights],
    totalRoms,
    samples: [],
    lastSampleMs: 0,
    deadlineMs: null,
    zeroPrefix: countZeroPrefix(unitWeights),
  };
}

/** Drop all ETA state — call at a terminal stage or when no run is in flight. */
export function resetEta(): void {
  _run = null;
}

/**
 * Correct a dispatched unit's weight to its real DELTA size (#1383 / #1382-M3).
 *
 * The plan seeds each unit's weight from its RAW pre-collapse ``rom_count``, but
 * the delta-restricted apply only touches new + changed shortcuts, so a unit's
 * ``sync_apply_unit`` frame carries the true count in ``unit_total``. Folding that
 * in as the unit dispatches shrinks ``totalRoms`` toward the real work and stops a
 * mostly-unchanged (small-delta) trailing unit from over-weighting the countdown
 * into "N min left" for work that finishes in seconds. Idempotent per unit —
 * every chunk of a unit carries the same ``unit_total``, so re-calls no-op once the
 * weight matches. A no-op when no run is measured or the index is out of range.
 *
 * This is also the safety net for the plan's skip prediction (#1382): a unit the
 * plan zero-weighted as a predicted skip but that actually dispatches re-corrects
 * here to its real delta size on its first chunk. A unit skipped wholesale never
 * emits a ``sync_apply_unit`` — its plan weight (0 when predicted, or the stale
 * seeded weight on a mis-prediction in the other direction) simply stands.
 */
export function observeUnitTotal(unitIndex: number, unitTotal: number): void {
  if (_run === null) return;
  if (unitIndex < 0 || unitIndex >= _run.unitWeights.length) return;
  const corrected = Math.max(0, unitTotal);
  const previous = Math.max(0, _run.unitWeights[unitIndex] ?? 0);
  if (corrected === previous) return;
  _run.totalRoms = Math.max(0, _run.totalRoms + (corrected - previous));
  _run.unitWeights[unitIndex] = corrected;
  // Keep the bar's leading-zero-unit width at its high-water mark. Correcting a
  // mispredicted skip UP off zero shortens the live prefix; honouring that would
  // retract width the bar already showed and freeze every later zero-weight unit
  // at the truncated floor. A correction DOWN to zero only lengthens the prefix,
  // which the max adopts (#1506).
  _run.zeroPrefix = Math.max(_run.zeroPrefix, countZeroPrefix(_run.unitWeights));
}

/**
 * Record one applying-stage sample. Throttled to ``SAMPLE_MIN_INTERVAL_MS`` so
 * the rate is measured over seconds; samples older than ``WINDOW_MS`` are dropped.
 * A silence longer than ``SEGMENT_BREAK_MS`` is a unit/fetch boundary: the prior
 * samples are discarded and a fresh measurement segment begins (this sample
 * bypasses the throttle), so no slope is ever measured across the gap — pairing a
 * pre-gap sample with a post-gap one yields an absurd rate. The estimator re-arms
 * to ``null`` until the new segment spans the readiness window. A no-op when no run
 * is being measured. Feed ONLY applying frames — fetch frames carry page/cover
 * counters, not item progress.
 */
export function observeApplyProgress(step: number, current: number, tMs: number): void {
  if (_run === null) return;
  // Segment break: a long silence is a boundary, not apply work. Drop the prior
  // samples so the cross-gap slope is never measured; clearing the array also lets
  // this sample bypass the throttle below (a fresh segment's first sample).
  if (_run.samples.length > 0 && tMs - _run.lastSampleMs > SEGMENT_BREAK_MS) {
    _run.samples = [];
  }
  if (_run.samples.length > 0 && tMs - _run.lastSampleMs < SAMPLE_MIN_INTERVAL_MS) return;
  const processed = cumulativeProcessed(_run.unitWeights, step, current);
  _run.samples.push({ tMs, processed });
  _run.lastSampleMs = tMs;
  const cutoff = tMs - WINDOW_MS;
  const recent = _run.samples.filter((s) => s.tMs >= cutoff);
  _run.samples = recent.length >= 2 ? recent : _run.samples.slice(-2);
  // Re-anchor the sticky countdown deadline from the fresh measurement. The
  // readiness gate re-arms liveEtaSeconds() to null ~5s after every inter-unit
  // fetch gap, and a run's tail of small units each applies in <5s and so never
  // re-arms it — a raw seconds snapshot would blink the readout back to the static
  // seed for the whole tail. Holding the last good measurement as an absolute
  // deadline keeps the countdown ticking down honestly through the gaps; a null
  // measurement KEEPS the prior deadline, so stickiness falls out naturally.
  const seconds = liveEtaSeconds();
  if (seconds !== null) _run.deadlineMs = tMs + seconds * 1000;
}

/**
 * The live remaining-seconds estimate, or ``null`` when the run isn't ready to
 * replace the static seed yet: no run, too few samples, too short a span, or a
 * flat/backward slope. Re-arms to null between measurement segments; the sticky
 * ``displayedEtaSeconds`` is what the UI actually renders.
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

/**
 * The seconds the UI should display on the countdown, derived from the sticky
 * deadline: ``max(0, (deadlineMs - nowMs) / 1000)`` while a deadline is set, else
 * ``null``. Unlike {@link liveEtaSeconds} (which re-arms to null between
 * measurement segments), this holds the last good deadline across fetch gaps and
 * small-unit tails, so the readout counts down smoothly instead of snapping back
 * to the static "up to X" seed. ``null`` before the first ready measurement and
 * after {@link resetEta} (which clears the run, and with it the deadline). Renders
 * tick as the caller passes a fresh ``nowMs`` on each progress frame.
 */
export function displayedEtaSeconds(nowMs: number): number | null {
  if (_run?.deadlineMs == null) return null;
  return Math.max(0, (_run.deadlineMs - nowMs) / 1000);
}

/**
 * Weighted coarse-bar fraction (0..1) over the run's per-unit plan weights —
 * the size-aware replacement for MainPage's equal-per-unit index weighting
 * (#1382). ``completedUnits`` units are done in full; the running unit (index
 * ``completedUnits``) contributes ``withinUnitFraction`` (clamped to 0..1) of
 * its own weight share. Uses the SAME weights the countdown uses (skip-aware
 * seeds, delta-corrected by {@link observeUnitTotal} as units dispatch), so a
 * predicted-skip unit occupies no bar width and a huge platform occupies its
 * real share instead of ``1/totalUnits``.
 *
 * A zero-weight unit is not free: an empty delta still refreshes covers, so a
 * plan whose LEADING units all weigh zero would pin the bar to empty for as
 * long as they work (#1506). Those units claim an equal ``1/totalUnits`` share
 * each, and the weighted apportionment is compressed into the band ABOVE that
 * floor (not maxed against it), so the weight-bearing tail still fills smoothly
 * instead of stalling. Only the leading run is floored — a zero-weight unit
 * following real work still takes no width.
 *
 * Returns ``null`` — the caller falls back to index weighting — when no run is
 * measured (QAM opened mid-run before any plan, old backend), when the plan's
 * unit count doesn't match ``totalUnits`` (a stale plan from another run), or
 * when the total weight is zero (an all-predicted-skip plan has no widths to
 * apportion).
 */
export function weightedCoarseFraction(
  completedUnits: number,
  withinUnitFraction: number,
  totalUnits: number,
): number | null {
  if (_run === null) return null;
  const weights = _run.unitWeights;
  if (weights.length !== totalUnits) return null;
  let totalWeight = 0;
  for (const w of weights) totalWeight += Math.max(0, w);
  if (totalWeight <= 0) return null;
  let completedWeight = 0;
  for (let i = 0; i < completedUnits && i < weights.length; i++) {
    completedWeight += Math.max(0, weights[i] ?? 0);
  }
  const runningWeight =
    completedUnits >= 0 && completedUnits < weights.length ? Math.max(0, weights[completedUnits] ?? 0) : 0;
  const within = Math.max(0, Math.min(1, withinUnitFraction));
  const weighted = Math.min(1, (completedWeight + within * runningWeight) / totalWeight);
  const floor = zeroPrefixFloor(_run.zeroPrefix, completedUnits, within, totalUnits);
  return Math.min(1, floor + (1 - floor) * weighted);
}

/**
 * The bar share owed to the plan's LEADING zero-weight units: an equal
 * ``1/totalUnits`` each, interpolated by *within* while the running unit is
 * still inside that run and held at its full share once past it. Takes the
 * LATCHED ``zeroPrefix`` (never a live weight scan) and is non-decreasing in
 * every input, so the bar it floors can never move backwards.
 */
function zeroPrefixFloor(zeroPrefix: number, completedUnits: number, within: number, totalUnits: number): number {
  if (totalUnits <= 0) return 0;
  const reached = Math.min(Math.max(0, completedUnits), zeroPrefix);
  const stillInside = completedUnits < zeroPrefix;
  return (reached + (stillInside ? within : 0)) / totalUnits;
}
