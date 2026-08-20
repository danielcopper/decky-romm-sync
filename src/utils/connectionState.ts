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
import { debugLog } from "../api/backend";
import { detach } from "./detach";

export type RommConnectionState = "checking" | "connected" | "offline";

let _state: RommConnectionState = "checking";
const connectionListeners = new Set<(s: RommConnectionState) => void>();

export function getRommConnectionState(): RommConnectionState {
  return _state;
}

/** Set the connection state, notifying subscribers only when it actually
 *  changes. Feed real reachability signals through {@link reportServerReachable};
 *  this direct setter is for the play section's own `checking`/verdict flow.
 *
 *  `reason` names the signal that produced the transition, and is logged with
 *  it. A dozen call sites write this state and the user only ever sees the
 *  result, so without the reason on the transition itself a wrong "RomM offline"
 *  cannot be attributed to the signal that caused it from a log alone (#1670). */
export function setRommConnectionState(s: RommConnectionState, reason = "unspecified"): void {
  if (_state === s) return;
  const previous = _state;
  _state = s;
  detach(debugLog(`connectionState: ${previous} -> ${s} (${reason})`));
  connectionListeners.forEach((l) => l(s));
}

/** Single funnel for reachability signals from any server-touching call or
 *  probe: `true` (the server responded) → `connected`, `false` (a definitive
 *  `SERVER_UNREACHABLE` / offline probe) → `offline`. Only feed it signals that
 *  genuinely mean the server is reachable or not — never an arbitrary error. */
export function reportServerReachable(ok: boolean): void {
  setRommConnectionState(ok ? "connected" : "offline", "reachability report");
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

/**
 * Server retry-ladder progress (#1345).
 *
 * The backend HTTP adapter retries transient failures with exponential backoff
 * (up to ~13s over 3 attempts) inside worker threads. It emits a
 * `server_retry_progress` event per retry, which `index.tsx` funnels into this
 * store, so a saves surface waiting on a server-touching call can show a live
 * "Connecting to RomM… (attempt N/M)" spinner instead of a frozen one. `null`
 * means no retry is in flight — the consumer clears it once its own load
 * settles (a retry ladder emits no "done" event).
 */
export interface ServerRetryProgress {
  /** 1-based number of the retry currently being attempted. */
  attempt: number;
  /** Total attempts the ladder will make before giving up. */
  maxAttempts: number;
}

let _retryProgress: ServerRetryProgress | null = null;
/** The load {@link beginServerLoad} was on when the shown frame was accepted.
 *  What makes "within one load" answerable at the moment a frame arrives. */
let _retryProgressGeneration = 0;
let _loadGeneration = 0;
const retryProgressListeners = new Set<(p: ServerRetryProgress | null) => void>();

export function getServerRetryProgress(): ServerRetryProgress | null {
  return _retryProgress;
}

/** Set (or clear, with `null`) the current retry progress and notify
 *  subscribers. A no-op when clearing an already-clear store so a consumer's
 *  post-load reset doesn't churn renders.
 *
 *  Within one load the shown attempt only ever climbs. Every lane's ladder
 *  writes this ONE slot and each emits its own frames, so the number on screen is
 *  otherwise whichever lane wrote last — a user watching a single load sees
 *  "attempt 3/3" walk back to "2/3" and up again (#1758). A lower frame is
 *  dropped rather than shown; the next load starts the climb over, which is the
 *  boundary {@link beginServerLoad} already draws. */
export function setServerRetryProgress(p: ServerRetryProgress | null): void {
  if (p === null) {
    if (_retryProgress === null) return;
    _retryProgress = null;
  } else {
    const shown = _retryProgress;
    if (shown !== null && _retryProgressGeneration === _loadGeneration && p.attempt < shown.attempt) return;
    _retryProgress = p;
    _retryProgressGeneration = _loadGeneration;
  }
  retryProgressListeners.forEach((l) => l(_retryProgress));
}

export function onServerRetryProgressChange(cb: (p: ServerRetryProgress | null) => void): () => void {
  retryProgressListeners.add(cb);
  return () => {
    retryProgressListeners.delete(cb);
  };
}

/** One load's claim on the retry-progress store, handed out by
 *  {@link beginServerLoad} and read only by {@link settleServerLoad}. */
export interface ServerLoadToken {
  readonly generation: number;
}

/** Claim the retry-progress store for a load that is starting now.
 *
 *  Several lazy lanes on the game detail page — the panel's save-slot load, the
 *  achievements tab's load — feed this ONE store and run concurrently: a lane
 *  torn down mid-flight still settles later. Counting claims across all lanes
 *  rather than per lane is what makes that late settle distinguishable from the
 *  newest one (#1345 F2). */
export function beginServerLoad(): ServerLoadToken {
  _loadGeneration += 1;
  return { generation: _loadGeneration };
}

/** Drop the retry frame a settling load was showing — but only while its claim
 *  is still the newest one taken out by ANY lane. A stale load resolving late
 *  must not wipe the live "(attempt N/M)" frame a newer load now owns. */
export function settleServerLoad(token: ServerLoadToken): void {
  if (token.generation === _loadGeneration) setServerRetryProgress(null);
}

/** Subscribe to the retry-progress store. Re-renders the caller on every
 *  change; cleans up its listener on unmount. */
export function useServerRetryProgress(): ServerRetryProgress | null {
  const [progress, setProgress] = useState<ServerRetryProgress | null>(getServerRetryProgress());
  useEffect(() => onServerRetryProgressChange(setProgress), []);
  return progress;
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
