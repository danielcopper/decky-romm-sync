import { getRomRelaunchOptions, logError } from "../api/backend";
import type { RelaunchOptionsResult } from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseAdmissionCurrent,
  isPruneLeaseCancellation,
  releasePruneLease,
  withPruneLease,
  type PruneLeaseAdmission,
} from "./pruneLease";

// Apply in bounded-concurrency batches (mirrors getExistingRomMShortcuts) so a
// reconcile touching many ROMs doesn't serialize worst-case per-shortcut
// confirm-poll timeouts.
const CONCURRENCY = 10;

/** Bound on the single-ROM relaunch-options fetch before a launch. */
const RECONFIRM_FETCH_TIMEOUT_MS = 3000;

export type RelaunchOptionsReconfirmResult =
  { status: "ready" } | { status: "best_effort_failure" } | { status: "timeout" } | { status: "cancelled" };

/**
 * Keep watching a re-confirm that timed out: its lease is still held by the
 * backend, so a late success has to hand it back or it pins the admission gate
 * until its TTL.
 */
function releaseLateReconfirmLease(
  fetchOutcome: Promise<{ kind: "result"; item: RelaunchOptionsResult } | { kind: "error"; error: unknown }>,
  context: string,
): void {
  void fetchOutcome
    .then(async (late) => {
      if (late.kind === "result" && late.item?.success) {
        await releasePruneLease(late.item.prune_lease_token, `${context}: timed-out launch_options re-confirm`);
      }
    })
    .catch((error: unknown) => logError(`${context}: late launch_options re-confirm cleanup failed: ${error}`));
}

type FetchedRelaunchOptions =
  | { kind: "options"; options: Extract<RelaunchOptionsResult, { success: true }> }
  | { kind: "verdict"; result: RelaunchOptionsReconfirmResult };

/**
 * Pull the ROM's resolved launch command, bounded by a timeout. Anything that is
 * not a usable command is already the whole answer: a timeout stops the launch,
 * a fetch or backend failure lets it proceed best-effort, and a lifecycle
 * cancellation outranks both.
 */
async function fetchRelaunchOptions(
  romId: number,
  context: string,
  admission: PruneLeaseAdmission,
): Promise<FetchedRelaunchOptions> {
  const fetchOutcome = getRomRelaunchOptions(romId).then(
    (item) => ({ kind: "result" as const, item }),
    (error: unknown) => ({ kind: "error" as const, error }),
  );
  let timer!: ReturnType<typeof setTimeout>;
  const timeout = new Promise<{ kind: "timeout" }>((resolve) => {
    timer = setTimeout(() => resolve({ kind: "timeout" }), RECONFIRM_FETCH_TIMEOUT_MS);
  });
  const outcome = await Promise.race([fetchOutcome, timeout]);
  if (outcome.kind === "timeout") {
    logError(`${context}: launch_options re-confirm timed out (launch cancelled)`);
    releaseLateReconfirmLease(fetchOutcome, context);
    const status = isPruneLeaseAdmissionCurrent(admission) ? "timeout" : "cancelled";
    return { kind: "verdict", result: { status } };
  }
  clearTimeout(timer);
  if (outcome.kind === "error") {
    if (!isPruneLeaseAdmissionCurrent(admission)) return { kind: "verdict", result: { status: "cancelled" } };
    logError(`${context}: launch_options re-confirm failed (launching anyway): ${outcome.error}`);
    return { kind: "verdict", result: { status: "best_effort_failure" } };
  }
  const item = outcome.item;
  if (!item) {
    const status = isPruneLeaseAdmissionCurrent(admission) ? "ready" : "cancelled";
    return { kind: "verdict", result: { status } };
  }
  if (!item.success) {
    if (!isPruneLeaseAdmissionCurrent(admission)) return { kind: "verdict", result: { status: "cancelled" } };
    logError(`${context}: launch_options re-confirm failed (launching anyway): ${item.message}`);
    return { kind: "verdict", result: { status: "best_effort_failure" } };
  }
  return { kind: "options", options: item };
}

/**
 * Heal any mid-session `launch_options` drift on one shortcut right before a
 * launch: pull the ROM's resolved command (`get_rom_relaunch_options`) and
 * confirm-set it onto the shortcut's appId. Ordinary fetch/write failures remain
 * best-effort. Lifecycle cancellation and timeout are explicit launch-stopping
 * results; a timed-out callable stays observed so a late lease is released.
 */
export async function reconfirmLaunchOptions(
  romId: number,
  appId: number,
  context: string,
  admission: PruneLeaseAdmission = capturePruneLeaseAdmission(),
): Promise<RelaunchOptionsReconfirmResult> {
  if (!isPruneLeaseAdmissionCurrent(admission)) return { status: "cancelled" };
  const fetched = await fetchRelaunchOptions(romId, context, admission);
  if (fetched.kind === "verdict") return fetched.result;
  const item = fetched.options;
  try {
    await withPruneLease(
      item.prune_lease_token,
      context,
      async (signal) => {
        if (signal.aborted) return;
        await setLaunchOptionsConfirmed(appId, item.launch_options);
      },
      context,
      admission,
    );
    return { status: isPruneLeaseAdmissionCurrent(admission) ? "ready" : "cancelled" };
  } catch (e) {
    if (isPruneLeaseCancellation(e, admission)) {
      return { status: "cancelled" };
    }
    logError(`${context}: launch_options re-confirm failed (launching anyway): ${e}`);
    return { status: "best_effort_failure" };
  }
}

/**
 * Confirm-set the launch command on every shortcut in `items`, batching the
 * per-item confirm-polls so a large set doesn't serialize their timeouts.
 * No-ops on a non-array or empty list. A failed confirm (false return) or a
 * thrown error is logged via `logError` with the `context` prefix and the
 * offending appId; the remaining items are still processed.
 */
export async function batchConfirmLaunchOptions(
  items: { app_id: number; launch_options: string }[],
  context: string,
  signal?: AbortSignal,
): Promise<void> {
  if (!Array.isArray(items) || items.length === 0) return;
  for (let i = 0; i < items.length; i += CONCURRENCY) {
    const batch = items.slice(i, i + CONCURRENCY);
    await Promise.all(
      batch.map(async (item) => {
        if (signal?.aborted) return;
        try {
          const ok = await setLaunchOptionsConfirmed(item.app_id, item.launch_options);
          if (!ok) {
            logError(`${context}: failed to confirm launch options for appId ${item.app_id}`);
          }
        } catch (e) {
          logError(`${context}: failed to set launch options for appId ${item.app_id}: ${e}`);
        }
      }),
    );
  }
}
