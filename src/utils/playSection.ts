/**
 * Pure helpers for the RomM play-section row: label resolution, BIOS-payload
 * shaping, and the timeout-promise primitive used by connection probing.
 *
 * Anything that takes inputs and returns outputs without touching component
 * state belongs here. Anything that talks to the backend belongs in
 * sectionRefresh.ts. Anything stateful belongs in the component itself.
 */

import type { BiosAnswer } from "../api/backend";
import type { CoreInfo, EmulatorOption, SaveStatus, SaveSyncDisplay } from "../types";
import { hasAnySaveConflict } from "./saveStatus";
import { formatTimeAgo } from "./formatters";

/** BIOS-only fields for the play-section row. Core data (active core, available
 *  cores) is sourced independently via `extractCoreInfo` from the dedicated
 *  `get_platform_core_info` path — it no longer rides the BIOS payload (#923). */
export interface BiosInfoFields {
  biosNeeded: boolean;
  biosLabel: string;
  /** Whether a file the ACTIVE CORE requires is not on disk — the whole of the
   *  play-row badge's rule, and the reason it is derived here rather than at the
   *  row: it is a local fact (`required_downloaded` counts what the reading
   *  found at each destination), and reassembling it from the numbers at the
   *  call site is how it drifted into keying off the readiness verdict instead.
   *  A required row whose verdict was withheld is out of both sides of the
   *  comparison — it is not on disk and not shown to be missing either.
   *
   *  The four-valued `bios_level` is deliberately NOT projected. The badge has
   *  one appearance, so it needs no colour input, and the BIOS tab reads the
   *  level off its own payload — carrying it here as well would be a second
   *  copy of the verdict for nobody to render. */
  biosRequiredMissing: boolean;
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

/** Project a whole BIOS answer — the backend's `bios_status` plus the label it
 *  pre-computed for it — into the BIOS-only fields the play-section row needs,
 *  or `null` when the payload carries no answer at all. The label is never
 *  re-derived here.
 *
 *  Four payloads. `bios_status` present: the requirement, and whether the active
 *  core is missing one of its files. `bios_status` absent: the backend answering
 *  "this core needs no BIOS", which clears the fields so a requirement can be
 *  taken back off the page (#1690). `bios_status_unknown` with an `"unknown"`
 *  level: a check that RAN and could not establish the requirement — an answer,
 *  and it clears too, because leaving a stale warning standing would assert what
 *  nothing can establish any more. `bios_status_unknown` with no level: a read
 *  that never happened, the one payload that must change nothing, so it projects
 *  to `null` and the caller writes nothing (#1693).
 *
 *  `bios_level` is read only to tell those last two apart, never projected —
 *  that is the same split `panelState.biosFieldsFromCache` draws off the same
 *  payload, and a divergence would leave the badge and the BIOS tab disagreeing
 *  about whether a question was answered.
 *
 *  Core data is sourced separately via `extractCoreInfo` (the BIOS payload no
 *  longer carries it, #923). */
export function extractBiosInfo(answer: BiosAnswer): BiosInfoFields | null {
  if (answer.bios_status_unknown) {
    if (answer.bios_level !== "unknown") return null;
    return { biosNeeded: false, biosLabel: "", biosRequiredMissing: false };
  }
  if (!answer.bios_status) {
    return { biosNeeded: false, biosLabel: "", biosRequiredMissing: false };
  }
  const requiredCount = answer.bios_status.required_count ?? 0;
  const requiredDownloaded = answer.bios_status.required_downloaded ?? 0;
  // A required row nothing could judge is neither present nor absent, so it is
  // taken out of the count before the comparison: the badge says a required file
  // is NOT THERE, and a reading that established nothing has not shown that. A
  // row answered `false` stays in and DOES raise the badge — a declared folder
  // the resolver listed and found no BIOS image in is exactly the state the
  // badge is for.
  const requiredJudged = requiredCount - (answer.bios_status.required_withheld ?? 0);
  return {
    biosNeeded: true,
    biosLabel: answer.bios_label ?? "",
    biosRequiredMissing: requiredJudged > 0 && requiredDownloaded < requiredJudged,
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
