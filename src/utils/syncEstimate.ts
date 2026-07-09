/**
 * Sync-time estimate — the pure cost model behind the always-on "Estimated
 * time" readout. Given a count of new and updated shortcuts it returns an
 * expected apply duration; anything that turns item counts into a
 * human-readable duration for the sync UI lives here. No I/O, no React.
 *
 * The per-item constants are the apply pipeline's dominant costs, not a
 * benchmark: a new shortcut pays the AddShortcut settle wait before its Set*
 * calls, an updated one does not. They are deliberately generous so the
 * preview estimate and the skip-preview upper bound never read short.
 */

/**
 * Seconds per newly created shortcut: the AddShortcut ~500ms settle wait plus
 * the Set* calls, the confirm poll, the inter-op cadence, and amortized artwork
 * fetch/apply.
 */
export const NEW_ITEM_SEC = 0.85;

/**
 * Seconds per updated shortcut: the Set* calls and confirm poll only — the
 * update path reuses the existing shortcut, so it skips the AddShortcut wait.
 */
export const UPDATED_ITEM_SEC = 0.35;

/**
 * Expected apply duration in seconds for *newCount* created and *changedCount*
 * updated shortcuts. Negative inputs are clamped to zero.
 */
export function estimateApplySeconds(newCount: number, changedCount: number): number {
  const created = Math.max(0, newCount);
  const updated = Math.max(0, changedCount);
  return created * NEW_ITEM_SEC + updated * UPDATED_ITEM_SEC;
}

/**
 * Render *seconds* as a coarse, approximate duration for the UI:
 * ``"< 1 min"`` under a minute, ``"~4 min"`` under an hour, ``"~1 h 10 min"``
 * (or ``"~1 h"`` on the hour) beyond. The leading ``~`` signals the value is an
 * estimate, not a countdown.
 */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return "< 1 min";
  const totalMinutes = Math.round(seconds / 60);
  if (totalMinutes < 60) return `~${totalMinutes} min`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes > 0 ? `~${hours} h ${minutes} min` : `~${hours} h`;
}
