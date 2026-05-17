/**
 * Pure helpers for the RomM play-section row: label resolution, BIOS-payload
 * shaping, and the timeout-promise primitive used by connection probing.
 *
 * Anything that takes inputs and returns outputs without touching component
 * state belongs here. Anything that talks to the backend belongs in
 * sectionRefresh.ts. Anything stateful belongs in the component itself.
 */

import type { AvailableCore, BiosStatus, SaveStatus, SaveSyncDisplay } from "../types";
import { hasAnySaveConflict } from "./saveStatus";
import { formatTimeAgo } from "./formatters";

export interface BiosInfoFields {
  biosNeeded: boolean;
  biosStatus: "ok" | "partial" | "missing" | null;
  biosLabel: string;
  activeCoreLabel: string | null;
  activeCoreIsDefault: boolean;
  availableCores: AvailableCore[];
}

export interface SaveSyncResolution {
  status: "synced" | "conflict" | "none";
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

/** Project a BiosStatus + pre-computed level/label into the narrow set of
 *  fields the play-section row needs. `bios_level` and `bios_label` are
 *  computed by the backend so the frontend never re-derives them. */
export function extractBiosInfo(
  b: BiosStatus,
  level: "ok" | "partial" | "missing" | null,
  label: string | null,
): BiosInfoFields {
  const activeCoreLabel = b.active_core_label ?? null;
  const availableCores = b.available_cores ?? [];
  const defaultCore = availableCores.find((c) => c.is_default);
  const activeCoreIsDefault = !activeCoreLabel || activeCoreLabel === defaultCore?.label;
  return {
    biosNeeded: true,
    biosStatus: level,
    biosLabel: label ?? "",
    activeCoreLabel,
    activeCoreIsDefault,
    availableCores,
  };
}

/** Promise that rejects after `ms` milliseconds. Pair with `Promise.race` to
 *  enforce a timeout on an otherwise unbounded async call. */
export function timeoutMs(ms: number): Promise<never> {
  return new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), ms));
}
