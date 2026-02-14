/**
 * Module-level sync progress store — single source of truth.
 *
 * Updated by:
 *   - sync_progress events from the backend (persistent listener in index.tsx)
 *   - syncManager.ts during the frontend applying phase
 *
 * Read by:
 *   - MainPage.tsx via a cheap setInterval (no callable round-trips)
 */

import type { SyncProgress } from "../types";

let _progress: SyncProgress = {
  running: false,
  phase: "",
  current: 0,
  total: 0,
  message: "",
};

export function setSyncProgress(p: SyncProgress): void {
  _progress = p;
}

export function updateSyncProgress(p: Partial<SyncProgress>): void {
  _progress = { ..._progress, ...p };
}

export function getSyncProgress(): SyncProgress {
  return _progress;
}
