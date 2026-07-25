import { logError, releasePruneConflictLease, renewPruneConflictLease } from "../api/backend";
import { withTimeout } from "./withTimeout";

const RELEASE_TIMEOUT_MS = 5000;
const RENEW_INTERVAL_MS = 60_000;

/** Bounded best-effort release for frontend-owned prune conflict leases. */
export async function releasePruneLease(token: string, context: string): Promise<void> {
  try {
    await withTimeout(releasePruneConflictLease(token), RELEASE_TIMEOUT_MS);
  } catch (e) {
    logError(`${context}: failed to release prune lease: ${e}`);
  }
}

/** Keep a frontend continuation demonstrably live until its bounded release. */
export function maintainPruneLease(token: string, context: string): () => Promise<void> {
  let stopped = false;
  const renew = async (): Promise<void> => {
    try {
      const result = await withTimeout(renewPruneConflictLease(token), RELEASE_TIMEOUT_MS);
      if (!result.success) logError(`${context}: prune lease renewal was refused: ${result.message}`);
    } catch (e) {
      logError(`${context}: failed to renew prune lease: ${e}`);
    }
  };
  const interval = setInterval(() => {
    if (!stopped) void renew();
  }, RENEW_INTERVAL_MS);
  return async () => {
    if (stopped) return;
    stopped = true;
    clearInterval(interval);
    await releasePruneLease(token, context);
  };
}

export async function withPruneLease<T>(
  token: string | null | undefined,
  context: string,
  operation: () => Promise<T>,
): Promise<T> {
  if (!token) return operation();
  const release = maintainPruneLease(token, context);
  try {
    return await operation();
  } finally {
    await release();
  }
}
