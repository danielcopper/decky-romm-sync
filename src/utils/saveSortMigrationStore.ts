/**
 * Module-level save sort migration state store.
 *
 * Updated by:
 *   - save_sort_changed event from backend (listener in index.tsx)
 *   - getSaveSortMigrationStatus() callable on plugin load
 *   - SettingsPage.tsx after successful migration
 *
 * Read by:
 *   - MainPage.tsx, SettingsPage.tsx, RomMGameInfoPanel.tsx
 */

import type { SaveSortMigrationStatus } from "../api/backend";

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
