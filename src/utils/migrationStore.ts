/**
 * Module-level migration state store.
 *
 * Updated by:
 *   - plugin load init in index.tsx (getMigrationStatus callable)
 *   - refreshMigrationState return value in MainPage, RomMGameInfoPanel,
 *     launchInterceptor, sessionManager
 *   - clearMigration() from SettingsPage after successful migration
 *
 * Read by:
 *   - MainPage.tsx and ConnectionSettings.tsx
 */

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
