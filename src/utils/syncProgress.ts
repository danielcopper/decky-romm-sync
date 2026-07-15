/**
 * Module-level sync progress store — single source of truth.
 *
 * Updated by:
 *   - sync_progress events from the backend (persistent listener in index.tsx)
 *   - syncManager.ts during the frontend applying phase
 *   - MainPage on mount via getSyncStatus() (backend-authoritative seed)
 *   - MainPage on handleSync click (optimistic running:true)
 *
 * Read by:
 *   - MainPage.tsx, which subscribes via onSyncProgressChange and re-renders
 *     on every notify (no setInterval polling), and reads ``runId`` to scope a
 *     Cancel click to the active run (#1202).
 *
 * The ``runId`` field is fed straight from the backend ``sync_progress`` payload
 * (the persistent listener in index.tsx passes the whole event through), so it
 * is the single source of run identity frontend-side.
 */

import type { SyncProgress } from "../types";

/**
 * Static sub-slice shares of a running unit's coarse-bar width (#1407). A unit
 * is worked in three sequential phases — fetch (paginate the ROM list), covers
 * (download/refresh cover art), apply (create/update the Steam shortcuts) — and
 * each phase owns a fixed fraction of the unit's slice, so the bar advances
 * continuously through the whole unit instead of resting frozen until
 * ``applying``. The three sum to 1; apply dominates because it is the phase the
 * user waits on longest per shortcut. First-cut static weights (the issue's
 * agreed 15/25/60); the #1382 plan already knows per-unit item counts if
 * smarter per-phase apportioning is ever wanted.
 */
export const FETCH_SHARE = 0.15;
export const COVERS_SHARE = 0.25;
export const APPLY_SHARE = 0.6;

/**
 * The within-unit fill fraction (0..1) for the running unit, placed in the
 * phase's own sub-slice so the coarse bar never jumps backwards at a
 * fetch→covers→apply boundary (#1407). Each phase's slice fills by that phase's
 * own ``current/total``; a later phase's slice floor sits at the sum of the
 * earlier phases' shares, so transitions only ever move the bar forward — the
 * fetch and cover frames each restart ``current/total`` from zero, but land in a
 * strictly-higher band than the phase before.
 *
 * Phase resolution:
 *   - ``applying`` → the whole fetch+covers width is done; fill the apply slice
 *     (keyed on the stage alone, so a merged frontend apply frame that still
 *     carries a stale ``subStage`` is unaffected).
 *   - ``fetching`` + ``subStage: "covers"`` → fetch slice done, fill covers.
 *   - ``fetching`` + ``subStage: "fetch"`` → fill the fetch slice.
 *   - ``fetching`` with no sub-stage (the unit's coarse anchor, or an old
 *     backend) → 0: rest at the unit floor, the pre-#1407 behaviour.
 *   - any other stage (discovering, finalizing, …) → 0.
 *
 * A falsy ``current``/``total`` yields the phase's floor (its share sum so far),
 * e.g. a covers frame with ``total: 0`` reads ``FETCH_SHARE`` — never a divide.
 */
export function withinUnitFraction(progress: SyncProgress | null | undefined): number {
  const current = progress?.current ?? 0;
  const total = progress?.total ?? 0;
  const frac = current > 0 && total > 0 ? Math.min(1, current / total) : 0;
  if (progress?.stage === "applying") {
    return FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * frac;
  }
  if (progress?.stage === "fetching") {
    if (progress.subStage === "covers") return FETCH_SHARE + COVERS_SHARE * frac;
    if (progress.subStage === "fetch") return FETCH_SHARE * frac;
  }
  return 0;
}

let _progress: SyncProgress = {
  running: false,
  stage: "",
  current: 0,
  total: 0,
  message: "",
  runId: "",
};
let _listeners: Array<() => void> = [];

/**
 * Notify every subscriber, each inside its own try/catch — a throwing subscriber
 * can neither starve later listeners nor break the emitting call site (e.g. the
 * per-item apply loop in syncManager, where a subscriber throw would otherwise
 * skip that game's shortcut creation). Console, not the ``logError`` backend
 * callable, at this store layer.
 */
function notify(): void {
  _listeners.forEach((fn) => {
    try {
      fn();
    } catch (e) {
      console.error("[RomM] sync-progress listener threw:", e);
    }
  });
}

export function setSyncProgress(p: SyncProgress): void {
  _progress = p;
  notify();
}

export function updateSyncProgress(p: Partial<SyncProgress>): void {
  _progress = { ..._progress, ...p };
  notify();
}

export function getSyncProgress(): SyncProgress {
  return _progress;
}

export function onSyncProgressChange(fn: () => void): () => void {
  _listeners.push(fn);
  return () => {
    _listeners = _listeners.filter((l) => l !== fn);
  };
}
