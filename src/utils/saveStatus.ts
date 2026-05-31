import type { SyncConflict } from "../types";

/**
 * Single source of truth for "does this save status require user action for a conflict?"
 *
 * Uses the `conflicts` array (canonical) rather than per-file `f.status === "conflict"`
 * checks. Backend emits a single conflict type (`sync_conflict`) for true
 * two-sided divergence between local + server.
 *
 * Accepts any object with an optional `conflicts` array, so it works with `SaveStatus`,
 * `CachedGameDetail.save_status`, and `save_status_updated` emit-event payloads.
 */
export function hasAnySaveConflict(status: { conflicts?: SyncConflict[] | null } | null | undefined): boolean {
  return (status?.conflicts?.length ?? 0) > 0;
}
