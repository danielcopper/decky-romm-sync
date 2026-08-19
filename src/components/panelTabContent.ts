/**
 * The body of the game detail panel's active tab.
 *
 * Only the pane the user is looking at is created. That is load-bearing for
 * SAVES: `SavesTab` fetches on mount, so building it for a tab nobody opened
 * would fire those requests on every game page.
 *
 * The ACHIEVEMENTS and BIOS panes are not built here — the panel mounts them for
 * every ROM and they decide themselves whether to render.
 */

import { createElement } from "react";
import type { Dispatch, SetStateAction } from "react";
import { GameInfoTab } from "./GameInfoTab";
import { SavesTab } from "./SavesTab";
import { SlotSetupWizard } from "./SlotSetupWizard";
import type { PanelState } from "./panelState";

interface TabContentContext {
  readonly appId: number;
  readonly romId: number;
  readonly state: PanelState;
  readonly setState: Dispatch<SetStateAction<PanelState>>;
}

/** Tell every surface showing this ROM that its save-sync facts moved. */
function announceSaveSyncChange(romId: number): void {
  globalThis.dispatchEvent(
    new CustomEvent("romm_data_changed", {
      detail: { type: "save_sync", rom_id: romId },
    }),
  );
}

/** Build the pane for the active tab, or null when the active tab builds none. */
export function buildTabContent({
  appId,
  romId,
  state,
  setState,
}: TabContentContext): ReturnType<typeof createElement> | null {
  if (state.activeTab === "info") {
    return createElement(GameInfoTab, {
      key: "tab-info",
      romName: state.romName,
      regions: state.regions,
      languages: state.languages,
      metadata: state.metadata,
      platformName: state.platformName,
      coverBase64: state.coverBase64,
      installed: state.installed,
      installedRom: state.installedRom,
    });
  }

  if (state.activeTab !== "saves") return null;

  if (state.saveSyncEnabled && !state.slotConfirmed) {
    return createElement(SlotSetupWizard, {
      romId,
      onComplete: () => {
        setState((prev) => ({ ...prev, slotConfirmed: true }));
        announceSaveSyncChange(romId);
      },
    });
  }

  return createElement(SavesTab, {
    appId,
    romId,
    saveStatus: state.saveStatus,
    conflicts: state.conflicts,
    activeSlot: state.activeSlot,
    activeSlotKnown: state.activeSlotKnown,
    availableSlots: state.availableSlots,
    slotsLoading: state.slotsLoading,
    onSlotSwitched: (newSlot, newStatus) => {
      setState((prev) => ({
        ...prev,
        activeSlot: newSlot === "" ? null : newSlot,
        // A completed switch is an answer about the active slot in its own
        // right — the announce below re-reads the list, but the tab must not
        // fall back to "unknown" for the round trip in between (#1747).
        activeSlotKnown: true,
        saveStatus: newStatus,
        conflicts: newStatus.conflicts ?? [],
      }));
      announceSaveSyncChange(romId);
    },
  });
}
