/**
 * RomMGameInfoPanel — the RomM panel injected below the PlaySection on game
 * detail pages.
 *
 * The shell around the GAME INFO / ACHIEVEMENTS / SAVES / BIOS panes: it owns
 * the panel's ROM identity, its event lane, the tab selection, and the
 * whole-panel replacements (version mismatch, corrupt-settings reset, pending
 * RetroDECK migration) that pre-empt all of them.
 *
 * The panel body itself takes no ROM-level actions — download, play, uninstall,
 * version and core selection live in the RomMPlaySection gear menu. The panes
 * below it are not all passive: the SAVES pane switches slots and rolls back
 * versions.
 *
 * Uses createElement throughout (no JSX) to match the RomMPlaySection pattern.
 * CSS classes prefixed with `romm-panel-` are injected separately by styleInjector.
 */

import { useState, useEffect, useRef, FC, createElement } from "react";
import { Focusable } from "@decky/ui";
import { refreshMigrationState, logError } from "../api/backend";
import { AchievementsTab } from "./AchievementsTab";
import { BiosTab } from "./BiosTab";
import { PanelTabBar } from "./PanelTabBar";
import { SaveSortWarning } from "./SaveSortWarning";
import { loadData, type PanelReadSeqs, type PanelState } from "./panelState";
import { wirePanelEvents } from "./panelEvents";
import { useSaveSlotsLoad } from "./panelSlotsLoad";
import { buildTabContent } from "./panelTabContent";
import { getMigrationState, onMigrationChange, setMigrationStatus } from "../utils/migrationStore";
import { getSettingsResetState, onSettingsResetChange } from "../utils/settingsResetStore";
import {
  getSaveSortMigrationState,
  onSaveSortMigrationChange,
  setSaveSortMigrationStatus,
} from "../utils/saveSortMigrationStore";
import { VersionErrorCard, useVersionError } from "./VersionErrorCard";
import { MigrationBlockedCard } from "./MigrationBlockedCard";
import { SettingsResetCard } from "./SettingsResetCard";
import { detach } from "../utils/detach";

interface RomMGameInfoPanelProps {
  appId: number;
}

