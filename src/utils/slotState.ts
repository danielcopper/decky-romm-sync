/**
 * Pure helpers that translate a `get_save_slots` callable response into the
 * side-effects the panel component needs to apply. Centralises the
 * "success:false means keep existing UI state" guard so it can be unit-tested
 * without rendering the panel component.
 *
 * Backend contract: on API failure the callable returns `success:false` with
 * an empty `slots` array so it doesn't clobber persisted state — the UI must
 * preserve the last-known good slot list rather than blank it on a transient
 * blip.
 */

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { SaveSlotSummary } from "../types";

export interface SlotsResponse {
  success: boolean;
  slots: SaveSlotSummary[];
  active_slot?: string | null;
  reason?: string;
  message?: string;
}

export interface RefreshSlotFields {
  activeSlot: string | null;
  /** Whether `activeSlot` is something a response said, or still the caller's
   *  own initial value. Only a success that CARRIES `active_slot` turns it on:
   *  a failure says nothing about the slot, and a success that omits the field
   *  keeps the previous name — so it keeps the previous verdict with it, rather
   *  than vouching for a value it never mentioned (#1747). */
  activeSlotKnown: boolean;
  availableSlots: SaveSlotSummary[];
}

export interface LoadSlotsFields extends RefreshSlotFields {
  slotsLoading: boolean;
}

/** Apply a `refreshSlotState` response: on success merge slots+active_slot,
 *  on failure leave UI untouched. */
export function applyRefreshSlotResult<S extends RefreshSlotFields>(
  slotResult: SlotsResponse,
  setter: Dispatch<SetStateAction<S>>,
): void {
  if (!slotResult.success) return;
  setter((prev) => ({
    ...prev,
    availableSlots: slotResult.slots,
    activeSlot: slotResult.active_slot === undefined ? prev.activeSlot : slotResult.active_slot,
    activeSlotKnown: slotResult.active_slot !== undefined || prev.activeSlotKnown,
  }));
}

/** Apply a `loadSlots` response: on success merge slots+active_slot and clear
 *  the loading spinner; on failure clear the spinner, log, and reset the
 *  loaded-once ref so a subsequent tab visit retries. */
export function applyLoadSlotsResult<S extends LoadSlotsFields>(
  result: SlotsResponse,
  setter: Dispatch<SetStateAction<S>>,
  loadedRef: MutableRefObject<boolean>,
  logError: (msg: string) => void,
): void {
  if (!result.success) {
    logError(`Failed to load save slots: ${result.message ?? result.reason ?? "unknown"}`);
    loadedRef.current = false;
    setter((prev) => ({ ...prev, slotsLoading: false }));
    return;
  }
  setter((prev) => ({
    ...prev,
    activeSlot: result.active_slot === undefined ? prev.activeSlot : result.active_slot,
    activeSlotKnown: result.active_slot !== undefined || prev.activeSlotKnown,
    availableSlots: result.slots,
    slotsLoading: false,
  }));
}
