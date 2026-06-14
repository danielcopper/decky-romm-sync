/**
 * Module-level corrupt-settings-reset state store.
 *
 * Mirrors migrationStore: a backend-state-driven persistent notice surfaced as
 * a QAM banner + game-detail card (no toast). The backend persists a marker
 * into settings.json when an unparseable file is quarantined at boot; it is
 * cleared only by an explicit user ack in the QAM (dismiss_settings_reset_notice).
 *
 * Updated by:
 *   - plugin load init in index.tsx (fetchSettingsResetState)
 *   - SettingsResetBanner Dismiss button (setSettingsResetState on ack success)
 *
 * Read by:
 *   - SettingsResetBanner (QAM) and SettingsResetCard (game detail)
 */

import { getSettingsResetNotice } from "../api/backend";

export interface SettingsResetState {
  pending: boolean;
  backedUpTo: string | null;
}

let _state: SettingsResetState = { pending: false, backedUpTo: null };
let _listeners: Array<() => void> = [];

export function setSettingsResetState(state: SettingsResetState): void {
  _state = state;
  _listeners.forEach((fn) => fn());
}

export function getSettingsResetState(): SettingsResetState {
  return _state;
}

export function onSettingsResetChange(fn: () => void): () => void {
  _listeners.push(fn);
  return () => {
    _listeners = _listeners.filter((l) => l !== fn);
  };
}

/**
 * Fetch the backend notice and update the store. Returns the resolved state so
 * callers (e.g. a post-sign-in refetch) can react without re-reading the store.
 */
export async function fetchSettingsResetState(): Promise<SettingsResetState> {
  const notice = await getSettingsResetNotice();
  const next: SettingsResetState = { pending: notice.pending, backedUpTo: notice.backed_up_to };
  setSettingsResetState(next);
  return next;
}
