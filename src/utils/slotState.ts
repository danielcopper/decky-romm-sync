/**
 * Pure helpers that translate a `get_save_slots` callable response into the
 * side-effects the panel component needs to apply. Centralises the
 * "success:false means keep existing UI state" guard so it can be unit-tested
 * without rendering the panel component.
 *
 * Backend contract: on API failure the callable returns `success:false` with
 * an empty `slots` array so it doesn't clobber persisted state — the UI must
 * preserve the last-known good slot list rather than blank it on a transient
 * blip. Such a failure may additionally carry the persisted listing under
 * `last_known`, which lands in its own field and never in the live ones.
 */

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { LastKnownSlots, SaveSlotSummary } from "../types";

export interface SlotsResponse {
  success: boolean;
  slots: SaveSlotSummary[];
  active_slot?: string | null;
  reason?: string;
  message?: string;
  /** The persisted listing a failed fetch hands back for a confirmed ROM;
   *  null — never an empty list — when the device knows nothing, so "we know
   *  nothing" cannot be read as "this ROM has no slots" (#1755). */
  last_known?: {
    slots: SaveSlotSummary[];
    active_slot: string | null;
  } | null;
}

/** Read the snapshot a response carries, or `null` when it carries none. */
function lastKnownFrom(result: SlotsResponse): LastKnownSlots | null {
  const known = result.last_known;
  if (!known?.slots.length) return null;
  return { slots: known.slots, activeSlot: known.active_slot };
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
  /** The slots as of the last contact, for the surfaces that have nothing live
   *  to show. Written only by a failure that carries one and dropped by any
   *  success — it is history, so it never feeds the two fields above (#1755). */
  lastKnownSlots: LastKnownSlots | null;
}

export interface LoadSlotsFields extends RefreshSlotFields {
  slotsLoading: boolean;
}

/** Apply a `refreshSlotState` response: on success merge slots+active_slot,
 *  on failure take the last-known snapshot if one came with it and otherwise
 *  leave the UI untouched. */
export function applyRefreshSlotResult<S extends RefreshSlotFields>(
  slotResult: SlotsResponse,
  setter: Dispatch<SetStateAction<S>>,
): void {
  if (!slotResult.success) {
    const lastKnown = lastKnownFrom(slotResult);
    if (lastKnown) setter((prev) => ({ ...prev, lastKnownSlots: lastKnown }));
    return;
  }
  setter((prev) => ({
    ...prev,
    availableSlots: slotResult.slots,
    activeSlot: slotResult.active_slot === undefined ? prev.activeSlot : slotResult.active_slot,
    activeSlotKnown: slotResult.active_slot !== undefined || prev.activeSlotKnown,
    lastKnownSlots: null,
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
    const lastKnown = lastKnownFrom(result);
    setter((prev) => ({ ...prev, slotsLoading: false, lastKnownSlots: lastKnown ?? prev.lastKnownSlots }));
    return;
  }
  setter((prev) => ({
    ...prev,
    activeSlot: result.active_slot === undefined ? prev.activeSlot : result.active_slot,
    activeSlotKnown: result.active_slot !== undefined || prev.activeSlotKnown,
    availableSlots: result.slots,
    slotsLoading: false,
    lastKnownSlots: null,
  }));
}
