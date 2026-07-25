import { logError, releasePruneConflictLease } from "../api/backend";
import { withTimeout } from "./withTimeout";

const RELEASE_TIMEOUT_MS = 5000;

/** Bounded best-effort release for frontend-owned prune conflict leases. */
export async function releasePruneLease(token: string, context: string): Promise<void> {
  try {
    await withTimeout(releasePruneConflictLease(token), RELEASE_TIMEOUT_MS);
  } catch (e) {
    logError(`${context}: failed to release prune lease: ${e}`);
  }
}
