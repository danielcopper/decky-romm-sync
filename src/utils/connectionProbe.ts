/**
 * The QAM connection row's backend probe, owned at module level so it outlives
 * the panel that displays it.
 *
 * The probe needs up to ~87s to reach a verdict — six `test_connection()`
 * attempts spread across a backoff ladder, then a liveness ping — because that
 * window is what lets it ride out a slow cold boot instead of calling a
 * still-starting backend dead (#1045). Owned by the panel's `useEffect`, that
 * budget was unreachable in practice: closing the QAM cancelled the run and
 * reopening restarted it at attempt 0, so anyone who did not hold the panel open
 * for the full window saw nothing but "Checking…", forever (#1730). Hoisting the
 * run out of the component decouples the two: the ladder finishes on its own
 * schedule, and a panel that mounts mid-run adopts the verdict when it lands.
 *
 * A mount re-probes rather than trusting what is stored — a backend reloaded
 * since the last verdict has to be able to recover the row — but it does NOT
 * clear the stored verdict first. Showing the last answer while the next one is
 * computed is the whole point; resetting to "Checking…" on every open would
 * reintroduce the symptom.
 */

import { useEffect, useState } from "react";
import type { RommErrorCode } from "../types";
import { getSettings, testConnection } from "../api/backend";
import { setVersionError } from "./connectionState";
import { detach } from "./detach";
import { withTimeout } from "./withTimeout";

// Each attempt is raced against a deadline because the callable hangs (rather
// than rejects) while the backend is still starting. The schedule mirrors the
// metadata init loop's tuned window in index.tsx (#1203).
const CONNECTION_RETRY_DELAYS = [2000, 5000, 10000, 15000, 20000];
const CONNECTION_CALLABLE_TIMEOUT = 5000;

/** Backend never answered after the retry budget — distinct from `false` ("not connected"). */
export type BackendFailed = "backend_failed";

/** A resolved-but-failed `test_connection()` probe. `reason`/`message` classify
 *  why so the connection row shows a specific label instead of a bare "Not
 *  connected"; both are absent when the probe never resolved (an unreachable
 *  server that hung past every deadline), which reads as the generic label. */
export interface ConnectionFailure {
  reason: RommErrorCode | undefined;
  message: string;
}

export interface ConnectionProbeState {
  /** `null` until the first verdict lands — the row's "Checking…" state. */
  connected: boolean | null | BackendFailed;
  failure: ConnectionFailure | null;
}

let _state: ConnectionProbeState = { connected: null, failure: null };
let _running = false;
const listeners = new Set<(s: ConnectionProbeState) => void>();

function publish(next: ConnectionProbeState): void {
  _state = next;
  listeners.forEach((l) => l(next));
}

export function getConnectionProbeState(): ConnectionProbeState {
  return _state;
}

export function onConnectionProbeChange(cb: (s: ConnectionProbeState) => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** Run the ladder to a verdict. A resolved call ends it — "not connected"
 *  (success:false) is an authoritative answer, not a failure — so only an
 *  exhausted budget reaches the liveness ping. */
async function runProbe(): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    try {
      const r = await withTimeout(testConnection(), CONNECTION_CALLABLE_TIMEOUT);
      publish({ connected: r.success, failure: r.success ? null : { reason: r.reason, message: r.message } });
      setVersionError(r.reason === "version_error" ? r.message : null);
      return;
    } catch {
      if (attempt >= CONNECTION_RETRY_DELAYS.length) {
        // Retry budget exhausted. test_connection() also waits out the server
        // round-trip — a hanging RomM server keeps the backend's retrying
        // heartbeat busy for up to ~90s, far past our per-attempt deadline — so
        // an exhausted budget alone can't tell a dead backend from an
        // unreachable server. Ping get_settings (a pure in-memory read that
        // resolves iff the backend RPC bridge is alive) to decide: alive ⇒ the
        // server is merely unreachable ("Not connected"); dead ⇒ the backend
        // never came up ("Backend error").
        try {
          await withTimeout(getSettings(), CONNECTION_CALLABLE_TIMEOUT);
          publish({ connected: false, failure: null });
        } catch (pingErr) {
          publish({ connected: "backend_failed", failure: null });
          // logError is itself a callable and would hang against a dead
          // backend — log to the console instead.
          console.error("[RomM] backend RPC bridge unreachable (get_settings ping failed):", pingErr);
        }
        return;
      }
      await new Promise<void>((resolve) => setTimeout(resolve, CONNECTION_RETRY_DELAYS[attempt]));
    }
  }
}

/** Start a probe unless one is already in flight. Re-entrant by design: every
 *  panel mount asks for a fresh verdict, and the guard collapses the concurrent
 *  asks into the single run they are all waiting on. */
export function ensureConnectionProbe(): void {
  if (_running) return;
  _running = true;
  detach(
    runProbe().finally(() => {
      _running = false;
    }),
  );
}

/** Subscribe to the probe's verdict and ask for a fresh one. Re-renders the
 *  caller on every change; cleans up its listener on unmount without disturbing
 *  a run still in flight.
 *
 *  Seeding from the store in the initializer and subscribing in the effect
 *  covers every verdict without a resync between the two: a publish only ever
 *  originates from a timer or a settled promise, neither of which can interleave
 *  with React's synchronous render-to-commit. */
export function useConnectionProbe(): ConnectionProbeState {
  const [state, setState] = useState<ConnectionProbeState>(getConnectionProbeState());
  useEffect(() => {
    const unsubscribe = onConnectionProbeChange(setState);
    ensureConnectionProbe();
    return unsubscribe;
  }, []);
  return state;
}

/** Reset the module state between tests. Not for production use. */
export function resetConnectionProbeForTests(): void {
  _state = { connected: null, failure: null };
  _running = false;
  listeners.clear();
}
