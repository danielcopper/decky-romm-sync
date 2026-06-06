/**
 * Fire-and-forget background refresh helpers for the play-section row.
 *
 * Each helper hits a single backend callable, merges the response into the
 * caller's state via a typed setter, and swallows errors (logging where it's
 * useful for debugging). Generic over the consumer's state shape so the
 * helpers stay decoupled from any particular component's full state.
 */

import type { Dispatch, SetStateAction } from "react";
import { getSaveStatus, getBiosStatus, getPlatformCoreInfo, getAchievementProgress, debugLog } from "../api/backend";
import { extractBiosInfo, extractCoreInfo, type BiosInfoFields, type CoreInfoFields } from "./playSection";

interface ActiveSlotFields {
  activeSlot: string | null;
}

interface AchievementFields {
  achievementEarned: number;
  achievementTotal: number;
}

export function refreshActiveSlotInBackground<S extends ActiveSlotFields>(
  romId: number,
  cancelled: () => boolean,
  setter: Dispatch<SetStateAction<S>>,
): void {
  getSaveStatus(romId)
    .then((saveStatus) => {
      if (!cancelled() && "active_slot" in saveStatus) {
        setter((prev) => ({ ...prev, activeSlot: saveStatus.active_slot ?? null }));
      }
    })
    .catch(() => {});
}

export function refreshBiosInBackground<S extends BiosInfoFields>(
  romId: number,
  cancelled: () => boolean,
  setter: Dispatch<SetStateAction<S>>,
): void {
  getBiosStatus(romId)
    .then((result) => {
      const b = result.bios_status;
      if (!cancelled() && b) {
        setter((prev) => ({
          ...prev,
          ...extractBiosInfo(result.bios_level, result.bios_label),
        }));
      }
    })
    .catch((e) => debugLog(`Background BIOS status fetch error: ${e}`));
}

/** Refresh core-selection state from the dedicated `get_platform_core_info`
 *  path (#923), fully decoupled from BIOS status. Keyed on the platform slug. */
export function refreshCoreInfoInBackground<S extends CoreInfoFields>(
  platformSlug: string,
  cancelled: () => boolean,
  setter: Dispatch<SetStateAction<S>>,
): void {
  getPlatformCoreInfo(platformSlug)
    .then((coreInfo) => {
      if (!cancelled()) {
        setter((prev) => ({
          ...prev,
          ...extractCoreInfo(coreInfo),
        }));
      }
    })
    .catch((e) => debugLog(`Background core info fetch error: ${e}`));
}

export function refreshAchievementsInBackground<S extends AchievementFields>(
  romId: number,
  cancelled: () => boolean,
  setter: Dispatch<SetStateAction<S>>,
): void {
  getAchievementProgress(romId)
    .then((result) => {
      if (!cancelled() && result.success) {
        setter((prev) => ({
          ...prev,
          achievementEarned: result.earned,
          achievementTotal: result.total,
        }));
      }
    })
    .catch((e) => debugLog(`Background achievement progress fetch error: ${e}`));
}