export const RomMGameInfoPanel: FC<RomMGameInfoPanelProps> = ({ appId }) => {
  // Subscribe to version error — re-renders when global state changes
  const versionError = useVersionError();

  const [state, setState] = useState<PanelState>({
    loading: true,
    romId: null,
    romName: "",
    platformName: "",
    installed: false,
    installedRom: null,
    metadata: null,
    coverBase64: null,
    biosStatus: null,
    biosLevel: null,
    coreInfo: null,
    saveSyncEnabled: false,
    saveStatus: null,
    conflicts: [],
    error: false,
    activeTab: "info",
    raId: null,
    slotConfirmed: false,
    activeSlot: "default",
    availableSlots: [],
    slotsLoading: false,
    regions: [],
    languages: [],
  });
  const romIdRef = useRef<number | null>(null);
  // Tracks the panel's own platform so the broadcast `bios` data-changed
  // handler can reject events for other platforms without a stale closure
  // (mirrors romIdRef). bios events fan out to every mounted panel (#1082).
  const platformSlugRef = useRef<string>("");
  // Load-once gate for the lazy SAVES tab data; the rule it enforces is stated
  // at `panelSlotsLoad.ts`. It lives here because a version switch resets it.
  const slotsLoadedRef = useRef(false);
  // Read sequences ordering two answers about the same ROM; the rule they
  // enforce is stated at `takeReadTicket`. They live here because the loads, the
  // event lane and the lazy slots lane take tickets from the same counters.
  const readSeqs = useRef<PanelReadSeqs>({ detail: 0, saveStatus: 0, slots: 0, slotTracking: 0 });
  const [migration, setMigration] = useState(getMigrationState());
  const [settingsReset, setSettingsReset] = useState(getSettingsResetState());
  const [saveSortPending, setSaveSortPending] = useState(getSaveSortMigrationState().pending);

  useEffect(() => {
    const unsub = onMigrationChange(() => setMigration(getMigrationState()));
    const unsubSettingsReset = onSettingsResetChange(() => setSettingsReset(getSettingsResetState()));
    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortPending(getSaveSortMigrationState().pending));
    return () => {
      unsub();
      unsubSettingsReset();
      unsubSaveSort();
    };
  }, []);

  useEffect(() => {
    refreshMigrationState()
      .then(({ retrodeck, save_sort }) => {
        setMigrationStatus(retrodeck);
        setSaveSortMigrationStatus(save_sort);
      })
      .catch((e) => logError(`Failed to refresh migration state: ${e}`));
  }, [appId]);

  useEffect(() => {
    let cancelled = false;

    detach(loadData(appId, () => cancelled, romIdRef, platformSlugRef, readSeqs, setState));

    const unwire = wirePanelEvents({
      appId,
      cancelled: () => cancelled,
      romIdRef,
      platformSlugRef,
      slotsLoadedRef,
      readSeqs,
      setState,
    });

    return () => {
      cancelled = true;
      unwire();
    };
  }, [appId]);

  useSaveSlotsLoad(state, slotsLoadedRef, readSeqs, setState);

  // --- Version mismatch — replace entire panel with polished error card ---
  if (versionError) {
    return createElement("div", { "data-romm": "true" }, createElement(VersionErrorCard, { message: versionError }));
  }

  // --- Corrupt-settings reset — surface the reason instead of a bare "offline" ---
  if (settingsReset.pending) {
    return createElement(
      "div",
      { "data-romm": "true" },
      createElement(SettingsResetCard, { backedUpTo: settingsReset.backedUpTo, compact: true }),
    );
  }

  // --- Pending RetroDECK migration — block the page until resolved ---
  if (migration.pending) {
    return createElement("div", { "data-romm": "true" }, createElement(MigrationBlockedCard, {}));
  }

  // --- Loading state ---
  // Use minHeight so Steam's scroll container allocates enough space
  // before async data loads and expands the panel.
  if (state.loading) {
    return createElement(
      "div",
      {
        "data-romm": "true",
        className: "romm-panel-container",
        style: { minHeight: "500px" },
      },
      createElement("div", { className: "romm-panel-loading" }, "Loading..."),
    );
  }

  // --- Error / not found state ---
  if (state.error || !state.romId) {
    return null;
  }

  const romId = state.romId;

  return createElement(
    "div",
    { "data-romm": "true" },
    saveSortPending ? createElement(SaveSortWarning, { key: "save-sort-warning" }) : null,
    createElement(PanelTabBar, {
      activeTab: state.activeTab,
      hasAchievements: !!state.raId,
      hasSaves: state.saveSyncEnabled,
      hasBios: !!state.biosStatus,
      onSelect: (tabId) => setState((prev) => ({ ...prev, activeTab: tabId })),
    }),
    createElement(
      Focusable,
      {
        noFocusRing: true,
        className: "romm-tab-content",
        style: { paddingBottom: "48px" },
      },
      buildTabContent({ appId, romId, state, setState }),
      // Mounted for every ROM and rendering nothing until their tab is active.
      // For achievements that is load-bearing: the list is fetched by the tab
      // itself, and unmounting it on a tab switch would re-fetch (and re-spinner)
      // on every visit. Its key is the ROM, so a version switch remounts it —
      // which is how its per-rom state and load-once gate re-key. BiosTab needs
      // no key: it renders from props and holds nothing of its own.
      createElement(AchievementsTab, {
        key: `achievements-${romId}`,
        romId,
        raId: state.raId,
        isActive: state.activeTab === "achievements",
      }),
      createElement(BiosTab, {
        biosStatus: state.biosStatus,
        biosLevel: state.biosLevel,
        coreInfo: state.coreInfo,
        isActive: state.activeTab === "bios",
      }),
    ),
  );
};
