/**
 * Module-level migration state store.
 *
 * Updated by:
 *   - plugin load init in index.tsx (getMigrationStatus callable)
 *   - refreshMigrationState return value in MainPage, RomMGameInfoPanel,
 *     launchInterceptor, sessionManager
 *   - clearMigration() from MigrationBlockedPage, on a successful migration and
 *     on a successful dismiss
 *
 * Read by:
 *   - MainPage.tsx, RomMGameInfoPanel.tsx and RomMPlaySection.tsx, all through
 *     {@link useMigrationStatus}
 *   - CustomPlayButton.tsx and launchInterceptor.ts, which read the current
 *     status through {@link getMigrationState} outside of any render
 *
 * Every write installs a NEW status object and notifies. That is what lets
 * {@link getMigrationState} serve as a `useSyncExternalStore` snapshot — React
 * compares snapshots by identity, so a getter handing back a fresh object per
 * call would re-render forever, and an in-place write would leave a subscriber
 * unable to tell that the status moved.
 */

import { useSyncExternalStore } from "react";
import type { MigrationStatus } from "../types";

let _migration: MigrationStatus = { pending: false };
let _listeners: Array<() => void> = [];

export function setMigrationStatus(status: MigrationStatus): void {
  _migration = status;
  _listeners.forEach((fn) => fn());
}

export function getMigrationState(): MigrationStatus {
  return _migration;
}

export function clearMigration(): void {
  _migration = { pending: false };
  _listeners.forEach((fn) => fn());
}

export function onMigrationChange(fn: () => void): () => void {
  _listeners.push(fn);
  return () => {
    _listeners = _listeners.filter((l) => l !== fn);
  };
}

/** Subscribe to the RetroDECK migration status from a component. Re-renders the
 *  caller whenever the status changes and drops its subscription on unmount. */
export function useMigrationStatus(): MigrationStatus {
  return useSyncExternalStore(onMigrationChange, getMigrationState);
}
