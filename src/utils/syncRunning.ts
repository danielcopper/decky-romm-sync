/**
 * "Is a library sync in flight?", read live off the module-level sync-progress
 * store, plus the one sentence every surface says about it.
 *
 * Each consumer subscribes itself rather than taking a snapshot as a prop: one
 * of them is rendered by `showModal` into a detached tree that never re-renders
 * with the panel, so a prop taken at open time would go stale while it is open.
 *
 * The backend refuses the removal callables with reason `"sync_active"` while a
 * run is in flight (#1390); the hint is what the UI says in place of letting the
 * user press into that refusal, and it is shared so the two surfaces that make
 * the same promise cannot word it two ways.
 */

import { useEffect, useState } from "react";
import { getSyncProgress, onSyncProgressChange } from "./syncProgress";

export const SYNC_RUNNING_HINT = "Unavailable while a library sync is running.";

export const useSyncRunning = (): boolean => {
  const [syncRunning, setSyncRunning] = useState(getSyncProgress().running);
  useEffect(() => onSyncProgressChange(() => setSyncRunning(getSyncProgress().running)), []);
  return syncRunning;
};
