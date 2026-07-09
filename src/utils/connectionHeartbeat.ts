/**
 * Connection heartbeat (#1345).
 *
 * While at least one game page is mounted, a single low-frequency timer probes
 * server reachability in BOTH directions: a failed probe flips the shared
 * connection state to `offline` (the server went away while the user sat on
 * the page), a successful one flips it back to `connected` (recovery without
 * user interaction). No page mounted — no polling. A module-level guard keeps
 * the timer single-instance so multiple mounted components never stack timers.
 */

import { probeReachability, debugLog } from "../api/backend";
import { reportServerReachable } from "./connectionState";
import { detach } from "./detach";

/** Probe cadence while a game page is open. Long enough to be unobtrusive,
 *  short enough that going offline or recovering feels automatic. */
export const CONNECTION_HEARTBEAT_INTERVAL_MS = 30_000;

let timer: ReturnType<typeof setInterval> | null = null;
let mountedPages = 0;

async function probeOnce(): Promise<void> {
  // probe_reachability never rejects on server problems — it resolves
  // {online: false} on any transport failure (single attempt, short timeout),
  // so both boolean outcomes are definitive verdicts and feed the store. A
  // rejected promise is a bridge error, not a server verdict: leave the store.
  try {
    const r = await probeReachability();
    reportServerReachable(r.online === true);
  } catch (e) {
    detach(debugLog(`connectionHeartbeat: probe failed (no verdict): ${e}`));
  }
}

/**
 * Register a mounted game page as a heartbeat owner. Starts the shared timer
 * with the first page; returns a cleanup that deregisters this owner and
 * stops the timer once the last page unmounts.
 */
export function registerConnectionHeartbeat(): () => void {
  mountedPages += 1;
  timer ??= setInterval(() => detach(probeOnce()), CONNECTION_HEARTBEAT_INTERVAL_MS);
  return () => {
    mountedPages -= 1;
    if (mountedPages <= 0) {
      mountedPages = 0;
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    }
  };
}
