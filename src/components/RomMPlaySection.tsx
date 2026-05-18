/**
 * RomMPlaySection — wraps CustomPlayButton and adds info items to its right,
 * mimicking Steam's native PlaySection layout:
 *
 *   [▶ Play ▾]   LAST PLAYED    PLAYTIME    ACHIEVEMENTS    SAVE SYNC    BIOS
 *                24. Jan.       14 Hours    To be impl.     ✅ 2h ago    🟢 OK
 *
 * Uses our own romm-play-section-row CSS class on the root.
 * Individual info items use our own romm-info-* CSS classes.
 * Save Sync and BIOS items only appear when relevant.
 */

import { useState, useEffect, useRef, FC, createElement } from "react";
import { toaster } from "@decky/api";
import {
  basicAppDetailsSectionStylerClasses,
  ConfirmModal,
  DialogButton,
  Focusable,
  Menu,
  MenuItem,
  MenuSeparator,
  showContextMenu,
  showModal,
} from "@decky/ui";
import { FaGamepad, FaCog, FaMicrochip, FaExclamationTriangle } from "react-icons/fa";
import { CustomPlayButton } from "./CustomPlayButton";
import { hasAnySaveConflict } from "../utils/saveStatus";
import { scrollToTop } from "../utils/scrollHelpers";
import { getEventTarget } from "../utils/events";
import {
  getCachedGameDetail,
  invalidateCachedGameDetail,
  testConnection,
  getSaveStatus,
  getBiosStatus,
  getSgdbArtworkBase64,
  getRomMetadata,
  removeRom,
  downloadAllFirmware,
  syncRomSaves,
  deleteLocalSaves,
  saveShortcutIcon,
  setGameCore,
  debugLog,
} from "../api/backend";
import type { AvailableCore, BiosStatus, SaveStatus } from "../types";
import type { RommDataChangedDetail } from "../types/events";
import { formatLastPlayed, formatPlaytime } from "../utils/formatters";
import {
  applySaveSyncDisplay,
  extractBiosInfo,
  resolveSaveSyncLabel,
  timeoutMs,
} from "../utils/playSection";
import {
  refreshAchievementsInBackground,
  refreshActiveSlotInBackground,
  refreshBiosInBackground,
} from "../utils/sectionRefresh";

/** Track which appIds have had auto-artwork applied this session */
const artworkApplied = new Set<number>();

/** Fetch SGDB artwork (hero, logo, wide grid, icon) and apply to Steam.
 *  Returns count of successfully applied images. */
async function applyArtwork(romId: number, appId: number): Promise<number> {
  const results = await Promise.all([
    getSgdbArtworkBase64(romId, 1).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 2).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 3).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 4).catch(() => ({ base64: null, no_api_key: false })),
  ]);

  if (results.some((r) => r.no_api_key)) return -1;

  let applied = 0;
  // SGDB type 1 = hero → Steam assetType 1
  if (results[0].base64) {
    await SteamClient.Apps.SetCustomArtworkForApp(appId, results[0].base64, "png", 1);
    applied++;
  }
  // SGDB type 2 = logo → Steam assetType 2
  if (results[1].base64) {
    await SteamClient.Apps.SetCustomArtworkForApp(appId, results[1].base64, "png", 2);
    applied++;
  }
  // SGDB type 3 = wide grid → Steam assetType 3
  if (results[2].base64) {
    await SteamClient.Apps.SetCustomArtworkForApp(appId, results[2].base64, "png", 3);
    applied++;
  }
  // Type 4 = icon (VDF-based)
  if (results[3].base64) {
    await saveShortcutIcon(appId, results[3].base64);
    applied++;
  }

  return applied;
}

interface RomMPlaySectionProps {
  appId: number;
}

type ConnectionState = "checking" | "connected" | "offline";

