/**
 * RomMGameInfoPanel — metadata and actions panel injected below the PlaySection
 * on RomM game detail pages.
 *
 * Layout:
 *   Game Info:    Platform, Description, Developer/Publisher, Genre tags, Release date
 *   ROM File:     Filename (only when installed)
 *   BIOS:         Status (only when platform needs BIOS)
 *   Save Sync:    Status (only when save sync enabled)
 *   Purely informational — all actions live in RomMPlaySection gear menu.
 *
 * Uses createElement throughout (no JSX) to match the RomMPlaySection pattern.
 * CSS classes prefixed with `romm-panel-` are injected separately by styleInjector.
 */

import { useState, useEffect, useRef, FC, createElement } from "react";
import { DialogButton, Focusable } from "@decky/ui";
import { getSaveSlots, debugLog, refreshMigrationState, logError } from "../api/backend";
import { SlotSetupWizard } from "./SlotSetupWizard";
import { SavesTab } from "./SavesTab";
import { AchievementsTab } from "./AchievementsTab";
import { BiosTab } from "./BiosTab";
import { infoRow, section } from "./panelSection";
import { loadData, type PanelState } from "./panelState";
import { wirePanelEvents } from "./panelEvents";
import { getMigrationState, onMigrationChange, setMigrationStatus } from "../utils/migrationStore";
import { getSettingsResetState, onSettingsResetChange } from "../utils/settingsResetStore";
import {
  getSaveSortMigrationState,
  onSaveSortMigrationChange,
  setSaveSortMigrationStatus,
} from "../utils/saveSortMigrationStore";
import {
  beginServerLoad,
  reportServerReachable,
  setServerRetryProgress,
  settleServerLoad,
  useRommConnectionState,
} from "../utils/connectionState";
import { applyLoadSlotsResult } from "../utils/slotState";
import { VersionErrorCard, useVersionError } from "./VersionErrorCard";
import { MigrationBlockedCard } from "./MigrationBlockedCard";
import { SettingsResetCard } from "./SettingsResetCard";
import { detach } from "../utils/detach";

interface RomMGameInfoPanelProps {
  appId: number;
}

