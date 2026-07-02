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
 *   - PlaytimeScopeBanner (QAM)
 */

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
