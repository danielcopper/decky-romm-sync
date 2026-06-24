import { logError } from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";

// Apply in bounded-concurrency batches (mirrors getExistingRomMShortcuts) so a
// reconcile touching many ROMs doesn't serialize worst-case per-shortcut
// confirm-poll timeouts.
const CONCURRENCY = 10;

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
