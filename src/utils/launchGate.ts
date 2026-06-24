/**
 * Shared pre-launch gate (ADR-0015). One funnel both the Play button and the
 * global launch watcher run before a RomM ROM is allowed to start.
 *
 * `runLaunchGate` SEQUENCES the gate steps and returns a {@link GateVerdict} —
 * it shows NO modals or toasts itself. Every side-effecting operation is
 * injected as a callback (see {@link LaunchGateOps}), so the gate is a pure
 * decision tree that callers act on: each caller maps the verdict onto its own
 * UI (the Play button drives in-place button states; the watcher drives
 * imperative modals).
 *
 * The gate also owns the cross-cutting skip-set (`markLaunchSkipped` /
 * `consumeLaunchSkip`): a one-shot handshake by which a gated launch tells the
 * global watcher "I already gated this appId — don't re-gate it".
 */

import type { SyncConflict } from "../types";
import { logError } from "../api/backend";

/**
 * Outcome of the injected pre-launch sync, shaped after the
 * `pre_launch_sync` callable result the caller already consumes
 * (`{ success, message, conflicts? }`). The gate maps it onto the verdict:
 *   - `conflicts` non-empty            -> `{ decision: "conflict", conflicts }`
 *   - `success === false` (no conflict)-> `{ decision: "sync_failed", message }`
 *   - otherwise                        -> `{ decision: "allow" }`
 */
export interface PreLaunchSyncOutcome {
  success: boolean;
  message: string;
  conflicts?: SyncConflict[];
}

/**
 * The gate's decision. Callers act on it; the gate never renders.
 *
 *   - `allow`            — every gate passed (or sync produced no blocker, or an
 *                          internal error was swallowed). Launch may proceed.
 *   - `block`            — a hard precondition failed and the user has not yet
 *                          been shown UI for it. `reason` selects the caller's
 *                          message (`not_installed`, `migration_pending`).
 *   - `abort`            — the user was shown UI (tracking-setup or core-change)
 *                          and chose not to proceed. The caller bails silently,
 *                          with no further message — the user already decided.
 *   - `conflict`         — pre-launch sync surfaced save conflicts; the caller
 *                          resolves them (e.g. SyncConflictModal).
 *   - `offline_drift`    — server unreachable AND the local save has drifted
 *                          since the last sync; the caller asks whether to play
 *                          anyway (OfflineDriftModal).
 *   - `sync_failed`      — pre-launch sync ran online but failed; `message`
 *                          carries the backend reason for the caller's confirm.
 */
export type GateVerdict =
  | { decision: "allow" }
  | { decision: "block"; reason: "not_installed" | "migration_pending" }
  | { decision: "abort" }
  | { decision: "conflict"; conflicts: SyncConflict[] }
  | { decision: "offline_drift" }
  | { decision: "sync_failed"; message: string };

/**
 * Injected operations for {@link runLaunchGate}. Every side effect the gate
 * needs is a callback so the gate body itself touches no DOM, network, or
 * module state — which makes it fully unit-testable with stubs.
 */
export interface LaunchGateOps {
  /**
   * Synchronous in-memory check: is a RetroDECK migration pending? When true
   * the gate blocks immediately (`block`/`migration_pending`) — launching with
   * a pending migration risks silent save-data loss.
   */
  migrationPending: () => boolean;

  /**
   * Ensure save-slot tracking is configured for this ROM. Returns `"proceed"`
   * to continue, or `"abort"` when the user was shown setup UI and declined
   * (the gate then returns `{ decision: "abort" }`).
   */
  ensureTrackingConfigured: () => Promise<"proceed" | "abort">;

  /**
   * Surface the emulator core-change confirm if the core changed since the last
   * launch. Returns `true` to proceed, `false` when the user cancelled (the
   * gate then returns `{ decision: "abort" }`).
   */
  checkCoreChange: () => Promise<boolean>;

  /**
   * Fresh reachability probe (wraps `probe_reachability`). `true` routes to the
   * online pre-launch sync; `false` routes to the offline drift check.
   */
  checkReachability: () => Promise<boolean>;

