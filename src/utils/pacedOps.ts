/**
 * Shared paced iteration for bulk Steam-shortcut operations.
 *
 * Steam mutates its in-memory shortcut store on every add/remove; driving those
 * mutations in a tight synchronous loop blocks the CEF renderer and can corrupt
 * that store (the documented removal-churn hazard, #977). Every bulk shortcut
 * loop — the sync add path and the DangerZone removal paths — runs through
 * {@link pacedForEach} so the pacing is defined in exactly one place and add and
 * remove can never diverge again. The add path paces one item at a time (the
 * CEF-safe 50ms cadence); bulk removals pace in larger chunks (a removal is a
 * single cheap call, so 25 back-to-back then one breather keeps the renderer
 * responsive at seconds of overhead instead of minutes).
 */

import { logError } from "../api/backend";

/** Resolve after ``ms`` milliseconds — the one timer primitive shared across the shortcut utils. */
export const delay = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

export interface PacedForEachOptions {
  /**
   * Items processed back-to-back before a breather delay. Default ``1`` — a
   * breather after every item (the per-item add cadence). Larger values pace in
   * chunks (bulk removals).
   */
  chunkSize?: number;
  /** Breather duration between chunks, in ms. Default ``50`` (the CEF-safe add cadence). */
  delayMs?: number;
  /**
   * Called after each item so a long run can keep an external watchdog alive
   * (the sync per-unit heartbeat). Throttled to at most once per
   * {@link heartbeatIntervalMs}; omit for operations with no watchdog (removals).
   */
  heartbeat?: () => void;
  /** Throttle window for {@link heartbeat}, in ms. Default ``10_000``. */
  heartbeatIntervalMs?: number;
  /**
   * Checked after each item; returning ``true`` stops the loop before the next
   * item. Omit for operations that can't be cancelled (removals).
   */
  isCancelled?: () => boolean;
}

/**
 * Iterate *items*, awaiting *fn* for each, pacing the loop so the CEF renderer
 * stays responsive. After every ``chunkSize`` items — but never after the last —
 * the loop waits ``delayMs`` (one breather per chunk, no trailing delay).
 * Returns ``false`` when ``isCancelled`` stopped the loop early, ``true`` when
 * every item ran.
 *
 * Per-item mode (``chunkSize`` 1) is this same body with one item per chunk: a
 * breather after every item except the last. The add path and bulk removals
 * share the one body — the only difference is the chunk size.
 *
 * Batch resilience: a *fn* that throws is logged and skipped, never aborting the
 * batch — one bad item can't strand the rest. Consumers that need item-specific
 * error reporting still catch inside *fn* (this is the last-resort backstop).
 */
export async function pacedForEach<T>(
  items: readonly T[],
  fn: (item: T, index: number) => void | Promise<void>,
  options: PacedForEachOptions = {},
): Promise<boolean> {
  const { chunkSize = 1, delayMs = 50, heartbeat, heartbeatIntervalMs = 10_000, isCancelled } = options;
  let lastHeartbeat = Date.now();
  for (const [i, item] of items.entries()) {
    try {
      await fn(item, i);
    } catch (e) {
      logError(`pacedForEach: item ${i} failed: ${e}`);
    }
    const done = i + 1;
    if (done % chunkSize === 0 && done < items.length) {
      await delay(delayMs);
    }
    if (heartbeat && Date.now() - lastHeartbeat > heartbeatIntervalMs) {
      heartbeat();
      lastHeartbeat = Date.now();
    }
    if (isCancelled?.()) return false;
  }
  return true;
}
