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
import { GameInfoTab } from "./GameInfoTab";
import { SavesTab } from "./SavesTab";
import { SlotSetupWizard } from "./SlotSetupWizard";
import type { PanelState, RomBinding } from "./panelState";

interface TabContentContext {
  readonly appId: number;
  readonly binding: RomBinding;
  readonly state: PanelState;
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
export function buildTabContent({ appId, binding, state }: TabContentContext): ReturnType<typeof createElement> | null {
  const romId = binding.romId;

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
        // Bound for the same reason the slot switch below is: the wizard calls
        // this after awaiting the confirm, and it is unkeyed too. `slotConfirmed`
        // is the gate deciding which of the two panes this function builds, so a
        // stale `true` puts a saves tab in front of a version whose tracking is
        // not configured (#1754).
        binding.write((prev) => ({ ...prev, slotConfirmed: true }));
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
    lastKnownSlots: state.lastKnownSlots,
    slotsLoading: state.slotsLoading,
    onSlotSwitched: (newSlot, newStatus) => {
      // `SavesTab` calls this after awaiting the switch, and gets no React key
      // of its own: it survives the version switch that re-points the panel to
      // another rom_id under the same appId, still holding the callback it was
      // rendered with. Only the ROM this pane was built for separates that
      // answer from one about the version now showing (#1754).
      binding.write((prev) => ({
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
