import { getRomRelaunchOptions, logError } from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";

// Apply in bounded-concurrency batches (mirrors getExistingRomMShortcuts) so a
// reconcile touching many ROMs doesn't serialize worst-case per-shortcut
// confirm-poll timeouts.
const CONCURRENCY = 10;

/** Bound on the single-ROM relaunch-options fetch before a launch. The Decky
 *  callable bridge can hang indefinitely on a wedged backend, so the read is
 *  raced against this timeout — a hang falls through to the launch instead of
 *  trapping the caller (mirrors index.tsx's withTimeout). */
const RECONFIRM_FETCH_TIMEOUT_MS = 3000;

/**
 * Heal any mid-session `launch_options` drift on one shortcut right before a
 * launch: pull the ROM's resolved command (`get_rom_relaunch_options`) and
 * confirm-set it onto the shortcut's appId. Best-effort — a hang (bounded by a
 * 3s race), a `null` item, or a thrown error is logged via `logError` with the
 * `context` prefix and never blocks the caller; the launch proceeds regardless.
 * Shared by the Play-button funnel and the direct-launch watcher relaunch path.
 */
export async function reconfirmLaunchOptions(romId: number, appId: number, context: string): Promise<void> {
  try {
    const item = await Promise.race([
      getRomRelaunchOptions(romId),
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("get_rom_relaunch_options timed out")), RECONFIRM_FETCH_TIMEOUT_MS),
      ),
    ]);
    if (item) await setLaunchOptionsConfirmed(appId, item.launch_options);
  } catch (e) {
    logError(`${context}: launch_options re-confirm failed (launching anyway): ${e}`);
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
): Promise<void> {
  if (!Array.isArray(items) || items.length === 0) return;
  for (let i = 0; i < items.length; i += CONCURRENCY) {
    const batch = items.slice(i, i + CONCURRENCY);
    await Promise.all(
      batch.map(async (item) => {
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
