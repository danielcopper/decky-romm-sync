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
// DialogButton is natively focusable by Steam's gamepad engine (unlike Focusable
// wrappers around non-interactive content, which don't register in this injection
// context). Style as content sections, not buttons.
import {
  getCachedGameDetail,
  _cachedGameDetailCache,
  getRomMetadata,
  getInstalledRom,
  checkPlatformBios,
  getSaveStatus,
  getArtworkBase64,
  getAchievements,
  getAchievementProgress,
  getSaveSlots,
  isSaveTrackingConfigured,
  debugLog,
} from "../api/backend";
import { SlotSetupWizard } from "./SlotSetupWizard";
import { SavesTab } from "./SavesTab";
import type { RomMetadata, InstalledRom, BiosStatus, SaveStatus, PendingConflict, Achievement, AchievementProgress, EarnedAchievement, SaveSlotSummary } from "../types";
import { getMigrationState, onMigrationChange } from "../utils/migrationStore";
import { getSaveSortMigrationState, onSaveSortMigrationChange } from "../utils/saveSortMigrationStore";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";

interface RomMGameInfoPanelProps {
  appId: number;
}

interface PanelState {
  loading: boolean;
  romId: number | null;
  romName: string;
  platformName: string;
  platformSlug: string;
  installed: boolean;
  installedRom: InstalledRom | null;
  metadata: RomMetadata | null;
  coverBase64: string | null;
  biosStatus: BiosStatus | null;
  saveSyncEnabled: boolean;
  saveStatus: SaveStatus | null;
  conflicts: PendingConflict[];
  error: boolean;
  activeTab: string;
  achievements: Achievement[];
  achievementProgress: AchievementProgress | null;
  achievementsLoading: boolean;
  raId: number | null;
  slotConfirmed: boolean;
  activeSlot: string | null;
  availableSlots: SaveSlotSummary[];
  slotsLoading: boolean;
}

