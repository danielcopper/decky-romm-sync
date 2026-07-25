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
  settlement: Promise<void> | undefined;
}

const activeLeases = new Map<string, ActiveLease>();
const ownerGenerations = new Map<string, { generation: number; mounted: boolean }>();
let pluginGeneration = 0;
let pluginMounted = true;

export interface PruneLeaseAdmission {
  pluginGeneration: number;
  owner?: string;
  ownerGeneration?: number;
}

class UnsettledContinuation {
  constructor(readonly error: unknown) {}
}

export class PruneLeaseAdmissionCancelled extends Error {}

export function isPruneLeaseCancelled(signal: AbortSignal | undefined): boolean {
  return signal?.aborted ?? false;
}

export function mountPruneLeasePlugin(): void {
  pluginGeneration++;
  pluginMounted = true;
}

export function mountPruneLeaseOwner(owner: string): void {
  const current = ownerGenerations.get(owner);
  if (current?.mounted) return;
  ownerGenerations.set(owner, { generation: (current?.generation ?? 0) + 1, mounted: true });
}

export function capturePruneLeaseAdmission(owner?: string): PruneLeaseAdmission {
  const current = owner === undefined ? undefined : ownerGenerations.get(owner);
  return {
    pluginGeneration,
    ...(owner === undefined ? {} : { owner, ownerGeneration: current?.generation ?? 0 }),
  };
}

export function isPruneLeaseAdmissionCurrent(admission: PruneLeaseAdmission): boolean {
  if (!pluginMounted || admission.pluginGeneration !== pluginGeneration) return false;
  if (admission.owner === undefined) return true;
  const current = ownerGenerations.get(admission.owner);
  return current?.mounted === true && current.generation === admission.ownerGeneration;
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

function retireLeaseAfterSettlement(token: string): Promise<void> {
  const active = activeLeases.get(token);
  if (!active) return Promise.resolve();
  activeLeases.delete(token);
  clearInterval(active.interval);
  active.abortController?.abort();
  const release = () => releasePruneLease(token, active.context);
  return active.settlement ? active.settlement.then(release) : release();
}

/** Keep a frontend continuation demonstrably live until its bounded release. */
export function maintainPruneLease(
  token: string,
  context: string,
  owner = context,
  abortController?: AbortController,
  settlement?: Promise<void>,
): () => Promise<void> {
  const renew = async (): Promise<void> => {
    if (!activeLeases.has(token)) return;
    try {
      const result = await withTimeout(renewPruneConflictLease(token), RELEASE_TIMEOUT_MS);
      if (!result.success) {
        logError(`${context}: prune lease renewal was refused: ${result.message}`);
        activeLeases.get(token)?.abortController?.abort();
        await finishLease(token, false);
      }
    } catch (e) {
      logError(`${context}: failed to renew prune lease: ${e}`);
    }
  };
  const interval = setInterval(() => void renew(), RENEW_INTERVAL_MS);
  activeLeases.set(token, { abortController, context, owner, interval, settlement });
  return () => finishLease(token, true);
}

export async function releasePruneLeasesByOwner(owner: string): Promise<void> {
  const current = ownerGenerations.get(owner);
  ownerGenerations.set(owner, { generation: (current?.generation ?? 0) + 1, mounted: false });
  const owned = [...activeLeases].filter(([, lease]) => lease.owner === owner);
  for (const [, lease] of owned) lease.abortController?.abort();
  const tokens = owned.map(([token]) => token);
  await Promise.all(tokens.map(retireLeaseAfterSettlement));
}

export async function releaseAllPruneLeases(): Promise<void> {
  pluginGeneration++;
  pluginMounted = false;
  for (const [owner, current] of ownerGenerations) {
    ownerGenerations.set(owner, { generation: current.generation + 1, mounted: false });
  }
  for (const lease of activeLeases.values()) lease.abortController?.abort();
  await Promise.all([...activeLeases].map(([token]) => retireLeaseAfterSettlement(token)));
}

async function boundedContinuation<T>(promise: Promise<T>): Promise<{ result: T; settled: true }> {
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
  admission: PruneLeaseAdmission = capturePruneLeaseAdmission(),
): Promise<T> {
  const uniqueTokens = [...new Set(tokens.filter((token): token is string => !!token))];
  if (!isPruneLeaseAdmissionCurrent(admission)) {
    await Promise.all(uniqueTokens.map((token) => releasePruneLease(token, context)));
    throw new PruneLeaseAdmissionCancelled(`${context}: continuation was cancelled before lease registration`);
  }
  const abortController = new AbortController();
  const operationPromise = Promise.resolve().then(() => operation(abortController.signal));
  const settlement = operationPromise.then(
    () => undefined,
    () => undefined,
  );
  const releases = uniqueTokens.map((token) => ({
    token,
    release: maintainPruneLease(token, context, owner, abortController, settlement),
  }));
  let timedOut = false;
  try {
    return (await boundedContinuation(operationPromise)).result;
  } catch (caught) {
    if (caught instanceof UnsettledContinuation) {
      timedOut = true;
      abortController.abort();
      for (const { token } of releases) void retireLeaseAfterSettlement(token);
      throw caught.error;
    }
    throw caught;
  } finally {
    if (!timedOut) await Promise.all(releases.map(({ release }) => release()));
  }
}

export function withPruneLease<T>(
  token: string | null | undefined,
  context: string,
  operation: (signal: AbortSignal) => Promise<T>,
  owner = context,
  admission?: PruneLeaseAdmission,
): Promise<T> {
  return withPruneLeases([token], context, operation, owner, admission);
}
