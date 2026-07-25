import { logError, releasePruneConflictLease, renewPruneConflictLease } from "../api/backend";
import { withTimeout } from "./withTimeout";

const RELEASE_TIMEOUT_MS = 5000;
const RENEW_INTERVAL_MS = 60_000;
const CONTINUATION_TIMEOUT_MS = 5 * 60_000;

interface ActiveLease {
  abortController: AbortController | undefined;
  context: string;
  owner: string;
  interval: ReturnType<typeof setInterval>;
}

const activeLeases = new Map<string, ActiveLease>();

class UnsettledContinuation {
  constructor(readonly error: unknown) {}
}

/** Bounded best-effort release for frontend-owned prune conflict leases. */
export async function releasePruneLease(token: string, context: string): Promise<void> {
  try {
    await withTimeout(releasePruneConflictLease(token), RELEASE_TIMEOUT_MS);
  } catch (e) {
    logError(`${context}: failed to release prune lease: ${e}`);
  }
}

async function finishLease(token: string, releaseBackend: boolean): Promise<void> {
  const active = activeLeases.get(token);
  if (!active) return;
  activeLeases.delete(token);
  clearInterval(active.interval);
  if (releaseBackend) await releasePruneLease(token, active.context);
}

/** Keep a frontend continuation demonstrably live until its bounded release. */
export function maintainPruneLease(
  token: string,
  context: string,
  owner = context,
  abortController?: AbortController,
): () => Promise<void> {
  const renew = async (): Promise<void> => {
    if (!activeLeases.has(token)) return;
    try {
      const result = await withTimeout(renewPruneConflictLease(token), RELEASE_TIMEOUT_MS);
      if (!result.success) {
        logError(`${context}: prune lease renewal was refused: ${result.message}`);
        await finishLease(token, false);
      }
    } catch (e) {
      logError(`${context}: failed to renew prune lease: ${e}`);
    }
  };
  const interval = setInterval(() => void renew(), RENEW_INTERVAL_MS);
  activeLeases.set(token, { abortController, context, owner, interval });
  return () => finishLease(token, true);
}

export async function releasePruneLeasesByOwner(owner: string): Promise<void> {
  const owned = [...activeLeases].filter(([, lease]) => lease.owner === owner);
  for (const [, lease] of owned) lease.abortController?.abort();
  const tokens = owned.map(([token]) => token);
  await Promise.all(tokens.map((token) => finishLease(token, true)));
}

export async function releaseAllPruneLeases(): Promise<void> {
  for (const lease of activeLeases.values()) lease.abortController?.abort();
  await Promise.all([...activeLeases].map(([token]) => finishLease(token, true)));
}

async function boundedContinuation<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  abortController: AbortController,
): Promise<{ result: T; settled: true }> {
  const promise = operation(abortController.signal);
  const settled = promise.then(
    (result) => ({ kind: "result" as const, result }),
    (error: unknown) => ({ kind: "error" as const, error }),
  );
  let timer!: ReturnType<typeof setTimeout>;
  const timeout = new Promise<{ kind: "timeout"; error: Error }>((resolve) => {
    timer = setTimeout(
      () => resolve({ kind: "timeout", error: new Error(`callable timed out after ${CONTINUATION_TIMEOUT_MS}ms`) }),
      CONTINUATION_TIMEOUT_MS,
    );
  });
  try {
    const outcome = await Promise.race([settled, timeout]);
    if (outcome.kind === "timeout") throw new UnsettledContinuation(outcome.error);
    if (outcome.kind === "error") throw outcome.error;
    return { result: outcome.result, settled: true };
  } finally {
    clearTimeout(timer);
  }
}

export async function withPruneLeases<T>(
  tokens: Array<string | null | undefined>,
  context: string,
  operation: (signal: AbortSignal) => Promise<T>,
  owner = context,
): Promise<T> {
  const uniqueTokens = [...new Set(tokens.filter((token): token is string => !!token))];
  const abortController = new AbortController();
  const releases = uniqueTokens.map((token) => ({
    token,
    release: maintainPruneLease(token, context, owner, abortController),
  }));
  try {
    return (await boundedContinuation(operation, abortController)).result;
  } catch (caught) {
    if (caught instanceof UnsettledContinuation) {
      abortController.abort();
      await Promise.all(releases.map(({ token }) => finishLease(token, false)));
      throw caught.error;
    }
    throw caught;
  } finally {
    await Promise.all(releases.map(({ release }) => release()));
  }
}

export function withPruneLease<T>(
  token: string | null | undefined,
  context: string,
  operation: (signal: AbortSignal) => Promise<T>,
  owner = context,
): Promise<T> {
  return withPruneLeases([token], context, operation, owner);
}
