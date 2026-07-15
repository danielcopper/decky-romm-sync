/**
 * Module-level per-run sync delta — the TRUE created/removed counts for one
 * sync run, so the post-sync toast reports what actually changed rather than
 * the total processed set.
 *
 * The library applies whole platforms (not per-ROM deltas), so an "applied
 * count" is not a real delta. The only exact, meaningful deltas are the
 * shortcuts the frontend brought under management this run — a fresh
 * `addShortcut` OR an adopted orphan (#1366) — and the shortcuts it removed
 * (`sync_stale` app_ids). Both a create and an adoption count as "added": each
 * brings a game under management for the first time. An adoption reuses an
 * existing Steam tile rather than minting one, but that is a renderer-cost
 * detail; this store feeds only the terminal toast (a library-management
 * summary), so it reports management, not AddShortcut calls. The counts are Sets
 * of appIds so an appId defensively recorded more than once in a run collapses
 * to one — belt-and-suspenders, since the backend emits each rom_id in exactly
 * one unit's shortcuts per run (not load-bearing dedup).
 *
 * Updated by:
 *   - syncManager create path (recordSyncCreated on a fresh addShortcut appId,
 *     or an adopted orphan's reused appId)
 *   - sync_stale listener in index.tsx (recordSyncRemoved per removed app_id)
 *   - sync_plan listener in index.tsx (resetSyncDelta at run start)
 *
 * Read by:
 *   - onSyncComplete in index.tsx (getSyncDelta for the terminal toast)
 */

const created = new Set<number>();
const removed = new Set<number>();

/** Clear all sets at the start of a run (sync_plan fires once per run). */
export function resetSyncDelta(): void {
  created.clear();
  removed.clear();
}

/** Record a newly-managed shortcut's appId (a fresh addShortcut, or an adopted orphan #1366). */
export function recordSyncCreated(appId: number): void {
  created.add(appId);
}

/** Record a removed shortcut's appId (a sync_stale entry). */
export function recordSyncRemoved(appId: number): void {
  removed.add(appId);
}

/** The deduplicated created/removed counts for the current run. */
export function getSyncDelta(): { added: number; removed: number } {
  return { added: created.size, removed: removed.size };
}
