/**
 * Module-level per-run sync delta — the TRUE created/removed counts for one
 * sync run, so the post-sync toast reports what actually changed rather than
 * the total processed set.
 *
 * The library applies whole platforms (not per-ROM deltas), so an "applied
 * count" is not a real delta. The only exact, meaningful deltas are the
 * shortcuts the frontend actually created (`addShortcut` calls) and removed
 * (`sync_stale` app_ids). A ROM can appear in multiple units (its platform unit
 * plus a collection unit like Favorites), so the counts are Sets of appIds —
 * the same shortcut created/removed across units is counted once.
 *
 * Updated by:
 *   - syncManager create path (recordSyncCreated on a fresh addShortcut appId)
 *   - sync_stale listener in index.tsx (recordSyncRemoved per removed app_id)
 *   - sync_plan listener in index.tsx (resetSyncDelta at run start)
 *
 * Read by:
 *   - onSyncComplete in index.tsx (getSyncDelta for the terminal toast;
 *     getAckedCreatedAppIds for the cover-mtime sweep + heal poll)
 */

const created = new Set<number>();
const removed = new Set<number>();
// Subset of `created` whose chunk's ack the backend actually committed (grid
// files written). onSyncComplete cover-stamps only these — a cancelled/interrupted
// run's final in-flight chunk creates shortcuts frontend-side (in `created`) whose
// ack was skipped, so their covers were never written; stamping them would point
// the tile at a 404. Recorded by the syncManager per-chunk stamp site, post-ack.
const ackedCreated = new Set<number>();

/** Clear all sets at the start of a run (sync_plan fires once per run). */
export function resetSyncDelta(): void {
  created.clear();
  removed.clear();
  ackedCreated.clear();
}

/** Record a newly created shortcut's appId (real addShortcut call). */
export function recordSyncCreated(appId: number): void {
  created.add(appId);
}

/** Record a removed shortcut's appId (a sync_stale entry). */
export function recordSyncRemoved(appId: number): void {
  removed.add(appId);
}

/**
 * Record the appIds a chunk newly created AND whose ack the backend committed
 * (grid files written) — the safe-to-cover-stamp subset. Called by the syncManager
 * per-chunk stamp site after ``reportUnitResults`` resolves.
 */
export function recordSyncAcked(appIds: number[]): void {
  for (const appId of appIds) ackedCreated.add(appId);
}

/** The deduplicated created/removed counts for the current run. */
export function getSyncDelta(): { added: number; removed: number } {
  return { added: created.size, removed: removed.size };
}

/** The deduplicated appIds created this run (real addShortcut calls). */
export function getCreatedAppIds(): number[] {
  return [...created];
}

/**
 * The deduplicated appIds created this run whose chunk was ACKED (committed +
 * grid files written). The safe set to cover-stamp — excludes a cancelled run's
 * uncommitted in-flight chunk (whose covers don't exist server-side yet).
 */
export function getAckedCreatedAppIds(): number[] {
  return [...ackedCreated];
}
