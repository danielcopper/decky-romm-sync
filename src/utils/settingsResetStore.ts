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
 *   - MainPage.tsx and RomMGameInfoPanel.tsx, both through
 *     {@link useSettingsResetState}, which decide whether to render the
 *     SettingsResetBanner (QAM) / SettingsResetCard (game detail)
 *
 * Every write installs a NEW state object and notifies. That is what lets
 * {@link getSettingsResetState} serve as a `useSyncExternalStore` snapshot —
 * React compares snapshots by identity, so a getter handing back a fresh object
 * per call would re-render forever, and an in-place write would leave a
 * subscriber unable to tell that the notice moved.
 */

import { useSyncExternalStore } from "react";
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

/** Subscribe to the corrupt-settings-reset notice from a component. Re-renders
 *  the caller whenever it changes and drops its subscription on unmount. */
export function useSettingsResetState(): SettingsResetState {
  return useSyncExternalStore(onSettingsResetChange, getSettingsResetState);
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
