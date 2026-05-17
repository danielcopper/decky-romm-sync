/**
 * Fire-and-forget background refresh helpers for the play-section row.
 *
 * Each helper hits a single backend callable, merges the response into the
 * caller's state via a typed setter, and swallows errors (logging where it's
 * useful for debugging). Generic over the consumer's state shape so the
 * helpers stay decoupled from any particular component's full state.
 */

import type { Dispatch, SetStateAction } from "react";
import type { BiosStatus } from "../types";
import { getSaveStatus, getBiosStatus, getAchievementProgress, debugLog } from "../api/backend";
import { extractBiosInfo, type BiosInfoFields } from "./playSection";

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
      if (!cancelled() && saveStatus && "active_slot" in saveStatus) {
        setter((prev) => ({ ...prev, activeSlot: saveStatus.active_slot ?? null }));
      }
    })
    .catch(() => {});
}

export function refreshBiosInBackground<S extends BiosInfoFields>(
  romId: number,
  cancelled: boolean,
  setter: Dispatch<SetStateAction<S>>,
): void {
  getBiosStatus(romId)
    .then((result) => {
      const b = result.bios_status;
      if (!cancelled && b) {
        setter((prev) => ({
          ...prev,
          ...extractBiosInfo(b as BiosStatus, result.bios_level, result.bios_label),
        }));
      }
    })
    .catch((e) => debugLog(`Background BIOS status fetch error: ${e}`));
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
