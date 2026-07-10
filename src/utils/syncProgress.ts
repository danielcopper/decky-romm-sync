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