  /**
   * Online branch: run pre-launch save sync. The gate maps its outcome onto
   * `conflict` / `sync_failed` / `allow` (see {@link PreLaunchSyncOutcome}).
   */
  preLaunchSync: () => Promise<PreLaunchSyncOutcome>;

  /**
   * Offline branch: has the local save drifted since the last sync (wraps
   * `check_local_drift`)? `true` -> `{ decision: "offline_drift" }`, else
   * `{ decision: "allow" }`.
   */
  checkLocalDrift: () => Promise<boolean>;
}

/**
 * Run the pre-launch gate for `appId` / `romId` and return a verdict. Shows no
 * UI — the caller acts on the verdict.
 *
 * Step order (each step's failure short-circuits the rest):
 *   1. migration pending      -> block / migration_pending
 *   2. ensureTrackingConfigured -> "abort" => abort
 *   3. checkCoreChange        -> cancel => abort
 *   4. checkReachability      -> online vs offline split
 *   5a. online:  preLaunchSync -> conflict | sync_failed | allow
 *   5b. offline: checkLocalDrift -> offline_drift | allow
 *
 * The gate NEVER throws and NEVER blocks the user on an internal error: the
 * whole body is wrapped so any thrown error (from an injected callback or
 * otherwise) resolves to `{ decision: "allow" }`. A bug in the gate must never
 * trap the user's game behind it.
 *
 * `_appId` / `_romId` are accepted so callers pass the identifiers the injected
 * ops were bound for (and to keep the signature stable as ops grow); the gate
 * itself routes purely through the callbacks, so they are intentionally unused.
 */
export async function runLaunchGate(_appId: number, _romId: number, ops: LaunchGateOps): Promise<GateVerdict> {
  try {
    // 1. Pending RetroDECK migration — hard block before any other work.
    if (ops.migrationPending()) {
      return { decision: "block", reason: "migration_pending" };
    }

    // 2. Save-slot tracking setup. "abort" means the user saw setup UI and
    //    declined — bail silently.
    if ((await ops.ensureTrackingConfigured()) === "abort") {
      return { decision: "abort" };
    }

    // 3. Emulator core-change confirm. Cancel => bail silently.
    if (!(await ops.checkCoreChange())) {
      return { decision: "abort" };
    }

    // 4. Fresh reachability probe decides the sync branch.
    const online = await ops.checkReachability();

    if (online) {
      // 5a. Online — run pre-launch sync and map its outcome.
      const sync = await ops.preLaunchSync();
      if (sync.conflicts && sync.conflicts.length > 0) {
        return { decision: "conflict", conflicts: sync.conflicts };
      }
      if (!sync.success) {
        return { decision: "sync_failed", message: sync.message };
      }
      return { decision: "allow" };
    }

    // 5b. Offline — block only when the local save has drifted; otherwise allow.
    if (await ops.checkLocalDrift()) {
      return { decision: "offline_drift" };
    }
    return { decision: "allow" };
  } catch (e) {
    // Never trap the user's game behind a gate bug — fail open to "allow". The
    // log leaves a breadcrumb so a gate bug that should have blocked isn't
    // swallowed with zero trace. After the watcher's preLaunchSync op handles
    // its own throws, this catch is only reached on a truly-unexpected error.
    logError(`runLaunchGate threw (failing open to allow): ${e}`);
    return { decision: "allow" };
  }
}

// ---------------------------------------------------------------------------
// Skip-set — shared one-shot handshake between a gated launch and the watcher.
//
// When a caller (Play button or watcher) has already run the gate and is about
// to start the game itself, it marks the appId here. The global watcher checks
// (and consumes) the mark at its entry so it does not re-gate a launch the
// caller already gated. The check is one-shot: `consumeLaunchSkip` deletes the
// mark as it reads it, so a later genuine launch of the same appId is gated
// normally.
// ---------------------------------------------------------------------------

const _skip = new Set<number>();

/** Mark `appId` as already-gated so the next watcher pass skips it once. */
export function markLaunchSkipped(appId: number): void {
  _skip.add(appId);
}

/**
 * One-shot check-and-delete: returns `true` (and clears the mark) if `appId`
 * was marked skipped, else `false`. The next launch of the same appId is gated
 * normally.
 */
export function consumeLaunchSkip(appId: number): boolean {
  return _skip.delete(appId);
}
