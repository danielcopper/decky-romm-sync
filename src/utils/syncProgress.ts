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
 *     on every notify (no setInterval polling).
 */

import type { SyncProgress } from "../types";

let _progress: SyncProgress = {
  running: false,
  stage: "",
  current: 0,
  total: 0,
  message: "",
};
let _listeners: Array<() => void> = [];

export function setSyncProgress(p: SyncProgress): void {
  _progress = p;
  _listeners.forEach((fn) => fn());
}

export function updateSyncProgress(p: Partial<SyncProgress>): void {
  _progress = { ..._progress, ...p };
  _listeners.forEach((fn) => fn());
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
