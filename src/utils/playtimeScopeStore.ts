/**
 * Module-level playtime-scope-notice state store.
 *
 * Mirrors settingsResetStore: a backend-state-driven persistent notice surfaced
 * as an account-wide QAM banner (no toast, no per-game card). The backend
 * persists a durable flag when a playtime reconcile is rejected because the
 * Client API Token lacks the `roms.user.read` scope needed to read cross-device
 * playtime; it clears automatically once a scoped token is present (a later
 * successful reconcile, or a fresh sign-in).
 *
 * Updated by:
 *   - MainPage mount (fetchPlaytimeScopeState)
 *   - PlaytimeScopeBanner Dismiss button (setPlaytimeScopeState — local dismiss)
 *
 * Read by:
 *   - MainPage.tsx through {@link usePlaytimeScopeState}, which decides whether
 *     to render the PlaytimeScopeBanner (QAM)
 *
 * Every write installs a NEW state object and notifies. That is what lets
 * {@link getPlaytimeScopeState} serve as a `useSyncExternalStore` snapshot —
 * React compares snapshots by identity, so a getter handing back a fresh object
 * per call would re-render forever, and an in-place write would leave a
 * subscriber unable to tell that the notice moved.
 */

import { useSyncExternalStore } from "react";
import { getPlaytimeScopeNotice } from "../api/backend";

export interface PlaytimeScopeState {
  pending: boolean;
}

let _state: PlaytimeScopeState = { pending: false };
let _listeners: Array<() => void> = [];

export function setPlaytimeScopeState(state: PlaytimeScopeState): void {
  _state = state;
  _listeners.forEach((fn) => fn());
}

export function getPlaytimeScopeState(): PlaytimeScopeState {
  return _state;
}

export function onPlaytimeScopeChange(fn: () => void): () => void {
  _listeners.push(fn);
  return () => {
    _listeners = _listeners.filter((l) => l !== fn);
  };
}

/** Subscribe to the playtime-scope notice from a component. Re-renders the
 *  caller whenever it changes and drops its subscription on unmount. */
export function usePlaytimeScopeState(): PlaytimeScopeState {
  return useSyncExternalStore(onPlaytimeScopeChange, getPlaytimeScopeState);
}

/**
 * Fetch the backend notice and update the store. Returns the resolved state so
 * callers (e.g. a post-sign-in refetch) can react without re-reading the store.
 */
export async function fetchPlaytimeScopeState(): Promise<PlaytimeScopeState> {
  const notice = await getPlaytimeScopeNotice();
  const next: PlaytimeScopeState = { pending: notice.pending };
  setPlaytimeScopeState(next);
  return next;
}
