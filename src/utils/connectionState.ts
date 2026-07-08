/**
 * Shared RomM connection state.
 *
 * A subscribable module-level store (mirrors the version-error half below): the
 * play section's mount check, every server-touching call, and the offline
 * recovery probe feed reachability signals in via {@link reportServerReachable}
 * / {@link setRommConnectionState}, and every surface that reflects
 * connectivity (the play-row offline badge, the Download button, the saves-tab
 * banner) subscribes so it re-derives live instead of only at mount (#1345).
 */

import { useEffect, useState } from "react";

export type RommConnectionState = "checking" | "connected" | "offline";

let _state: RommConnectionState = "checking";
const connectionListeners = new Set<(s: RommConnectionState) => void>();

export function getRommConnectionState(): RommConnectionState {
  return _state;
}

/** Set the connection state, notifying subscribers only when it actually
 *  changes. Feed real reachability signals through {@link reportServerReachable};
 *  this direct setter is for the play section's own `checking`/verdict flow. */
export function setRommConnectionState(s: RommConnectionState): void {
  if (_state === s) return;
  _state = s;
  connectionListeners.forEach((l) => l(s));
}

/** Single funnel for reachability signals from any server-touching call or
 *  probe: `true` (the server responded) → `connected`, `false` (a definitive
 *  `SERVER_UNREACHABLE` / offline probe) → `offline`. Only feed it signals that
 *  genuinely mean the server is reachable or not — never an arbitrary error. */
export function reportServerReachable(ok: boolean): void {
  setRommConnectionState(ok ? "connected" : "offline");
}

export function onRommConnectionChange(cb: (s: RommConnectionState) => void): () => void {
  connectionListeners.add(cb);
  return () => {
    connectionListeners.delete(cb);
  };
}

/** Subscribe to the shared connection state. Re-renders the caller whenever the
 *  state changes; cleans up its listener on unmount. */
export function useRommConnectionState(): RommConnectionState {
  const [state, setState] = useState<RommConnectionState>(getRommConnectionState());
  useEffect(() => onRommConnectionChange(setState), []);
  return state;
}

/** Version mismatch error — set when server returns reason: "version_error" */
let _versionError: string | null = null;
const versionErrorListeners = new Set<(err: string | null) => void>();

export function getVersionError() {
  return _versionError;
}
export function setVersionError(msg: string | null) {
  if (_versionError === msg) return;
  _versionError = msg;
  versionErrorListeners.forEach((l) => l(msg));
}
export function onVersionErrorChange(cb: (err: string | null) => void): () => void {
  versionErrorListeners.add(cb);
  return () => {
    versionErrorListeners.delete(cb);
  };
}