interface InfoState {
  romId: number | null;
  romName: string;
  platformSlug: string;
  romFile: string;
  lastPlayed: string;
  playtime: string;
  saveSyncEnabled: boolean;
  saveSyncStatus: "synced" | "conflict" | "none" | null;
  saveSyncLabel: string;
  biosNeeded: boolean;
  biosStatus: "ok" | "partial" | "missing" | null; // NOSONAR(typescript:S4323) — inline union inside InfoState; extracting an alias adds indirection for no reuse benefit.
  biosLabel: string;
  activeCoreLabel: string | null;
  activeCoreIsDefault: boolean;
  availableCores: Array<{ core_so: string; label: string; is_default: boolean }>;
  activeSlot: string | null;
  raId: number | null;
  achievementEarned: number;
  achievementTotal: number;
}

import { setRommConnectionState, setVersionError } from "../utils/connectionState";
import { useVersionError } from "./VersionErrorCard";
import { useMigrationStatus } from "./MigrationBlockedPage";

/** Cache-first initial render. Resolves the cached game detail for this appId,
 *  pushes it into InfoState, and fires the background refresh tasks (active
 *  slot, artwork, metadata, achievements, BIOS) whose results are merged in
 *  later. Module-scope so the FC body stays focused on rendering. */
async function loadCached(
  appId: number,
  cancelled: () => boolean,
  romIdRef: React.MutableRefObject<number | null>,
  setter: React.Dispatch<React.SetStateAction<InfoState>>,
) {
  try {
    const cached = await getCachedGameDetail(appId);
    if (cancelled() || !cached.found) return;

    const romId = cached.rom_id!;
    romIdRef.current = romId;

    // Process save sync from backend-computed display fields
    let saveSyncStatus: "synced" | "conflict" | "none" | null = null;
    let saveSyncLabel = "";
    if (cached.save_sync_enabled && cached.save_sync_display) {
      saveSyncStatus = cached.save_sync_display.status;
      saveSyncLabel = resolveSaveSyncLabel(cached.save_sync_display);
    }

    if (cancelled()) return;
    setter((prev) => ({
      ...prev,
      romId,
      romName: cached.rom_name || "",
      platformSlug: cached.platform_slug || "",
      romFile: cached.rom_file || "",
      saveSyncEnabled: cached.save_sync_enabled ?? false,
      saveSyncStatus,
      saveSyncLabel,
      raId: cached.ra_id ?? null,
      achievementEarned: cached.achievement_summary?.earned ?? 0,
      achievementTotal: cached.achievement_summary?.total ?? 0,
    }));

    // Background: fetch active_slot from save status (not in cached data)
    if (cached.save_sync_enabled) {
      refreshActiveSlotInBackground(romId, cancelled, setter);
    }

    // Auto-apply SGDB artwork on first visit (fire-and-forget)
    // Only mark as applied after success so transient failures allow retry on next visit
    if (!artworkApplied.has(appId)) {
      applyArtwork(romId, appId)
        .then(() => { artworkApplied.add(appId); })
        .catch((e) => debugLog(`Auto-artwork error: ${e}`));
    }

    const staleFields = cached.stale_fields ?? [];

    // Background: fetch metadata if stale
    if (romId && staleFields.includes("metadata")) {
      getRomMetadata(romId).catch((e) => debugLog(`Background metadata fetch error: ${e}`));
    }

    // Achievements: render from cache, background refresh if stale
    if (cached.ra_id && staleFields.includes("achievements")) {
      refreshAchievementsInBackground(romId, cancelled, setter);
    }

    // BIOS: render from cache first, background refresh if stale
    const cachedBios = cached.bios_status;
    if (cachedBios) {
      setter((prev) => ({
        ...prev,
        ...extractBiosInfo(cachedBios as BiosStatus, cached.bios_level ?? null, cached.bios_label ?? null),
      }));
    }

    if (staleFields.includes("bios")) {
      refreshBiosInBackground(romId, cancelled(), setter);
    }
  } catch (e) {
    debugLog(`RomMPlaySection: loadCached error: ${e}`);
  }
}

