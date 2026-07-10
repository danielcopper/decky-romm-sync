/**
 * Sync-time estimate — the pure cost model behind the always-on "Estimated
 * time" readout. Given a count of new and updated shortcuts it returns an
 * expected apply duration; anything that turns item counts into a
 * human-readable duration for the sync UI lives here. No I/O, no React.
 *
 * The per-item constants are calibrated to the measured post-poll apply rates
 * (2026-07 on-device, after the AddShortcut overview-poll replaced the blind
 * ~500ms settle wait): a new shortcut runs ~0.30–0.36 s/item, an updated one
 * ~0.15 s/item. The constants sit deliberately above those means so the preview
 * estimate and the skip-preview upper bound never read short. A flat fetch
 * allowance folded into ``estimateApplySeconds`` covers the multi-page
 * fetch/prep phases the per-item model would otherwise ignore.
 */

/**
 * Seconds per newly created shortcut — calibrated to the measured ~0.30–0.36
 * s/item post-poll create rate (2026-07 on-device), with a deliberate ceiling
 * margin. Covers the AddShortcut + overview poll, the Set* calls, the confirm
 * poll, the inter-op cadence, and amortized artwork fetch/apply.
 */
export const NEW_ITEM_SEC = 0.45;

/**
 * Seconds per updated shortcut — calibrated to the measured ~0.15 s/item update
 * rate (2026-07 on-device), with a deliberate ceiling margin. The update path
 * reuses the existing shortcut (no AddShortcut), so it is just the Set* calls
 * and confirm poll.
 */
export const UPDATED_ITEM_SEC = 0.2;

/**
 * Expected apply duration in seconds for *newCount* created and *changedCount*
 * updated shortcuts, plus a flat fetch allowance for the multi-page fetch/prep
 * phases that precede and interleave the apply. Negative counts are clamped to
 * zero (the allowance still applies).
 */
export function estimateApplySeconds(newCount: number, changedCount: number): number {
  // Flat allowance for the fetch/prep phases the per-item model ignores (the
  // multi-page ROM + save fetches a resume re-runs, ~1–1.5 min on-device).
  // Small libraries overshoot slightly; the "up to ~X" / "~X min" wording covers
  // it and the live countdown corrects downward within seconds of applying.
  const FETCH_ALLOWANCE_SEC = 90;
  const created = Math.max(0, newCount);
  const updated = Math.max(0, changedCount);
  return created * NEW_ITEM_SEC + updated * UPDATED_ITEM_SEC + FETCH_ALLOWANCE_SEC;
}

/**
 * Shared coarse-duration renderer for the sync UI's two readouts. Renders
 * *seconds* as ``"< 1 min"`` under a minute, ``"~N min"`` under an hour, and
 * ``"~H h M min"`` / ``"~H h"`` (on the hour) beyond, appending *suffix* to every
 * branch. *roundMinutes* controls the minute rounding: ``Math.round`` for the
 * neutral estimate, ``Math.ceil`` for the live countdown (which must never promise
 * less time than it expects). The leading ``~`` signals an estimate.
 */
export function formatApproxDuration(seconds: number, roundMinutes: (n: number) => number, suffix: string): string {
  if (seconds < 60) return `< 1 min${suffix}`;
  const totalMinutes = roundMinutes(seconds / 60);
  if (totalMinutes < 60) return `~${totalMinutes} min${suffix}`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes > 0 ? `~${hours} h ${minutes} min${suffix}` : `~${hours} h${suffix}`;
}

/**
 * Render *seconds* as a coarse, approximate duration for the UI:
 * ``"< 1 min"`` under a minute, ``"~4 min"`` under an hour, ``"~1 h 10 min"``
 * (or ``"~1 h"`` on the hour) beyond. The neutral estimate rounds to the nearest
 * minute and carries no suffix.
 */
export function formatDuration(seconds: number): string {
  return formatApproxDuration(seconds, Math.round, "");
}
