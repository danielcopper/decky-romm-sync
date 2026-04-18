import type { PendingConflict } from "../types";

/**
 * Single source of truth for "does this save status require user action for a conflict?"
 *
 * Uses the `conflicts` array (canonical) rather than per-file `f.status === "conflict"`
 * checks, which only catch three-way (file-level) conflicts and miss `newer_in_slot`
 * (slot-level) conflicts that RomM surfaces when another device posts a parallel save.
 *
 * Accepts any object with an optional `conflicts` array, so it works with `SaveStatus`,
 * `CachedGameDetail.save_status`, and `save_status_updated` emit-event payloads.
 */
export function hasAnySaveConflict(
  status: { conflicts?: PendingConflict[] | null } | null | undefined,
): boolean {
  return (status?.conflicts?.length ?? 0) > 0;
}
