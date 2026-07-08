/**
 * Offline recovery probe (#1345).
 *
 * While the shared connection state is `offline` AND at least one game page is
 * mounted, a single low-frequency timer re-probes reachability so the plugin
 * detects the server coming back without any user interaction; on success the
 * store flips to `connected`, which re-enables every subscribed surface. There
 * is deliberately NO polling while connected — the timer only runs in the
 * offline state. A module-level guard keeps it single-instance so multiple
 * mounted components (play section, info panel) never stack timers.
 */

import { probeReachability, debugLog } from "../api/backend";
import { getRommConnectionState, onRommConnectionChange, reportServerReachable } from "./connectionState";
import { detach } from "./detach";

/** Re-probe cadence while offline. Long enough to be unobtrusive, short enough
 *  that recovery feels automatic to a user waiting on a game page. */
export const OFFLINE_RECOVERY_INTERVAL_MS = 30_000;

let timer: ReturnType<typeof setInterval> | null = null;
let mountedPages = 0;
let unsubscribe: (() => void) | null = null;

async function probeOnce(): Promise<void> {
  // Only a definitive positive result flips the store; a throw or a still-offline
  // probe leaves it offline (the next tick retries). reportServerReachable(true)
  // notifies the store, which re-runs evaluate() and stops this timer.
  const online = await probeReachability()
    .then((r) => r.online === true)
    .catch((e) => {
      detach(debugLog(`offlineRecovery: probe failed (staying offline): ${e}`));
      return false;
    });
  if (online) reportServerReachable(true);
}

function evaluate(): void {
  const shouldRun = mountedPages > 0 && getRommConnectionState() === "offline";
  if (shouldRun && timer === null) {
    timer = setInterval(() => detach(probeOnce()), OFFLINE_RECOVERY_INTERVAL_MS);
  } else if (!shouldRun && timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

/**
 * Register a mounted game page as a recovery-probe owner. Starts the shared
 * timer when the state is (or becomes) offline; returns a cleanup that
 * deregisters this owner and stops the timer once the last page unmounts.
 */
export function registerOfflineRecovery(): () => void {
  mountedPages += 1;
  if (unsubscribe === null) unsubscribe = onRommConnectionChange(evaluate);
  evaluate();
  return () => {
    mountedPages -= 1;
    if (mountedPages <= 0) {
      mountedPages = 0;
      if (unsubscribe) {
        unsubscribe();
        unsubscribe = null;
      }
    }
    evaluate();
  };
}
