/**
 * Pure helpers for the RomM play-section row: label resolution, BIOS-payload
 * shaping, and the timeout-promise primitive used by connection probing.
 *
 * Anything that takes inputs and returns outputs without touching component
 * state belongs here. Anything that talks to the backend belongs in
 * sectionRefresh.ts. Anything stateful belongs in the component itself.
 */

import type { CachedGameDetail } from "../api/backend";
import type { CoreInfo, EmulatorOption, SaveStatus, SaveSyncDisplay } from "../types";
import { hasAnySaveConflict } from "./saveStatus";
import { formatTimeAgo } from "./formatters";

/** BIOS-only fields for the play-section row. Core data (active core, available
 *  cores) is sourced independently via `extractCoreInfo` from the dedicated
 *  `get_platform_core_info` path — it no longer rides the BIOS payload (#923). */
export interface BiosInfoFields {
  biosNeeded: boolean;
  biosStatus: "ok" | "partial" | "missing" | "unmanaged" | null;
  biosLabel: string;
}

/** Core-selection fields for the play-section row, derived from the dedicated
 *  `get_platform_core_info` path (#923), decoupled from BIOS status. */
export interface CoreInfoFields {
  activeCoreLabel: string | null;
  activeCoreIsDefault: boolean;
  emulators: EmulatorOption[];
  emulatorDataAvailable: boolean;
  platformCoreLabel: string | null;
  hasGameOverride: boolean;
}

export interface SaveSyncResolution {
  status: "synced" | "pending" | "conflict" | "none";
  label: string;
}

/** Resolve the human-readable save-sync label from the backend's typed display
 *  payload. Backend ships a static `label` for every case except
 *  `synced + has-recent-check`, where it leaves `label` null and passes
 *  `last_sync_check_at` through for time-ago formatting at render time. */
export function resolveSaveSyncLabel(display: SaveSyncDisplay): string {
  if (display.label !== null) return display.label;
  if (display.last_sync_check_at) {
    return formatTimeAgo(display.last_sync_check_at) ?? "Not synced";
  }
  return "Not synced";
}

/** Map a SaveSyncDisplay (typed display payload) to a status+label pair.
 *  Defensive fallback handles a SaveStatus missing the pre-computed display —
 *  should not occur in current callers, kept conservative. */
export function applySaveSyncDisplay(
  display: SaveSyncDisplay | undefined,
  saveStatus: SaveStatus | null,
): SaveSyncResolution {
  if (display) {
    return { status: display.status, label: resolveSaveSyncLabel(display) };
  }
  if (hasAnySaveConflict(saveStatus)) return { status: "conflict", label: "Conflict" };
  return { status: "none", label: "No saves" };
}

/** Project a whole BIOS answer — the backend's `bios_status` plus the level and
 *  label it pre-computed for it — into the BIOS-only fields the play-section row
 *  needs. `level` and `label` are never re-derived here. Only the PRESENCE of
 *  `status` is read: its absence is the backend answering "this core needs no
 *  BIOS", which clears all three fields, so a requirement can be taken back off
 *  the page (#1690). A read that FAILED is not that answer and must never be
 *  funnelled in as one — the caller drops it and leaves the shown level
 *  standing. Core data is sourced separately via `extractCoreInfo` (the BIOS
 *  payload no longer carries it, #923). */
export function extractBiosInfo(
  status: CachedGameDetail["bios_status"],
  level: "ok" | "partial" | "missing" | "unmanaged" | null,
  label: string | null,
): BiosInfoFields {
  if (!status) return { biosNeeded: false, biosStatus: null, biosLabel: "" };
  return {
    biosNeeded: true,
    biosStatus: level,
    biosLabel: label ?? "",
  };
}

/** Project a CoreInfo response (from the dedicated `get_platform_core_info`
 *  path, #923) into the core-selection fields the play-section row needs. The
 *  active core is "default" when it equals the default emulator or no override
 *  is set. */
export function extractCoreInfo(coreInfo: CoreInfo): CoreInfoFields {
  const activeCoreLabel = coreInfo.active_core_label ?? null;
  const emulators = coreInfo.emulators;
  const defaultEmulator = emulators.find((e) => e.is_default);
  const activeCoreIsDefault = !activeCoreLabel || activeCoreLabel === defaultEmulator?.label;
  return {
    activeCoreLabel,
    activeCoreIsDefault,
    emulators,
    emulatorDataAvailable: coreInfo.emulator_data_available,
    platformCoreLabel: coreInfo.platform_core_label ?? null,
    hasGameOverride: coreInfo.has_game_override,
  };
}

/** Promise that rejects after `ms` milliseconds. Pair with `Promise.race` to
 *  enforce a timeout on an otherwise unbounded async call. */
export function timeoutMs(ms: number): Promise<never> {
  return new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), ms));
}