/** Format a Unix timestamp (seconds) as a release date string (e.g. "15 Mar 2003") */
function formatReleaseDate(timestamp: number | null): string | null {
  if (!timestamp || timestamp <= 0) return null;
  const date = new Date(timestamp * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${date.getDate()} ${months[date.getMonth()]} ${date.getFullYear()}`;
}

/** Refresh slot configuration and available slots — extracted to reduce nesting depth. */
function refreshSlotState(
  romId: number,
  setter: React.Dispatch<React.SetStateAction<PanelState>>,
): void {
  isSaveTrackingConfigured(romId)
    .then((result) => setter((prev) => ({ ...prev, slotConfirmed: result.configured })))
    .catch(() => {});
  getSaveSlots(romId)
    .then((slotResult) => {
      setter((prev) => {
        const newSlot = slotResult.active_slot === undefined ? prev.activeSlot : slotResult.active_slot;
        return {
          ...prev,
          availableSlots: slotResult.slots || [],
          activeSlot: newSlot,
        };
      });
    })
    .catch(() => {});
}

export const RomMGameInfoPanel: FC<RomMGameInfoPanelProps> = ({ appId }) => {
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
    saveSyncEnabled: false,
    saveStatus: null,
    conflicts: [],
    error: false,
    activeTab: "info",
    achievements: [],
    achievementProgress: null,
    achievementsLoading: false,
    raId: null,
    slotConfirmed: false,
    activeSlot: "default",
    availableSlots: [],
    slotsLoading: false,
  });
  const romIdRef = useRef<number | null>(null);
  const [migrationPending, setMigrationPending] = useState(getMigrationState().pending);
  const [saveSortPending, setSaveSortPending] = useState(getSaveSortMigrationState().pending);

  useEffect(() => {
    const unsub = onMigrationChange(() => setMigrationPending(getMigrationState().pending));
    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortPending(getSaveSortMigrationState().pending));
    return () => { unsub(); unsubSaveSort(); };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadData() {
      try {
        // Phase 1: Cache-first — render instantly from cached data
        const cached = await getCachedGameDetail(appId);
        if (cancelled) return;
        if (!cached.found) {
          setState((prev) => ({ ...prev, loading: false, error: true }));
          return;
        }

        const romId = cached.rom_id!;
        const romName = cached.rom_name || "";
        const platformName = cached.platform_name || "";
        const platformSlug = cached.platform_slug || "";

        romIdRef.current = romId;

        // Build initial BIOS status from cache
        let biosStatus: BiosStatus | null = null;
        if (cached.bios_status) {
          biosStatus = {
            needs_bios: true,
            ...cached.bios_status,
          };
        }

        // Build initial save status from cache
        let saveStatus: SaveStatus | null = null;
        if (cached.save_status) {
          saveStatus = {
            rom_id: romId,
            files: cached.save_status.files.map((f) => ({
              filename: f.filename,
              status: f.status as "skip" | "download" | "upload" | "conflict",
              local_path: null,
              local_hash: null,
              local_mtime: null,
              local_size: null,
              server_save_id: null,
              server_file_name: null,
              server_emulator: null,
              server_updated_at: null,
              server_size: null,
              last_sync_at: f.last_sync_at ?? null,
            })),
            playtime: { total_seconds: 0, session_count: 0, last_session_start: null, last_session_duration_sec: null },
            device_id: "",
            last_sync_check_at: cached.save_status.last_sync_check_at ?? null,
          };
        }

        // Use pre-computed conflicts from backend
        const conflicts: PendingConflict[] = cached.save_status?.conflicts ?? [];

        // Store ra_id for tab visibility
        const raId = (cached as any).ra_id ?? null;

        // Render immediately with cached data (metadata may be null — that's OK)
        setState({
          loading: false,
          romId,
          romName,
          platformName,
          platformSlug,
          installed: cached.installed ?? false,
          installedRom: null, // Will be filled by background fetch if installed
          metadata: cached.metadata as RomMetadata | null,
          coverBase64: null, // Will be filled by background fetch
          biosStatus,
          saveSyncEnabled: cached.save_sync_enabled ?? false,
          saveStatus,
          conflicts,
          error: false,
          activeTab: "info",
          achievements: [],
          achievementProgress: null,
          achievementsLoading: false,
          raId,
          slotConfirmed: false,
          activeSlot: "default",
          availableSlots: [],
          slotsLoading: false,
        });

        // Check if save slot tracking is configured for this game
        if (cached.save_sync_enabled) {
          refreshSlotState(romId, setState);
        }

        // Phase 2: Background fetch for data not available in cache
        // (installed ROM details, cover art, full save/BIOS detail, metadata if missing)
        const bgPromises: Promise<void>[] = [];

        // Installed ROM details (for filename display)
        if (cached.installed) {
          bgPromises.push(
            getInstalledRom(romId).then((installed) => {
              if (!cancelled && installed) {
                setState((prev) => ({ ...prev, installedRom: installed }));
              }
            }).catch(() => {}),
          );
        }

        // Cover art
        bgPromises.push(
          getArtworkBase64(romId).then((result) => {
            if (!cancelled && result.base64) {
              setState((prev) => ({ ...prev, coverBase64: result.base64 }));
            }
          }).catch(() => {}),
        );

        // Metadata (if missing or stale)
        const metaStale = cached.stale_fields?.includes("metadata") ?? true;
        if (!cached.metadata || metaStale) {
          bgPromises.push(
            getRomMetadata(romId).then((meta) => {
              if (!cancelled && meta) {
                setState((prev) => ({ ...prev, metadata: meta }));
              }
            }).catch(() => {}),
          );
        }

        await Promise.all(bgPromises);
      } catch (e) {
        debugLog(`RomMGameInfoPanel: loadData error: ${e}`);
        if (!cancelled) setState((prev) => ({ ...prev, loading: false, error: true }));
      }
    }

    loadData();

    // Listen for uninstall events to update state (uses ref to avoid stale closure)
    const onUninstall = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.rom_id === romIdRef.current) {
        setState((prev) => ({ ...prev, installed: false, installedRom: null }));
      }
    };
    window.addEventListener("romm_rom_uninstalled", onUninstall);

    const onDataChanged = async (e: Event) => {
      try {
      const detail = (e as CustomEvent).detail;
      if (!romIdRef.current) return;

      if (detail?.type === "save_sync_settings") {
        const enabled = detail.save_sync_enabled as boolean;
        if (enabled) {
          const updatedStatus = await getSaveStatus(romIdRef.current).catch((): SaveStatus | null => null);
          const conflicts: PendingConflict[] = updatedStatus?.conflicts ?? [];
          setState((prev) => ({
            ...prev,
            saveSyncEnabled: true,
            saveStatus: updatedStatus,
            conflicts,
          }));
        } else {
          setState((prev) => ({ ...prev, saveSyncEnabled: false }));
        }
        return;
      }

      if (detail?.type === "save_sync" && (!detail.rom_id || detail.rom_id === romIdRef.current)) {
        const updatedStatus: SaveStatus | null = detail.save_status ?? await getSaveStatus(romIdRef.current).catch((): SaveStatus | null => null);
        const conflicts: PendingConflict[] = updatedStatus?.conflicts ?? [];
        setState((prev) => ({
          ...prev,
          saveStatus: updatedStatus,
          conflicts,
        }));
        // Also re-check slot configuration + refresh slot data
        refreshSlotState(romIdRef.current, setState);
      } else if (detail?.type === "bios" && detail.platform_slug) {
        const updated = await checkPlatformBios(detail.platform_slug).catch((): BiosStatus => ({ needs_bios: false }));
        setState((prev) => ({ ...prev, biosStatus: updated.needs_bios ? updated : null }));
      } else if (detail?.type === "core_changed") {
        // Re-fetch cached game detail to pick up new core info
        delete (_cachedGameDetailCache as Record<number, unknown>)[appId];
        const cached = await getCachedGameDetail(appId);
        if (cancelled || !cached.found) return;
        let biosStatus: BiosStatus | null = null;
        if (cached.bios_status) {
          biosStatus = {
            needs_bios: true,
            ...cached.bios_status,
          };
        }
        setState((prev) => ({ ...prev, biosStatus }));
      } else if (detail?.type === "metadata" && detail.rom_id === romIdRef.current) {
        const meta = await getRomMetadata(romIdRef.current).catch((): RomMetadata | null => null);
        setState((prev) => ({ ...prev, metadata: meta }));
      }
      } catch (err) {
        debugLog(`RomMGameInfoPanel: onDataChanged error: ${err}`);
      }
    };
    window.addEventListener("romm_data_changed", onDataChanged);

    const onTabSwitch = (e: Event) => {
      const tab = (e as CustomEvent).detail?.tab;
      if (tab) setState((prev) => ({ ...prev, activeTab: tab }));
    };
    window.addEventListener("romm_tab_switch", onTabSwitch);

    return () => {
      cancelled = true;
      window.removeEventListener("romm_rom_uninstalled", onUninstall);
      window.removeEventListener("romm_data_changed", onDataChanged);
      window.removeEventListener("romm_tab_switch", onTabSwitch);
    };
  }, [appId]);


  // Lazy-load achievements when the achievements tab becomes active
  const achievementsLoadedRef = useRef(false);
  useEffect(() => {
    if (state.activeTab !== "achievements" || !state.raId || !state.romId) return;
    if (achievementsLoadedRef.current) return;
    achievementsLoadedRef.current = true;

    let cancelled = false;
    setState((prev) => ({ ...prev, achievementsLoading: true }));

    async function loadAchievements() {
      try {
        const [listResult, progressResult] = await Promise.all([
          getAchievements(state.romId!),
          getAchievementProgress(state.romId!),
        ]);
        if (cancelled) return;
        setState((prev) => ({
          ...prev,
          achievements: listResult.success ? listResult.achievements : [],
          achievementProgress: progressResult.success ? progressResult : null,
          achievementsLoading: false,
        }));
      } catch (e) {
        debugLog(`Failed to load achievements: ${e}`);
        if (!cancelled) {
          achievementsLoadedRef.current = false;
          setState((prev) => ({ ...prev, achievementsLoading: false }));
        }
      }
    }

    loadAchievements();
    return () => { cancelled = true; };
  }, [state.activeTab, state.raId, state.romId]);

  const slotsLoadedRef = useRef(false);
  useEffect(() => {
    if (state.activeTab !== "saves" || !state.saveSyncEnabled || !state.romId) return;
    if (slotsLoadedRef.current) return;
    slotsLoadedRef.current = true;

    let cancelled = false;
    setState((prev) => ({ ...prev, slotsLoading: true }));

    async function loadSlots() {
      try {
        if (!state.romId) return;
        const result = await getSaveSlots(state.romId);
        if (cancelled) return;
        setState((prev) => ({
          ...prev,
          activeSlot: result.active_slot === undefined ? prev.activeSlot : result.active_slot,
          availableSlots: result.slots || [],
          slotsLoading: false,
        }));
      } catch (e) {
        debugLog(`Failed to load save slots: ${e}`);
        if (!cancelled) {
          slotsLoadedRef.current = false;
          setState((prev) => ({ ...prev, slotsLoading: false }));
        }
      }
    }

    loadSlots();
    return () => { cancelled = true; };
  }, [state.activeTab, state.saveSyncEnabled, state.romId]);

  // --- Render helpers ---

  /** A labeled info row: LABEL on the left, value on the right */
  const infoRow = (key: string, label: string, value: string) =>
    createElement("div", { key, className: "romm-panel-info-row" },
      createElement("span", { className: "romm-panel-label" }, label),
      createElement("span", { className: "romm-panel-value" }, value),
    );

  /** A section with a title and children — uses DialogButton (not Focusable)
   *  because DialogButton is natively focusable by Steam's gamepad engine.
   *  Styled to look like a content section, not a button.
   *  Steam's outer scroll container auto-scrolls to focused elements. */
  const section = (key: string, title: string | null, ...children: (ReturnType<typeof createElement> | null)[]) =>
    createElement(DialogButton as any, {
      key,
      className: "romm-panel-section",
      style: {
        background: "transparent",
        border: "none",
        padding: "12px 0",
        textAlign: "left" as const,
        width: "100%",
        cursor: "default",
        display: "block",
      },
      noFocusRing: false,
      onFocus: scrollFocusedToCenter,
    },
      title ? createElement("div", { className: "romm-panel-section-title" }, title) : null,
      ...children.filter(Boolean),
    );

  // --- Loading state ---
  // Use minHeight so Steam's scroll container allocates enough space
  // before async data loads and expands the panel.
  if (state.loading) {
    return createElement("div", {
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

  const meta = state.metadata;

  // --- Game Info section ---
  const gameInfoChildren: ReturnType<typeof createElement>[] = [];

  if (meta) {
    if (meta.summary) {
      gameInfoChildren.push(
        createElement("div", { key: "summary", className: "romm-panel-summary" }, meta.summary),
      );
    }

    // Platform after description
    if (state.platformName) {
      gameInfoChildren.push(infoRow("platform", "Platform", state.platformName));
    }

    if (meta.companies && meta.companies.length > 0) {
      gameInfoChildren.push(infoRow("companies", "Developer / Publisher", meta.companies.join(", ")));
    }

    if (meta.genres && meta.genres.length > 0) {
      gameInfoChildren.push(
        createElement("div", { key: "genres", className: "romm-panel-info-row" },
          createElement("span", { className: "romm-panel-label" }, "Genres"),
          createElement("div", { className: "romm-panel-tags" },
            ...meta.genres.map((g) =>
              createElement("span", { key: g, className: "romm-panel-tag" }, g),
            ),
          ),
        ),
      );
    }

    const releaseDate = formatReleaseDate(meta.first_release_date);
    if (releaseDate) {
      gameInfoChildren.push(infoRow("release-date", "Release Date", releaseDate));
    }

    if (meta.game_modes && meta.game_modes.length > 0) {
      gameInfoChildren.push(infoRow("game-modes", "Game Modes", meta.game_modes.join(", ")));
    }

    if (meta.player_count) {
      gameInfoChildren.push(infoRow("players", "Players", meta.player_count));
    }

    if (meta.average_rating != null && meta.average_rating > 0) {
      gameInfoChildren.push(infoRow("rating", "Rating", `${Math.round(meta.average_rating)}%`));
    }
  } else {
    // No metadata — still show platform
    if (state.platformName) {
      gameInfoChildren.push(infoRow("platform", "Platform", state.platformName));
    }
  }

  const gameInfoContent = gameInfoChildren.length > 0
    ? gameInfoChildren
    : [createElement("div", { key: "no-meta", className: "romm-panel-muted" }, "No metadata available")];

  const gameInfoSection = state.coverBase64
    ? section("game-info", "Game Info",
        createElement("div", {
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
    : section("game-info", "Game Info", ...gameInfoContent);

  // --- ROM File section (only when installed) ---
  const romFileSection = state.installed && state.installedRom
    ? section("rom-file", "ROM File",
        infoRow("filename", "Filename", state.installedRom.file_name),
      )
    : null;

  // --- BIOS & Core section (two-column layout when platform needs BIOS) ---
  let biosSection: ReturnType<typeof createElement> | null = null;
  if (state.biosStatus) {
    const bios = state.biosStatus;
    const localCount = bios.local_count ?? 0;
    const serverCount = bios.server_count ?? 0;
    const reqCount = bios.required_count;
    const reqDone = bios.required_downloaded;

    let biosColor: string;
    let biosLabel: string;
    if (reqCount != null && reqDone != null) {
      biosColor = reqDone >= reqCount ? "#5ba32b" : reqDone > 0 ? "#d4a72c" : "#d94126";
      biosLabel = reqDone >= reqCount
        ? `All required ready (${localCount}/${serverCount})`
        : `${reqDone}/${reqCount} required files ready`;
    } else {
      biosColor = bios.all_downloaded ? "#5ba32b" : localCount > 0 ? "#d4a72c" : "#d94126";
      biosLabel = bios.all_downloaded
        ? `All ready (${localCount}/${serverCount})`
        : `${localCount}/${serverCount} files ready`;
    }

    // Left column: BIOS status + file list
    const biosColumn: (ReturnType<typeof createElement> | null)[] = [];

    biosColumn.push(
      createElement("div", { key: "bios-title", className: "romm-panel-section-title", style: { marginBottom: "8px" } }, "BIOS"),
    );

    biosColumn.push(
      createElement("div", {
        key: "bios-row",
        className: "romm-panel-status-inline",
      },
        createElement("span", {
          className: "romm-status-dot",
          style: { backgroundColor: biosColor },
        }),
        createElement("span", { className: "romm-panel-value" }, biosLabel),
      ),
    );

    // Build core_so -> label lookup from available_cores
    const coreLabelMap: Record<string, string> = {};
    if (bios.available_cores) {
      for (const c of bios.available_cores) {
        coreLabelMap[c.core_so] = c.label;
      }
    }

    // Filter out unknown files (not in registry) — they're noise from the server
    const knownFiles = (bios.files ?? []).filter((f) => f.classification !== "unknown");
    const unknownCount = (bios.files ?? []).length - knownFiles.length;

    if (knownFiles.length > 0) {
      const fileElements = knownFiles.map((f) => {
        // Dot color logic:
        // Green: downloaded
        // Red: missing + required by current core
        // Orange: missing + required by another core (not current)
        // Grey: optional for current core or not used by any known core
        let dotColor: string;
        if (f.downloaded) {
          dotColor = "#5ba32b";
        } else if (f.used_by_active !== false && f.classification === "required") {
          dotColor = "#d94126";
        } else if (!f.used_by_active && f.cores) {
          const requiredByOther = Object.values(f.cores).some((c) => c.required);
          dotColor = requiredByOther ? "#d4a72c" : "#8f98a0";
        } else {
          dotColor = "#8f98a0";
        }

        // Build per-core lines
        const coreLines: ReturnType<typeof createElement>[] = [];
        if (f.cores) {
          for (const [coreSo, coreData] of Object.entries(f.cores)) {
            const label = coreLabelMap[coreSo] || coreSo.replace(/_libretro$/, "");
            const suffix = coreData.required ? " (required)" : " (optional)";
            coreLines.push(
              createElement("div", {
                key: `core-${coreSo}`,
                style: { color: "rgba(255, 255, 255, 0.5)", fontSize: "12px" },
              }, `${label}${suffix}`),
            );
          }
        }

        return createElement("div", { key: f.file_name, className: "romm-panel-file-row" },
          createElement("span", {
            key: "dot",
            className: "romm-status-dot",
            style: { backgroundColor: dotColor },
          }),
          createElement("span", { key: "name", className: "romm-panel-file-name" },
            f.description || f.file_name,
          ),
          coreLines.length > 0
            ? createElement("div", {
                key: "cores",
                style: { flexBasis: "100%", display: "flex", flexDirection: "column" as const, gap: "2px", marginLeft: "18px" },
              }, ...coreLines)
            : null,
        );
      });

      // Add unknown count note if any
      if (unknownCount > 0) {
        fileElements.push(
          createElement("div", {
            key: "unknown-note",
            className: "romm-panel-file-row",
            style: { color: "rgba(255, 255, 255, 0.4)", fontSize: "12px", marginTop: "8px" },
          }, `+ ${unknownCount} other file${unknownCount !== 1 ? "s" : ""} on server (not required by any known core)`),
        );
      }

      biosColumn.push(
        createElement("div", { key: "bios-file-list", className: "romm-panel-file-list" },
          ...fileElements,
        ),
      );
    }

    // Right column: Core info
    const coreColumn: (ReturnType<typeof createElement> | null)[] = [];

    coreColumn.push(
      createElement("div", { key: "core-title", className: "romm-panel-section-title", style: { marginBottom: "8px" } }, "Emulator"),
    );

    if (bios.active_core_label) {
      coreColumn.push(infoRow("core", "Active Core", bios.active_core_label));
    } else {
      coreColumn.push(infoRow("core", "Active Core", "Default"));
    }

    biosSection = section("bios-core", null,
      createElement("div", {
        key: "bios-core-columns",
        style: { display: "flex", gap: "24px" },
      },
        createElement("div", { key: "bios-col", style: { flex: 1, minWidth: 0 } }, ...biosColumn.filter(Boolean)),
        createElement("div", { key: "core-col", style: { flexShrink: 0, minWidth: "120px" } }, ...coreColumn.filter(Boolean)),
      ),
    );
  }

  // --- Tab bar ---
  const tabs: { id: string; label: string; visible: boolean }[] = [
    { id: "info", label: "GAME INFO", visible: true },
    { id: "achievements", label: "ACHIEVEMENTS", visible: !!state.raId },
    { id: "saves", label: "SAVES", visible: state.saveSyncEnabled },
    { id: "bios", label: "BIOS", visible: !!state.biosStatus },
  ];

  const tabBar = createElement(Focusable as any, {
    className: "romm-tab-bar",
    "flow-children": "right",
    "data-romm": "true",
  },
    ...tabs.filter((t) => t.visible).map((t) =>
      createElement(DialogButton as any, {
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
      }, t.label),
    ),
  );

  // --- Achievements tab content ---
  let achievementsContent: ReturnType<typeof createElement> | null = null;
  if (state.activeTab === "achievements") {
    if (state.achievementsLoading) {
      achievementsContent = createElement("div", { className: "romm-panel-loading" }, "Loading achievements...");
    } else if (state.achievements.length === 0) {
      achievementsContent = createElement("div", { className: "romm-panel-muted" }, "No achievements found for this game");
    } else {
      const progress = state.achievementProgress;
      const earned = progress?.earned ?? 0;
      const total = progress?.total ?? state.achievements.length;

      // Build map from badge_id -> earned data (id in earned_achievements is badge_id)
      const earnedMap = new Map<string, EarnedAchievement>();
      for (const ea of (progress?.earned_achievements ?? [])) {
        earnedMap.set(ea.id, ea);
      }

      // Sort: earned first, then by display_order
      const sorted = [...state.achievements].sort((a, b) => {
        const aEarned = earnedMap.has(a.badge_id) ? 0 : 1;
        const bEarned = earnedMap.has(b.badge_id) ? 0 : 1;
        if (aEarned !== bEarned) return aEarned - bEarned;
        return (a.display_order || 0) - (b.display_order || 0);
      });

      const earnedList = sorted.filter((a) => earnedMap.has(a.badge_id));
      const lockedList = sorted.filter((a) => !earnedMap.has(a.badge_id));

      const formatCheevoDate = (dateStr: string) => {
        // "2025-02-14 15:45:38" -> "2025-02-14 15:45"
        return dateStr.replace(/:\d{2}$/, "");
      };

      // Generate unique sparkle positions per achievement using a simple seed hash
      const makeHcSparkles = (seed: number) => {
        // Simple deterministic pseudo-random from seed
        const rng = (i: number) => {
          let x = Math.sin(seed * 9301 + i * 4973) * 49297;
          return x - Math.floor(x);
        };
        // 4 sparkles, positions along edges/corners with some spread outside
        return Array.from({ length: 4 }, (_, i) => ({
          top: `${Math.round(rng(i * 3) * 100)}%`,
          left: `${Math.round(rng(i * 3 + 1) * 100)}%`,
          dur: 2.2 + rng(i * 3 + 2) * 1.8, // 2.2–4.0s
          delay: rng(i * 7 + 5) * 2.0,      // 0–2.0s
        }));
      };

      const renderCheevoRow = (a: Achievement) => {
        const earnedData = earnedMap.get(a.badge_id);
        const isEarned = !!earnedData;
        const isHardcore = !!(earnedData?.date_hardcore);

        const rowClasses = [
          "romm-cheevo-row",
          isEarned ? "romm-cheevo-row-earned" : "",
        ].filter(Boolean).join(" ");

        const imgClasses = [
          "romm-cheevo-badge-img",
          isHardcore ? "romm-cheevo-badge-img-hc" : "",
        ].filter(Boolean).join(" ");

        // Date column for earned achievements — show both normal and HC dates
        const dateChildren: ReturnType<typeof createElement>[] = [];
        if (earnedData?.date) {
          dateChildren.push(
            createElement("span", { key: "date", className: "romm-cheevo-date" },
              formatCheevoDate(earnedData.date)),
          );
        }
        if (isHardcore && earnedData?.date_hardcore) {
          dateChildren.push(
            createElement("span", {
              key: "hc-row",
              style: { display: "inline-flex", alignItems: "center", gap: "4px" },
            },
              createElement("span", { className: "romm-cheevo-hc-badge" }, "HC"),
              createElement("span", { className: "romm-cheevo-date" },
                formatCheevoDate(earnedData.date_hardcore)),
            ),
          );
        }

        // Badge image — wrapped with sparkle container for HC achievements
        const imgEl = createElement("img", {
          className: imgClasses,
          src: isEarned ? a.badge_url : (a.badge_url_lock || a.badge_url),
          style: isEarned ? {} : { filter: "grayscale(0.7) opacity(0.6)" },
        });

        const badgeElement = isHardcore
          ? createElement("div", { className: "romm-cheevo-img-wrap" },
              imgEl,
              createElement("span", { className: "romm-cheevo-img-sparkles" },
                ...makeHcSparkles(a.ra_id).map((sp, i) =>
                  createElement("span", {
                    key: `hc-sp-${i}`,
                    className: "romm-cheevo-img-sparkle-dot",
                    style: {
                      "--romm-sparkle-top": sp.top,
                      "--romm-sparkle-left": sp.left,
                      "--romm-sparkle-delay": `${sp.delay.toFixed(1)}s`,
                      "--romm-sparkle-dur": `${sp.dur.toFixed(1)}s`,
                    } as any,
                  }),
                ),
              ),
            )
          : imgEl;

        return createElement(DialogButton as any, {
          key: `cheevo-${a.ra_id}`,
          className: rowClasses,
          noFocusRing: false,
          onFocus: scrollFocusedToCenter,
          style: {
            background: "transparent",
            border: "none",
            padding: 0,
            textAlign: "left" as const,
            cursor: "default",
            display: "flex",
            alignItems: "center",
            gap: "12px",
          },
        },
          badgeElement,
          createElement("div", { className: "romm-cheevo-details" },
            createElement("div", { className: "romm-cheevo-title" }, a.title),
            createElement("div", { className: "romm-cheevo-desc" }, a.description),
            a.num_awarded > 0
              ? createElement("div", { className: "romm-cheevo-rarity" },
                  `${a.num_awarded} players earned this`)
              : null,
          ),
          dateChildren.length > 0
            ? createElement("div", { className: "romm-cheevo-dates" }, ...dateChildren)
            : null,
          createElement("div", {
            className: `romm-cheevo-points ${isEarned ? "" : "romm-cheevo-points-locked"}`,
          }, `${a.points} pts`),
        );
      };

      const cheevoChildren: ReturnType<typeof createElement>[] = [];

      // Summary bar
      cheevoChildren.push(
        createElement("div", { key: "summary", className: "romm-cheevo-summary" },
          createElement("span", { className: "romm-cheevo-summary-text" },
            `${earned} / ${total} Achievements`),
          progress?.earned_hardcore
            ? createElement("span", { className: "romm-cheevo-summary-sub" },
                `${progress.earned_hardcore} hardcore`)
            : null,
        ),
      );

      // Progress bar
      const pct = total > 0 ? (earned / total) * 100 : 0;
      cheevoChildren.push(
        createElement("div", { key: "progress-bar", className: "romm-cheevo-progress-bar" },
          createElement("div", {
            className: "romm-cheevo-progress-fill",
            style: { width: `${pct}%` },
          }),
        ),
      );

      // Earned section
      if (earnedList.length > 0) {
        cheevoChildren.push(
          createElement("div", { key: "earned-title", className: "romm-cheevo-section-title" },
            `Earned (${earnedList.length})`),
        );
        earnedList.forEach((a) => cheevoChildren.push(renderCheevoRow(a)));
      }

      // Locked section
      if (lockedList.length > 0) {
        cheevoChildren.push(
          createElement("div", { key: "locked-title", className: "romm-cheevo-section-title" },
            `Locked (${lockedList.length})`),
        );
        lockedList.forEach((a) => cheevoChildren.push(renderCheevoRow(a)));
      }

      achievementsContent = createElement("div", { className: "romm-cheevo-list" }, ...cheevoChildren);
    }
  }

  // --- Migration warning (when path change pending) ---
  const migrationWarning = migrationPending
    ? createElement("div", {
        key: "migration-warning",
        style: {
          padding: "8px 12px",
          marginBottom: "12px",
          backgroundColor: "rgba(212, 167, 44, 0.15)",
          borderLeft: "3px solid #d4a72c",
          borderRadius: "4px",
        },
      },
        createElement("div", {
          style: { fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" },
        }, "\u26A0\uFE0F RetroDECK location changed"),
        createElement("div", {
          style: { fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" },
        }, "File paths may be incorrect. Go to Settings to migrate files."),
      )
    : null;

  const saveSortWarning = saveSortPending
    ? createElement("div", {
        key: "save-sort-warning",
        style: {
          padding: "8px 12px",
          marginBottom: "12px",
          backgroundColor: "rgba(212, 167, 44, 0.15)",
          borderLeft: "3px solid #d4a72c",
          borderRadius: "4px",
        },
      },
        createElement("div", {
          style: { fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" },
        }, "\u26A0\uFE0F RetroArch save sorting changed"),
        createElement("div", {
          style: { fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" },
        }, "Save file paths may be incorrect. Go to Settings to migrate."),
      )
    : null;

  // --- Determine active tab content ---
  let activeTabContent: ReturnType<typeof createElement> | null = null;
  if (state.activeTab === "info") {
    activeTabContent = createElement("div", { key: "tab-info" },
      gameInfoSection,
      romFileSection,
    );
  } else if (state.activeTab === "achievements") {
    // Don't wrap in section() — that creates ONE giant focusable element.
    // Individual rows are now DialogButtons, enabling focus-driven scrolling.
    activeTabContent = achievementsContent;
  } else if (state.activeTab === "saves") {
    if (state.saveSyncEnabled && !state.slotConfirmed) {
      activeTabContent = createElement(SlotSetupWizard, {
        romId: state.romId,
        onComplete: () => {
          // Refresh: mark as configured and reload save status
          setState((prev) => ({ ...prev, slotConfirmed: true }));
          globalThis.dispatchEvent(new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: state.romId },
          }));
        },
      } as any);
    } else {
      activeTabContent = createElement(SavesTab, {
        romId: state.romId!,
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
          globalThis.dispatchEvent(new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: state.romId },
          }));
        },
      });
    }
  } else if (state.activeTab === "bios") {
    activeTabContent = biosSection;
  }

  return createElement("div", { "data-romm": "true" },
    migrationWarning,
    saveSortWarning,
    tabBar,
    createElement(Focusable as any, {
      noFocusRing: true,
      className: "romm-tab-content",
      style: { paddingBottom: "48px" },
    },
      activeTabContent,
    ),
  );
};