export const RomMPlaySection: FC<RomMPlaySectionProps> = ({ appId }) => { // NOSONAR(typescript:S3776) — React FC body; decomposed in #392. Holds Steam menu + achievements + save-sync row.
  // Subscribe to version error — re-renders when global state changes
  const versionError = useVersionError();
  const migration = useMigrationStatus();

  // Read playtime from Steam's own overview synchronously (already written by metadataPatches)
  // This avoids an unnecessary render from setting it inside the async effect.
  const overview = appStore.GetAppOverviewByAppID(appId);
  const initialLastPlayed = formatLastPlayed(overview?.rt_last_time_played ?? 0);
  const initialPlaytime = formatPlaytime(overview?.minutes_playtime_forever ?? 0);

  const [info, setInfo] = useState<InfoState>({
    romId: null,
    romName: "",
    platformSlug: "",
    romFile: "",
    lastPlayed: initialLastPlayed,
    playtime: initialPlaytime,
    saveSyncEnabled: false,
    saveSyncStatus: null,
    saveSyncLabel: "",
    biosNeeded: false,
    biosStatus: null,
    biosLabel: "",
    activeCoreLabel: null,
    activeCoreIsDefault: true,
    availableCores: [],
    activeSlot: "default",
    raId: null,
    achievementEarned: 0,
    achievementTotal: 0,
  });
  const [connectionState, setConnectionState] = useState<ConnectionState>("checking");
  const [actionPending, setActionPending] = useState<string | null>(null);
  const romIdRef = useRef<number | null>(null);

  // Cache-first load: render instantly from cached data, then check connection in background
  useEffect(() => {
    let cancelled = false;

    loadCached(appId, () => cancelled, romIdRef, setInfo);

    // Per-event-type handlers — each owns one branch of the data-changed dispatch.
    // Defined inside useEffect to share the cancelled/romIdRef/setInfo closure.
    const handleSaveSyncSettingsChange = async (detail: Extract<RommDataChangedDetail, { type: "save_sync_settings" }>) => {
      const enabled = detail.save_sync_enabled;
      if (enabled) {
        const rid = romIdRef.current;
        if (rid) {
          const saveStatus = await getSaveStatus(rid).catch((): SaveStatus | null => null);
          const { status: ss, label: sl } = applySaveSyncDisplay(saveStatus?.save_sync_display, saveStatus);
          setInfo((prev) => ({ ...prev, saveSyncEnabled: true, saveSyncStatus: ss, saveSyncLabel: sl }));
        } else {
          setInfo((prev) => ({ ...prev, saveSyncEnabled: true }));
        }
      } else {
        setInfo((prev) => ({ ...prev, saveSyncEnabled: false, saveSyncStatus: null, saveSyncLabel: "" }));
      }
    };

    const handleCoreChange = async () => {
      const rid = romIdRef.current;
      if (!rid) return;
      const result = await getBiosStatus(rid);
      if (cancelled) return;
      const b = result.bios_status;
      if (!b) return;
      const activeCoreLabel = b.active_core_label ?? null;
      const availableCores = b.available_cores ?? [];
      const defaultCore = availableCores.find((c) => c.is_default);
      const activeCoreIsDefault = !activeCoreLabel || activeCoreLabel === defaultCore?.label;
      setInfo((prev) => ({
        ...prev,
        activeCoreLabel,
        activeCoreIsDefault,
        availableCores,
        biosStatus: result.bios_level,
        biosLabel: result.bios_label ?? "",
      }));
    };

    const handleSaveSyncChange = async (detail: Extract<RommDataChangedDetail, { type: "save_sync" }>) => {
      const romId = romIdRef.current ?? detail.rom_id;
      if (!romId) return;
      // If event specifies a rom_id, skip if it's not for this game
      if (detail.rom_id && romIdRef.current && detail.rom_id !== romIdRef.current) return;
      const saveStatus: SaveStatus | null = detail.save_status ?? await getSaveStatus(romId).catch((): SaveStatus | null => null);
      const { status: saveSyncStatus, label: saveSyncLabel } = applySaveSyncDisplay(saveStatus?.save_sync_display, saveStatus);
      setInfo((prev) => ({ ...prev, saveSyncStatus, saveSyncLabel, activeSlot: saveStatus && "active_slot" in saveStatus ? saveStatus.active_slot ?? null : prev.activeSlot }));
    };

    const onDataChanged = async (e: Event) => {
      try {
        const detail = (e as CustomEvent).detail;
        switch (detail?.type) {
          case "save_sync_settings": await handleSaveSyncSettingsChange(detail); break;
          case "core_changed": await handleCoreChange(); break;
          case "save_sync": await handleSaveSyncChange(detail); break;
        }
      } catch (err) {
        debugLog(`RomMPlaySection: onDataChanged error: ${err}`);
      }
    };
    globalThis.addEventListener("romm_data_changed", onDataChanged);

    return () => {
      cancelled = true;
      globalThis.removeEventListener("romm_data_changed", onDataChanged);
    };
  }, [appId]);

  // Background connection check — runs after initial cached render
  // If connected + installed + save sync enabled, also runs background save status check
  useEffect(() => {
    let cancelled = false;

    async function doSaveCheck(isCancelled: boolean) {
      const romId = romIdRef.current;
      if (!romId || !info.saveSyncEnabled) return;
      try {
        const saveStatus = await getSaveStatus(romId);
        if (isCancelled) return;
        const hasConflict = hasAnySaveConflict(saveStatus);
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", {
          detail: { type: "save_sync", rom_id: romId, has_conflict: hasConflict },
        }));
        const { status: ss, label: sl } = applySaveSyncDisplay(saveStatus?.save_sync_display, saveStatus);
        setInfo((prev) => ({ ...prev, saveSyncStatus: ss, saveSyncLabel: sl, activeSlot: saveStatus && "active_slot" in saveStatus ? saveStatus.active_slot ?? null : prev.activeSlot }));
      } catch (e) {
        debugLog(`RomMPlaySection: background save check error: ${e}`);
      }
    }

    const check = async () => {
      // Reset stale connection state immediately so downstream consumers
      // (e.g. CustomPlayButton) don't stay stuck on a previous "offline"
      setRommConnectionState("checking");
      globalThis.dispatchEvent(new CustomEvent("romm_connection_changed", { detail: { state: "checking" } }));

      try {
        const result = await Promise.race([testConnection(), timeoutMs(5000)]);
        if (cancelled) return;
        if (result.error_code === "version_error") {
          setVersionError(result.message);
          setRommConnectionState("offline");
          setConnectionState("offline");
          globalThis.dispatchEvent(new CustomEvent("romm_connection_changed", { detail: { state: "offline" } }));
          return;
        }
        const connected = result.success;
        const connState = connected ? "connected" : "offline";
        setRommConnectionState(connState);
        setConnectionState(connState);
        globalThis.dispatchEvent(new CustomEvent("romm_connection_changed", { detail: { state: connState } }));

        // If connected, do background save status check to detect new conflicts
        if (connected) await doSaveCheck(cancelled);
      } catch {
        if (!cancelled) {
          setRommConnectionState("offline");
          setConnectionState("offline");
          globalThis.dispatchEvent(new CustomEvent("romm_connection_changed", { detail: { state: "offline" } }));
        }
      }
    };
    check();
    return () => { cancelled = true; };
  }, [info.saveSyncEnabled]);

  // Helper: create an info item with header and value (Steam's two-line pattern)
  const infoItem = (key: string, header: string, value: string, extraClass?: string) =>
    createElement("div", {
      key,
      className: `romm-info-item ${extraClass || ""}`.trim(),
    },
      createElement("div", { className: "romm-info-header" }, header),
      createElement("div", { className: "romm-info-value" }, value),
    );


  // --- Gear button action handlers ---

  const handleRefreshArtwork = async () => {
    if (actionPending) return;
    if (!info.romId) {
      toaster.toast({ title: "RomM Sync", body: "ROM info not loaded yet" });
      return;
    }
    setActionPending("artwork");
    try {
      const applied = await applyArtwork(info.romId, appId);
      if (applied === -1) {
        toaster.toast({ title: "RomM Sync", body: "Set a SteamGridDB API key in settings first" });
      } else if (applied > 0) {
        toaster.toast({ title: "RomM Sync", body: `Artwork refreshed (${applied}/4 images applied)` });
      } else {
        toaster.toast({ title: "RomM Sync", body: "No artwork found" });
      }
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Failed to refresh artwork" });
    } finally {
      setActionPending(null);
    }
  };

  const handleRefreshMetadata = async () => {
    if (actionPending || !info.romId) return;
    setActionPending("metadata");
    try {
      await getRomMetadata(info.romId);
      toaster.toast({ title: "RomM Sync", body: "Metadata refreshed" });
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "metadata", rom_id: info.romId } }));
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Failed to refresh metadata" });
    } finally {
      setActionPending(null);
    }
  };

  const handleSyncSaves = async () => {
    if (actionPending || !info.romId) return;
    setActionPending("savesync");
    try {
      const result = await syncRomSaves(info.romId);
      if (result.success) {
        const n = result.synced ?? 0;
        const c = result.conflicts?.length ?? 0;
        let label: string;
        if (n === 0) {
          label = "no files updated";
        } else if (n === 1) {
          label = "1 file updated";
        } else {
          label = `${n} files updated`;
        }
        if (c > 0) label += `, ${c} conflict(s) need resolution`;
        toaster.toast({ title: "RomM Sync", body: `Saves synced (${label})` });
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: info.romId } }));
        // Refresh save sync status — last_sync_check_at was just set by the backend
        setInfo((prev) => ({ ...prev, saveSyncStatus: "synced" as const, saveSyncLabel: "Just now" }));
      } else {
        toaster.toast({ title: "RomM Sync", body: result.message || "Save sync failed" });
      }
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Save sync failed" });
    } finally {
      setActionPending(null);
    }
  };

  const handleDownloadBios = async () => {
    if (actionPending || !info.platformSlug) return;
    setActionPending("bios");
    try {
      const result = await downloadAllFirmware(info.platformSlug);
      if (result.success) {
        toaster.toast({ title: "RomM Sync", body: `BIOS downloaded (${result.downloaded ?? 0} files)` });
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "bios", platform_slug: info.platformSlug } }));
        // Refresh BIOS status — getBiosStatus ships pre-computed level/label so we don't re-derive.
        if (info.romId) {
          const refreshed = await getBiosStatus(info.romId).catch(() => ({
            bios_status: null as BiosStatus | null,
            bios_level: null as "ok" | "partial" | "missing" | null,
            bios_label: null as string | null,
          }));
          if (refreshed.bios_status) {
            setInfo((prev) => ({
              ...prev,
              biosStatus: refreshed.bios_level,
              biosLabel: refreshed.bios_label ?? "",
            }));
          }
        }
      } else {
        toaster.toast({ title: "RomM Sync", body: result.message || "BIOS download failed" });
      }
    } catch {
      toaster.toast({ title: "RomM Sync", body: "BIOS download failed" });
    } finally {
      setActionPending(null);
    }
  };

  const handleUninstall = async () => {
    if (actionPending || !info.romId) return;
    setActionPending("uninstall");
    try {
      const result = await removeRom(info.romId);
      if (result.success) {
        globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: info.romId } }));
        toaster.toast({ title: "RomM Sync", body: `${info.romName || "ROM"} uninstalled` });
      } else {
        toaster.toast({ title: "RomM Sync", body: result.message || "Uninstall failed" });
      }
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Uninstall failed" });
    } finally {
      setActionPending(null);
    }
  };

  const handleDeleteSaves = () => {
    if (actionPending || !info.romId) return;
    const romId = info.romId;
    showModal(
      createElement(ConfirmModal, {
        strTitle: "Delete Local Saves",
        strDescription: "This will delete local save files for this game. Make sure saves are synced to RomM first — the next sync will re-download them from the server.",
        strOKButtonText: "Delete",
        strCancelButtonText: "Cancel",
        onOK: async () => {
          setActionPending("deletesaves");
          try {
            const result = await deleteLocalSaves(romId);
            if (result.success) {
              toaster.toast({ title: "RomM Sync", body: result.message });
              // Directly update PlaySection status — no local saves remain
              setInfo((prev) => ({ ...prev, saveSyncStatus: "none" as const, saveSyncLabel: "No saves" }));
              globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
            } else {
              toaster.toast({ title: "RomM Sync", body: result.message || "Failed to delete saves" });
            }
          } catch {
            toaster.toast({ title: "RomM Sync", body: "Failed to delete saves" });
          } finally {
            setActionPending(null);
          }
        },
      }),
    );
  };

  const handleChangeGameCore = async (coreLabel: string) => {
    if (!info.platformSlug || !info.romFile) return;
    const romPath = `./${info.romFile}`;
    debugLog(`handleChangeGameCore: slug=${info.platformSlug} romPath=${romPath} coreLabel=${coreLabel}`);
    try {
      const result = await setGameCore(info.platformSlug, romPath, coreLabel);
      debugLog(`handleChangeGameCore: result=${JSON.stringify(result)}`);
      if (result.success) {
        toaster.toast({ title: "RomM Sync", body: `Core set to ${coreLabel}` });
        // Use bios_status from the set_game_core response directly (avoids cache staleness).
        // For pre-computed level/label, re-fetch via getBiosStatus which ships them.
        const bios = result.bios_status;
        debugLog(`handleChangeGameCore: bios active_core_label=${bios?.active_core_label}`);
        if (bios && info.romId) {
          const newLabel = bios.active_core_label ?? null;
          const cores = bios.available_cores ?? info.availableCores;
          const defaultC = cores.find((c: AvailableCore) => c.is_default);
          const refreshed = await getBiosStatus(info.romId).catch(() => ({
            bios_status: null as BiosStatus | null,
            bios_level: null as "ok" | "partial" | "missing" | null,
            bios_label: null as string | null,
          }));
          setInfo((prev) => ({
            ...prev,
            activeCoreLabel: newLabel,
            activeCoreIsDefault: !newLabel || (defaultC != null && newLabel === defaultC.label),
            availableCores: cores,
            biosStatus: refreshed.bios_level,
            biosLabel: refreshed.bios_label ?? "",
          }));
        }
        // Invalidate the frontend cache and notify other components (e.g. GameInfoPanel)
        invalidateCachedGameDetail(appId);
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "core_changed", platform_slug: info.platformSlug } }));
      } else {
        toaster.toast({ title: "RomM Sync", body: result.message || "Failed to set core" });
      }
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Failed to set core" });
    }
  };

  const showCoreMenu = (e: Event) => {
    showContextMenu(
      createElement(Menu, { label: "Emulator Core" },
        createElement(MenuItem, { key: "core-compat", disabled: true },
          "Switching cores may affect save compatibility",
        ),
        createElement(MenuItem, { key: "core-retrodeck-bug", disabled: true },
          "\u26a0 Per-game switch not applied (RetroDECK bug) — use QAM for system-wide",
        ),
        createElement(MenuSeparator, { key: "core-sep" }),
        ...info.availableCores.map((c) => {
          // Always send the core label — even for the default core.
          // Clearing the override (empty string) would fall back to the platform
          // override, not the ES-DE default, which is confusing.
          return createElement(MenuItem, {
            key: `core-${c.core_so}`,
            onClick: () => handleChangeGameCore(c.label),
          }, `${c.label}${c.is_default ? " (default)" : ""}${info.activeCoreLabel === c.label ? " \u2713" : ""}`);
        }),
      ),
      getEventTarget(e),
    );
  };

  const showRomMMenu = (e: Event) => {
    showContextMenu(
      createElement(Menu, { label: "RomM Actions" },
        createElement(MenuItem, { key: "refresh-artwork", onClick: handleRefreshArtwork }, "Refresh Artwork"),
        createElement(MenuItem, { key: "refresh-metadata", onClick: handleRefreshMetadata }, "Refresh Metadata"),
        createElement(MenuItem, { key: "sync-saves", onClick: handleSyncSaves }, "Sync Save Files"),
        createElement(MenuItem, { key: "download-bios", onClick: handleDownloadBios }, "Download BIOS"),
        createElement(MenuSeparator, { key: "sep" }),
        createElement(MenuItem, { key: "delete-saves", tone: "destructive", onClick: handleDeleteSaves }, "Delete Local Saves"),
        createElement(MenuItem, { key: "uninstall", tone: "destructive", onClick: handleUninstall }, "Uninstall"),
      ),
      getEventTarget(e),
    );
  };

  const showSteamMenu = (e: Event) => {
    showContextMenu(
      createElement(Menu, { label: "Steam" },
        createElement(MenuItem, { key: "properties", onClick: () => {
          SteamClient.Apps.OpenAppSettingsDialog(appId, "general");
        } }, "Properties"),
      ),
      getEventTarget(e),
    );
  };

  // Version mismatch — render nothing (VersionErrorCard is shown in RomMGameInfoPanel instead)
  if (versionError) {
    return null;
  }

  // Pending RetroDECK migration — render nothing (MigrationBlockedCard is shown in RomMGameInfoPanel instead)
  if (migration.pending) {
    return null;
  }

  // Build info items array
  const infoItems: ReturnType<typeof createElement>[] = [];

  // Offline indicator (first — most prominent)
  if (connectionState === "offline") {
    infoItems.push(
      createElement("div", {
        key: "offline-indicator",
        className: "romm-info-item",
      },
        createElement("div", { className: "romm-info-header" },
          createElement(FaExclamationTriangle, { size: 12, color: "#ff8800" }),
        ),
        createElement("div", {
          className: "romm-info-value",
          style: { color: "#ff8800" },
        }, "RomM offline"),
      ),
    );
  }

  // Last Played
  if (info.lastPlayed) {
    infoItems.push(infoItem("last-played", "LAST PLAYED", info.lastPlayed));
  }

  // Playtime
  if (info.playtime) {
    infoItems.push(infoItem("playtime", "PLAYTIME", info.playtime));
  }

  // Achievements badge (only when RA data available)
  if (info.raId) {
    const hasEarned = info.achievementEarned > 0;
    const countLabel = info.achievementTotal > 0
      ? `${info.achievementEarned}/${info.achievementTotal}`
      : `${info.achievementEarned}`;

    // Generate sparkle dots at random fixed positions (only when earned > 0)
    // Positions are deterministic per-index so they don't shift on re-render
    const sparklePositions = [
      { top: "5%", left: "80%" },
      { top: "70%", left: "10%" },
      { top: "15%", left: "35%" },
      { top: "85%", left: "70%" },
      { top: "45%", left: "90%" },
    ];
    const sparkleDurs = [2.4, 3.5, 2.8, 3.8, 3.1];
    const sparkleDelays = [0, 0.9, 0.3, 1.6, 1.1];
    const sparkleDots = hasEarned ? sparklePositions.map((pos, i) =>
      createElement("span", {
        key: `sparkle-${pos.top}-${pos.left}`,
        className: "romm-sparkle-dot",
        style: {
          "--romm-sparkle-top": pos.top,
          "--romm-sparkle-left": pos.left,
          "--romm-sparkle-delay": `${sparkleDelays[i]}s`,
          "--romm-sparkle-dur": `${sparkleDurs[i]}s`,
        } satisfies CSSPropertiesWithVars,
      }),
    ) : [];

    infoItems.push(
      createElement("div", {
        key: "achievements",
        className: "romm-info-item romm-cheevo-badge",
        onClick: () => {
          globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
        },
      },
        createElement("div", { className: "romm-info-header" }, "ACHIEVEMENTS"),
        createElement("div", {
          className: "romm-cheevo-badge-sparkle",
        },
          // Trophy icon with sparkle container
          createElement("span", { style: { position: "relative", display: "inline-block" } },
            createElement("span", {
              className: hasEarned ? "romm-cheevo-trophy" : "romm-cheevo-trophy-none",
            }, "\uD83C\uDFC6"),
            hasEarned ? createElement("span", { className: "romm-sparkle-container" }, ...sparkleDots) : null,
          ),
          createElement("span", { className: "romm-cheevo-count" }, countLabel),
        ),
      ),
    );
  }

  // Save Sync moved to dedicated tab — show legacy slot warning only
  if (info.activeSlot == null && info.saveSyncEnabled) {
    infoItems.push(
      createElement("div", {
        key: "legacy-slot-warning",
        className: "romm-info-item",
      },
        createElement("div", { className: "romm-info-header" }, "SAVE SYNC"),
        createElement("div", {
          style: { fontSize: "11px", color: "#ff8800", marginTop: "4px" },
        }, "\u26A0 Legacy save slot"),
      ),
    );
  }

  // BIOS warning (only when files are missing — OK status moved to tab)
  if (info.biosNeeded && info.biosStatus && info.biosStatus !== "ok") {
    const biosColor = info.biosStatus === "partial" ? "#d4a72c" : "#d94126";
    infoItems.push(
      createElement("div", {
        key: "bios",
        className: "romm-info-item",
        onClick: () => {
          globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        },
        style: { cursor: "pointer" },
      },
        createElement("div", { className: "romm-info-header" }, "BIOS"),
        createElement("div", {
          className: "romm-info-value",
          style: { display: "flex", alignItems: "center", gap: "6px" },
        },
          createElement("span", {
            className: "romm-status-dot",
            style: { backgroundColor: biosColor },
          }),
          info.biosLabel,
        ),
      ),
    );
  }

  return createElement(Focusable, {
    "data-romm": "true",
    className: `romm-play-section-row ${basicAppDetailsSectionStylerClasses?.PlaySection || ""}`.trim(),
    "flow-children": "right",
    style: {
      display: "flex",
      alignItems: "center",
      gap: "20px",
      padding: "16px 2.8vw",
      background: "rgba(14, 20, 27, 0.33)",
      boxSizing: "border-box",
    },
  },
    // Play button on the left
    createElement(CustomPlayButton, { appId }),
    // Info items row
    createElement("div", {
      className: "romm-info-items",
      style: {
        display: "flex",
        alignItems: "center",
        gap: "20px",
        flexWrap: "nowrap",
        overflow: "hidden",
      },
    },
      ...infoItems,
    ),
    // Gear icon buttons pushed to the far right
    createElement("div", {
      style: {
        marginLeft: "auto",
        display: "flex",
        alignItems: "center",
        gap: "8px",
        flexShrink: 0,
      },
    },
      // RomM actions button
      createElement(DialogButton, {
        className: "romm-gear-btn",
        onClick: showRomMMenu,
        onFocus: scrollToTop,
        title: "RomM Actions",
      },
        createElement(FaGamepad, { size: 18, color: "#553e98" }),
      ),
      // Core selection button (only when multiple cores available)
      ...(info.availableCores.length > 1 ? [
        createElement(DialogButton, {
          key: "core-btn",
          className: "romm-gear-btn",
          onClick: showCoreMenu,
          onFocus: scrollToTop,
          title: "Emulator Core",
        },
          createElement(FaMicrochip, { size: 18, color: info.activeCoreIsDefault ? "#8f98a0" : "#d4a72c" }),
        ),
      ] : []),
      // Steam properties button
      createElement(DialogButton, {
        className: "romm-gear-btn",
        onClick: showSteamMenu,
        onFocus: scrollToTop,
        title: "Steam Properties",
      },
        createElement(FaCog, { size: 18, color: "#8f98a0" }),
      ),
    ),
  );
};
