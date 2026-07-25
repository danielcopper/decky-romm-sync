import { removeShortcut } from "./steamShortcuts";
import { pacedForEach } from "./pacedOps";

// Bulk removals run through the shared paced loop in chunked mode: 25 removals
// back-to-back, then a 50ms breather so the CEF renderer never blocks and Steam's
// in-memory shortcut store can't be corrupted by removal churn (#977). Unlike the
// add path (strict 50ms/item), a removal is a single cheap call, so chunked
// yielding keeps overhead at ~seconds rather than minutes on a 5000-game library.
const REMOVAL_CHUNK_SIZE = 25;
const REMOVAL_CHUNK_DELAY_MS = 50;

/**
 * Remove many Steam shortcuts, chunk-paced. Each ``removeShortcut`` is awaited in
 * sequence (no more fire-and-forget stacking of thousands of pending removals);
 * per-item errors are swallowed by ``removeShortcut``, so one bad appId never
 * aborts the batch. Resolves once every removal has run — callers await this
 * before their post-removal steps (result reporting, collection clear, re-count).
 * Shared by every bulk-removal path: the DangerZone actions and the sync-run
 * stale-shortcut cleanup (``sync_stale``).
 *
 * *onProgress* (optional) is called after each removal with ``(removed, total)``
 * so a caller can drive a live progress counter; omit it (as ``sync_stale`` does)
 * for no counter and unchanged behavior.
 */
export async function removeShortcutsPaced(
  appIds: number[],
  onProgress?: (removed: number, total: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  const total = appIds.length;
  await pacedForEach(appIds, (appId) => removeShortcut(appId), {
    chunkSize: REMOVAL_CHUNK_SIZE,
    delayMs: REMOVAL_CHUNK_DELAY_MS,
    // Only thread the hook when a caller wants one (exactOptionalPropertyTypes
    // forbids passing an explicit ``undefined`` for an optional property).
    ...(onProgress ? { onProgress: (completed: number) => onProgress(completed, total) } : {}),
    ...(signal ? { isCancelled: () => signal.aborted } : {}),
  });
}
