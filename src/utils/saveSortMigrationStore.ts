/**
 * Module-level save sort migration state store.
 *
 * Updated by:
 *   - plugin load init in index.tsx (getSaveSortMigrationStatus callable)
 *   - refreshMigrationState return value in MainPage, RomMGameInfoPanel,
 *     launchInterceptor, sessionManager
 *   - clearSaveSortMigration() from SettingsPage after successful migration
 *
 * Read by:
 *   - MainPage.tsx, SettingsPage.tsx and RomMGameInfoPanel.tsx, all through
 *     {@link useSaveSortMigrationState}
 *
 * Every write installs a NEW status object and notifies. That is what lets
 * {@link getSaveSortMigrationState} serve as a `useSyncExternalStore` snapshot —
 * React compares snapshots by identity, so a getter handing back a fresh object
 * per call would re-render forever, and an in-place write would leave a
 * subscriber unable to tell that the status moved.
 */

import { useSyncExternalStore } from "react";
import type { SaveSortMigrationStatus } from "../types";

let _status: SaveSortMigrationStatus = { pending: false };
let _listeners: Array<() => void> = [];

export function setSaveSortMigrationStatus(status: SaveSortMigrationStatus): void {
  _status = status;
  _listeners.forEach((fn) => fn());
}

export function getSaveSortMigrationState(): SaveSortMigrationStatus {
  return _status;
}

export function clearSaveSortMigration(): void {
  _status = { pending: false };
  _listeners.forEach((fn) => fn());
}

export function onSaveSortMigrationChange(fn: () => void): () => void {
  _listeners.push(fn);
  return () => {
    _listeners = _listeners.filter((l) => l !== fn);
  };
}

/** Subscribe to the save-sort migration status from a component. Re-renders the
 *  caller whenever the status changes and drops its subscription on unmount. */
export function useSaveSortMigrationState(): SaveSortMigrationStatus {
  return useSyncExternalStore(onSaveSortMigrationChange, getSaveSortMigrationState);
}