/** Format a Unix timestamp (seconds) as a release date string (e.g. "15 Mar 2003") */
function formatReleaseDate(timestamp: number | null): string | null {
  if (!timestamp || timestamp <= 0) return null;
  const date = new Date(timestamp * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${date.getDate()} ${months[date.getMonth()]} ${date.getFullYear()}`;
}

// S3776 is raised on the declaration line, so its NOSONAR must stay there. prettier-ignore stops
// Prettier from relocating the trailing comment into the body (which would break the suppression).
// prettier-ignore
export const RomMGameInfoPanel: FC<RomMGameInfoPanelProps> = ({ appId }) => { // NOSONAR(typescript:S3776) — React FC fan-out; decomposed in #387/#391/Phase 7. Further split scatters handlers.
  // Subscribe to version error — re-renders when global state changes
  const versionError = useVersionError();
  // Subscribe to the shared connection state so the saves-tab slot load can take
  // the known-offline fast path and re-load automatically on reconnect (#1345).
  const isOffline = useRommConnectionState() === "offline";

  const [state, setState] = useState<PanelState>({
    loading: true,
    romId: null,
    romName: "",
    platformName: "",
    platformSlug: "",
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
  // Load-once gate for the lazy-loaded SAVES tab data. A version switch resets it
  // (`handleVersionSwitched` in panelEvents.ts) so the slots re-fetch for the
  // newly-bound rom_id instead of lingering from the previous version.
  const slotsLoadedRef = useRef(false);
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

    detach(loadData(appId, () => cancelled, romIdRef, platformSlugRef, setState));

    const unwire = wirePanelEvents({
      appId,
      cancelled: () => cancelled,
      romIdRef,
      platformSlugRef,
      slotsLoadedRef,
      setState,
    });

    return () => {
      cancelled = true;
      unwire();
    };
  }, [appId]);

  useEffect(() => {
    if (state.activeTab !== "saves" || !state.saveSyncEnabled || !state.romId) return;
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

    const load = beginServerLoad();
    let cancelled = false;
    let settled = false;

    async function loadSlots() {
      // Drop stale retry progress from a prior load so the SavesTab's
      // ConnectingIndicator starts at plain "Connecting to RomM…" (#1345).
      setServerRetryProgress(null);
      setState((prev) => ({ ...prev, slotsLoading: true }));
      try {
        if (!state.romId) return;
        const result = await getSaveSlots(state.romId);
        if (cancelled) return;
        settled = true;
        // A completed slot fetch is a reachability signal (#1345): a success
        // proves the server is reachable; an unreachable server drives the store
        // offline (which then renders the fast path above on the next dependent
        // run). Any OTHER failure reason is a server-side "no" (the server
        // answered), not a connectivity verdict — leave the store untouched.
        if (result.success) {
          reportServerReachable(true);
        } else if (result.reason === "server_unreachable") {
          reportServerReachable(false);
        }
        applyLoadSlotsResult<PanelState>(result, setState, slotsLoadedRef, (msg) => {
          detach(debugLog(msg));
        });
      } catch (e) {
        detach(debugLog(`Failed to load save slots: ${e}`));
        if (!cancelled) {
          settled = true;
          slotsLoadedRef.current = false;
          setState((prev) => ({ ...prev, slotsLoading: false }));
        }
      } finally {
        // Clear-on-settle in addition to clear-on-start (#1345 F2) — refused
        // when a newer load of any lane already owns the shared frame.
        settleServerLoad(load);
      }
    }

    detach(loadSlots());
    return () => {
      cancelled = true;
      // Torn down mid-flight (e.g. a concurrent call flipped the store offline,
      // re-running this effect) — release the load-once gate and drop the spinner
      // so the re-run isn't wedged behind a stuck slotsLoading/slotsLoadedRef, and
      // a later reconnect reliably reloads (#1345 F2). A settled load already set
      // its own final state, so leave it alone.
      if (!settled) {
        slotsLoadedRef.current = false;
        setState((prev) => (prev.slotsLoading ? { ...prev, slotsLoading: false } : prev));
      }
    };
  }, [state.activeTab, state.saveSyncEnabled, state.romId, isOffline]);

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
  const meta = state.metadata;

  // --- Game Info section ---
  const gameInfoChildren: ReturnType<typeof createElement>[] = [];

  // The RomM game name (distinct from the Steam shortcut hero title, which can differ).
  if (state.romName) {
    gameInfoChildren.push(createElement("div", { key: "rom-name", className: "romm-panel-rom-name" }, state.romName));
  }

  // Region / Languages of the active version (ADR-0021) — omitted when empty.
  if (state.regions.length > 0) {
    gameInfoChildren.push(infoRow("regions", "Region", state.regions.join("/")));
  }
  if (state.languages.length > 0) {
    gameInfoChildren.push(infoRow("languages", "Languages", state.languages.join(", ")));
  }

  if (meta) {
    if (meta.summary) {
      gameInfoChildren.push(createElement("div", { key: "summary", className: "romm-panel-summary" }, meta.summary));
    }

    // Platform after description
    if (state.platformName) {
      gameInfoChildren.push(infoRow("platform", "Platform", state.platformName));
    }

    if (meta.companies.length > 0) {
      gameInfoChildren.push(infoRow("companies", "Developer / Publisher", meta.companies.join(", ")));
    }

    if (meta.genres.length > 0) {
      gameInfoChildren.push(
        createElement(
          "div",
          { key: "genres", className: "romm-panel-info-row" },
          createElement("span", { className: "romm-panel-label" }, "Genres"),
          createElement(
            "div",
            { className: "romm-panel-tags" },
            ...meta.genres.map((g) => createElement("span", { key: g, className: "romm-panel-tag" }, g)),
          ),
        ),
      );
    }

    const releaseDate = formatReleaseDate(meta.first_release_date);
    if (releaseDate) {
      gameInfoChildren.push(infoRow("release-date", "Release Date", releaseDate));
    }

    if (meta.game_modes.length > 0) {
      gameInfoChildren.push(infoRow("game-modes", "Game Modes", meta.game_modes.join(", ")));
    }

    if (meta.player_count) {
      gameInfoChildren.push(infoRow("players", "Players", meta.player_count));
    }

    if (meta.average_rating != null && meta.average_rating > 0) {
      gameInfoChildren.push(infoRow("rating", "Rating", `${Math.round(meta.average_rating)}%`));
    }
  } else if (state.platformName) {
    // No metadata — still show platform
    gameInfoChildren.push(infoRow("platform", "Platform", state.platformName));
  }

  // "No metadata available" fires only when NO descriptive row was added (name,
  // region/languages, metadata, or platform). The version switcher no longer lives
  // here — it moved to the play-button section (#1297) — so this is a plain count
  // of the descriptive rows.
  if (gameInfoChildren.length === 0) {
    gameInfoChildren.push(createElement("div", { key: "no-meta", className: "romm-panel-muted" }, "No metadata available"));
  }
  const gameInfoContent = gameInfoChildren;

  const gameInfoSection = state.coverBase64
    ? section(
        "game-info",
        null,
        createElement(
          "div",
          {
            key: "game-info-row",
            style: { display: "flex", gap: "16px", alignItems: "flex-start" },
          },
          createElement("img", {
            key: "cover",
            src: `data:image/png;base64,${state.coverBase64}`,
            style: { width: "120px", borderRadius: "4px", flexShrink: 0, objectFit: "cover" as const },
          }),
          createElement("div", { key: "details", style: { flex: 1 } }, ...gameInfoContent),
        ),
      )
    : section("game-info", null, ...gameInfoContent);

  // --- ROM File section (only when installed) ---
  // A downloaded ROM the system cannot launch keeps its files and its row; only
  // the shortcut's launch command is withheld. This is the one place that says
  // so — without it the game simply never starts and nothing explains why.
  const noLaunchTargetNote =
    state.installedRom && !state.installedRom.launchable
      ? createElement(
          "div",
          { key: "no-launch-target", className: "romm-panel-muted", style: { marginTop: "4px" } },
          `Downloaded, but nothing here is a format ${state.installedRom.system} can launch — no launch ` +
            `command was set. The files are on disk; install them in the emulator to play.`,
        )
      : null;

  const romFileSection =
    state.installed && state.installedRom
      ? section(
          "rom-file",
          "ROM File",
          infoRow("filename", "Filename", state.installedRom.file_name),
          noLaunchTargetNote,
        )
      : null;

  // --- Tab bar ---
  const tabs: { id: string; label: string; visible: boolean }[] = [
    { id: "info", label: "GAME INFO", visible: true },
    { id: "achievements", label: "ACHIEVEMENTS", visible: !!state.raId },
    { id: "saves", label: "SAVES", visible: state.saveSyncEnabled },
    { id: "bios", label: "BIOS", visible: !!state.biosStatus },
  ];

  const tabBar = createElement(
    Focusable,
    {
      className: "romm-tab-bar",
      "flow-children": "right",
      "data-romm": "true",
    },
    ...tabs
      .filter((t) => t.visible)
      .map((t) =>
        createElement(
          DialogButton,
          {
            key: `tab-${t.id}`,
            className: `romm-tab ${state.activeTab === t.id ? "romm-tab-active" : ""}`,
            onClick: () => setState((prev) => ({ ...prev, activeTab: t.id })),
            style: {
              background: "transparent",
              border: "none",
              borderBottom: state.activeTab === t.id ? "2px solid #1a9fff" : "2px solid transparent",
              padding: "10px 16px",
              minWidth: "auto",
              width: "auto",
            },
            noFocusRing: false,
          },
          t.label,
        ),
      ),
  );

  const saveSortWarning = saveSortPending
    ? createElement(
        "div",
        {
          key: "save-sort-warning",
          style: {
            padding: "8px 12px",
            marginBottom: "12px",
            backgroundColor: "rgba(212, 167, 44, 0.15)",
            borderLeft: "3px solid #d4a72c",
            borderRadius: "4px",
          },
        },
        createElement(
          "div",
          {
            style: { fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" },
          },
          "\u26A0\uFE0F RetroArch save sorting changed",
        ),
        createElement(
          "div",
          {
            style: { fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" },
          },
          "Save file paths may be incorrect. Go to Settings to migrate.",
        ),
      )
    : null;

  // --- Determine active tab content ---
  // The ACHIEVEMENTS and BIOS panes are not built here: they are mounted below
  // for every ROM and decide themselves whether to render.
  let activeTabContent: ReturnType<typeof createElement> | null = null;
  if (state.activeTab === "info") {
    activeTabContent = createElement("div", { key: "tab-info" }, gameInfoSection, romFileSection);
  } else if (state.activeTab === "saves") {
    if (state.saveSyncEnabled && !state.slotConfirmed) {
      activeTabContent = createElement(SlotSetupWizard, {
        romId: state.romId,
        onComplete: () => {
          // Refresh: mark as configured and reload save status
          setState((prev) => ({ ...prev, slotConfirmed: true }));
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "save_sync", rom_id: state.romId },
            }),
          );
        },
      });
    } else {
      activeTabContent = createElement(SavesTab, {
        appId,
        romId,
        saveStatus: state.saveStatus,
        conflicts: state.conflicts,
        activeSlot: state.activeSlot,
        availableSlots: state.availableSlots,
        slotsLoading: state.slotsLoading,
        onSlotSwitched: (newSlot, newStatus) => {
          setState((prev) => ({
            ...prev,
            activeSlot: newSlot === "" ? null : newSlot,
            saveStatus: newStatus,
            conflicts: newStatus.conflicts ?? [],
          }));
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "save_sync", rom_id: state.romId },
            }),
          );
        },
      });
    }
  }

  return createElement(
    "div",
    { "data-romm": "true" },
    saveSortWarning,
    tabBar,
    createElement(
      Focusable,
      {
        noFocusRing: true,
        className: "romm-tab-content",
        style: { paddingBottom: "48px" },
      },
      activeTabContent,
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
