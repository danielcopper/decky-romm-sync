/**
 * The game-detail panel's lazy SAVES slot load.
 *
 * The slot list is fetched on the first activation of the SAVES tab and not
 * again for the same ROM once it succeeds — `slotsLoadedRef` is that gate. A
 * fetch that fails, or is torn down mid-flight, reopens it so a reconnect or a
 * later activation retries. The gate is owned by the panel because a version
 * switch resets it (`handleVersionSwitched` in `panelEvents.ts`) so the slots
 * re-fetch for the newly-bound rom_id.
 */

import { useEffect } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { getSaveSlots, debugLog } from "../api/backend";
import {
  beginServerLoad,
  reportServerReachable,
  setServerRetryProgress,
  settleServerLoad,
  useRommConnectionState,
} from "../utils/connectionState";
import { applyLoadSlotsResult } from "../utils/slotState";
import { detach } from "../utils/detach";
import { takeReadTicket, type PanelReadSeqs, type PanelState } from "./panelState";

/** One run of the slot fetch. The effect's cleanup can tear a run down while it
 *  is still in flight, so the two ends share this record rather than a closure
 *  over each other's locals. */
interface SlotLoadRun {
  cancelled: boolean;
  settled: boolean;
}

/** Fetch the slot list for `romId` and fold it into panel state. */
async function fetchSlots(
  romId: number,
  run: SlotLoadRun,
  slotsLoadedRef: MutableRefObject<boolean>,
  readSeqs: MutableRefObject<PanelReadSeqs>,
  setState: Dispatch<SetStateAction<PanelState>>,
): Promise<void> {
  const load = beginServerLoad();
  // Drop stale retry progress from a prior load so the SavesTab's
  // ConnectingIndicator starts at plain "Connecting to RomM…" (#1345).
  setServerRetryProgress(null);
  // Taken here rather than in the effect: this is where the read is issued, and
  // it is what orders this lane against the panel's slot refreshes. `cancelled`
  // below cannot do it — the effect sets it only when React commits the
  // teardown, so an answer arriving before that commit still folds in (#1717).
  const slotsOvertaken = takeReadTicket(readSeqs, "slots");
  setState((prev) => ({ ...prev, slotsLoading: true }));
  try {
    const result = await getSaveSlots(romId);
    if (run.cancelled) return;
    run.settled = true;
    // A completed slot fetch is a reachability signal (#1345): a success
    // proves the server is reachable; an unreachable server drives the store
    // offline (which then renders the known-offline fast path in the effect
    // below on the next dependent run). Any OTHER failure reason is a
    // server-side "no" (the server answered), not a connectivity verdict —
    // leave the store untouched.
    if (result.success) {
      reportServerReachable(true);
    } else if (result.reason === "server_unreachable") {
      reportServerReachable(false);
    }
    if (result.success && slotsOvertaken()) {
      // A newer slot read owns the list; this run still owes the spinner it put
      // up. Only the list is fenced — a failure carries no slot data at all, and
      // its gate reset is what lets a later tab visit retry.
      setState((prev) => ({ ...prev, slotsLoading: false }));
      return;
    }
    applyLoadSlotsResult<PanelState>(result, setState, slotsLoadedRef, (msg) => {
      detach(debugLog(msg));
    });
  } catch (e) {
    detach(debugLog(`Failed to load save slots: ${e}`));
    if (!run.cancelled) {
      run.settled = true;
      slotsLoadedRef.current = false;
      setState((prev) => ({ ...prev, slotsLoading: false }));
    }
  } finally {
    // Clear-on-settle in addition to clear-on-start (#1345 F2) — refused
    // when a newer load of any lane already owns the shared frame.
    settleServerLoad(load);
  }
}

/** Release a run torn down before it settled — e.g. a concurrent call flipped
 *  the store offline and re-ran the effect. Gives the load-once gate back and
 *  drops the spinner, so the re-run isn't wedged behind a stuck
 *  slotsLoading/slotsLoadedRef and a later reconnect reliably reloads (#1345
 *  F2). A settled run already set its own final state — leave it alone. */
function releaseUnsettledRun(
  run: SlotLoadRun,
  slotsLoadedRef: MutableRefObject<boolean>,
  setState: Dispatch<SetStateAction<PanelState>>,
): void {
  if (run.settled) return;
  slotsLoadedRef.current = false;
  setState((prev) => (prev.slotsLoading ? { ...prev, slotsLoading: false } : prev));
}

/** Load the ROM's save slots once the SAVES tab is first shown. */
export function useSaveSlotsLoad(
  state: PanelState,
  slotsLoadedRef: MutableRefObject<boolean>,
  readSeqs: MutableRefObject<PanelReadSeqs>,
  setState: Dispatch<SetStateAction<PanelState>>,
): void {
  // The shared connection state lets the fetch take the known-offline fast path
  // below and re-load automatically on reconnect (#1345).
  const isOffline = useRommConnectionState() === "offline";
  const { activeTab, saveSyncEnabled, romId } = state;

  useEffect(() => {
    if (activeTab !== "saves" || !saveSyncEnabled || !romId) return;
    if (slotsLoadedRef.current) return;

    // Known-offline fast path (#1345): the server slot fetch runs through the
    // retry+backoff ladder, so on a known-unreachable server it would hang
    // "Loading slots…" for tens of seconds before the local degraded view (the
    // per-slot "Server unreachable" notices) renders. Skip the fetch entirely —
    // slotsLoading is still false here (the slotsLoadedRef guard above means the
    // connected path never set it), so SavesTab renders the degraded view now.
    // slotsLoadedRef stays false, so a flip back to connected re-runs this effect
    // (isOffline dep) and loads.
    if (isOffline) return;
    slotsLoadedRef.current = true;

    const run: SlotLoadRun = { cancelled: false, settled: false };
    detach(fetchSlots(romId, run, slotsLoadedRef, readSeqs, setState));
    return () => {
      run.cancelled = true;
      releaseUnsettledRun(run, slotsLoadedRef, setState);
    };
    // `slotsLoadedRef`, `readSeqs` and `setState` are stable for the panel's
    // lifetime (two useRef objects and a useState setter) and never re-run this
    // effect; they are listed only because arriving as parameters puts them out
    // of reach of exhaustive-deps' stability inference.
  }, [activeTab, saveSyncEnabled, romId, isOffline, slotsLoadedRef, readSeqs, setState]);
}
