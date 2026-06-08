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
  ConfirmModal,
  DialogButton,
  Focusable,
  Menu,
  MenuItem,
  MenuSeparator,
  showContextMenu,
  showModal,
} from "@decky/ui";
import { basicAppDetailsSectionStylerClasses } from "../utils/deckyUiInternals";
import { FaGamepad, FaCog, FaMicrochip, FaExclamationTriangle } from "react-icons/fa";
import { CustomPlayButton } from "./CustomPlayButton";
import { SgdbGamePickerModalContent } from "./SgdbGamePickerModal";
import { applyArtwork } from "../utils/artwork";
import { hasAnySaveConflict } from "../utils/saveStatus";
import { scrollToTop } from "../utils/scrollHelpers";
import { getEventTarget } from "../utils/events";
import {
  getCachedGameDetail,
  invalidateCachedGameDetail,
  testConnection,
  getSaveStatus,
  getBiosStatus,
  getPlatformCoreInfo,
  getSgdbResolution,
  getRomMetadata,
  refreshCoverArtwork,
  removeRom,
  downloadAllFirmware,
  syncRomSaves,
  deleteLocalSaves,
  setGameCore,
  clearGameCore,
  reconcilePlaytime,
  debugLog,
} from "../api/backend";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import type { BiosStatus, SaveStatus } from "../types";
import type { RommDataChangedDetail } from "../types/events";
import { formatLastPlayed, formatPlaytime } from "../utils/formatters";
import { biosColorForLevel } from "../utils/biosColor";
import {
  applySaveSyncDisplay,
  extractBiosInfo,
  extractCoreInfo,
  resolveSaveSyncLabel,
  timeoutMs,
} from "../utils/playSection";
import {
  refreshAchievementsInBackground,
  refreshActiveSlotInBackground,
  refreshBiosInBackground,
  refreshCoreInfoInBackground,
} from "../utils/sectionRefresh";

/** Track which appIds have had auto-artwork applied this session */
const artworkApplied = new Set<number>();

interface RomMPlaySectionProps {
  appId: number;
}

type ConnectionState = "checking" | "connected" | "offline";

interface InfoState {
  romId: number | null;
  romName: string;
  platformSlug: string;
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
  platformCoreLabel: string | null;
  hasGameOverride: boolean;
  activeSlot: string | null;
  raId: number | null;
  achievementEarned: number;
  achievementTotal: number;
}

import { setRommConnectionState, setVersionError } from "../utils/connectionState";
import { useVersionError } from "./VersionErrorCard";
import { useMigrationStatus } from "./MigrationBlockedPage";
import { detach } from "../utils/detach";

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
        .then(() => {
          artworkApplied.add(appId);
        })
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
        ...extractBiosInfo(cached.bios_level ?? null, cached.bios_label ?? null),
      }));
    }

    if (staleFields.includes("bios")) {
      refreshBiosInBackground(romId, cancelled, setter);
    }

    // Core info: sourced from its OWN path (#923), independent of BIOS status.
    // Fetched non-blocking so the core button / badge can render once cores are
    // known, regardless of whether the platform needs BIOS. Keyed on rom_id so
    // the active core reflects a per-game DB override (epic #945).
    if (romId) {
      refreshCoreInfoInBackground(romId, cancelled, setter);
    }
  } catch (e) {
    detach(debugLog(`RomMPlaySection: loadCached error: ${e}`));
  }
}

// S3776 is raised on the declaration line, so its NOSONAR must stay there. prettier-ignore stops
// Prettier from relocating the trailing comment into the body (which would break the suppression).
// prettier-ignore
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
    platformCoreLabel: null,
    hasGameOverride: false,
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

    detach(loadCached(appId, () => cancelled, romIdRef, setInfo));

    // Per-event-type handlers — each owns one branch of the data-changed dispatch.
    // Defined inside useEffect to share the cancelled/romIdRef/setInfo closure.
    const handleSaveSyncSettingsChange = async (
      detail: Extract<RommDataChangedDetail, { type: "save_sync_settings" }>,
    ) => {
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

    const handleCoreChange = async (_detail: Extract<RommDataChangedDetail, { type: "core_changed" }>) => {
      const rid = romIdRef.current;
      if (!rid) return;
      // Core data comes from the dedicated core-info path (#923), keyed on the
      // rom_id from a ref to avoid a stale-closure read of InfoState. The active
      // core reflects the per-game DB override (epic #945). BIOS level/label
      // still come from the (now core-free) BIOS status — the active core just
      // switched, so the BIOS requirements may have changed.
      const [coreInfo, biosResult] = await Promise.all([getPlatformCoreInfo(rid), getBiosStatus(rid)]);
      if (cancelled) return;
      setInfo((prev) => ({
        ...prev,
        ...extractCoreInfo(coreInfo),
        // The new core may need different (or no) BIOS — re-derive biosNeeded
        // from the refreshed status so the missing-BIOS badge keys off the
        // active core, not the core that was active at mount (#923).
        biosNeeded: !!biosResult.bios_status,
        biosStatus: biosResult.bios_level,
        biosLabel: biosResult.bios_label ?? "",
      }));
    };

    const handleSaveSyncChange = async (detail: Extract<RommDataChangedDetail, { type: "save_sync" }>) => {
      const romId = romIdRef.current ?? detail.rom_id;
      if (!romId) return;
      // If event specifies a rom_id, skip if it's not for this game
      if (detail.rom_id && romIdRef.current && detail.rom_id !== romIdRef.current) return;
      const saveStatus: SaveStatus | null =
        detail.save_status ?? (await getSaveStatus(romId).catch((): SaveStatus | null => null));
      const { status: saveSyncStatus, label: saveSyncLabel } = applySaveSyncDisplay(
        saveStatus?.save_sync_display,
        saveStatus,
      );
      setInfo((prev) => ({
        ...prev,
        saveSyncStatus,
        saveSyncLabel,
        activeSlot: saveStatus && "active_slot" in saveStatus ? (saveStatus.active_slot ?? null) : prev.activeSlot,
      }));
    };

    const onDataChanged = (e: Event) => {
      detach(
        (async () => {
          try {
            const detail = (e as CustomEvent).detail;
            switch (detail?.type) {
              case "save_sync_settings":
                await handleSaveSyncSettingsChange(detail);
                break;
              case "core_changed":
                await handleCoreChange(detail);
                break;
              case "save_sync":
                await handleSaveSyncChange(detail);
                break;
            }
          } catch (err) {
            detach(debugLog(`RomMPlaySection: onDataChanged error: ${err}`));
          }
        })(),
      );
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

    // Reconcile-on-view (#868) — pull-only: folds the RomM playtime note total
    // into the local total so a session played on another device shows up the
    // moment the detail page opens. INDEPENDENT of save-sync — only gated on
    // connectivity, so it must NOT sit behind doSaveCheck's saveSyncEnabled
    // guard. Pushes the reconciled total through updatePlaytimeDisplay (the
    // overview write-chokepoint), which emits romm_playtime_changed; the
    // reactive PLAYTIME effect (#869) re-reads the overview and refreshes the
    // display on the same mount. server_query_failed → no-op (stay local).
    async function doReconcilePlaytime(isCancelled: boolean) {
      const romId = romIdRef.current;
      if (!romId) return;
      try {
        const result = await reconcilePlaytime(romId);
        if (isCancelled) return;
        if (result.server_query_failed) return;
        updatePlaytimeDisplay(appId, result.total_seconds, false);
      } catch (e) {
        detach(debugLog(`RomMPlaySection: playtime reconcile error: ${e}`));
      }
    }

    async function doSaveCheck(isCancelled: boolean) {
      const romId = romIdRef.current;
      if (!romId || !info.saveSyncEnabled) return;
      try {
        const saveStatus = await getSaveStatus(romId);
        if (isCancelled) return;
        const hasConflict = hasAnySaveConflict(saveStatus);
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: romId, has_conflict: hasConflict },
          }),
        );
        const { status: ss, label: sl } = applySaveSyncDisplay(saveStatus.save_sync_display, saveStatus);
        setInfo((prev) => ({
          ...prev,
          saveSyncStatus: ss,
          saveSyncLabel: sl,
          activeSlot: "active_slot" in saveStatus ? (saveStatus.active_slot ?? null) : prev.activeSlot,
        }));
      } catch (e) {
        detach(debugLog(`RomMPlaySection: background save check error: ${e}`));
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

        if (connected) {
          // Fire-and-forget reconcile — non-blocking, runs regardless of
          // save-sync. NOT awaited so it never delays the save check or render.
          detach(doReconcilePlaytime(cancelled));
          // Background save status check to detect new conflicts (save-sync only)
          await doSaveCheck(cancelled);
        }
      } catch {
        if (!cancelled) {
          setRommConnectionState("offline");
          setConnectionState("offline");
          globalThis.dispatchEvent(new CustomEvent("romm_connection_changed", { detail: { state: "offline" } }));
        }
      }
    };
    detach(check());
    return () => {
      cancelled = true;
    };
  }, [info.saveSyncEnabled, appId]);

  // Reactive PLAYTIME display (#869) — re-read Steam's overview whenever the
  // playtime write-chokepoint (updatePlaytimeDisplay) fires romm_playtime_changed
  // for this appId. Drives the displayed PLAYTIME / LAST PLAYED from the source
  // of truth (the overview) instead of a mount-only snapshot, so a session end
  // (handleGameStop) or a multi-device reconcile-on-view refreshes the value on
  // the SAME mount — no navigate-away/back remount required.
  useEffect(() => {
    const onPlaytimeChanged = (e: Event) => {
      const detail = (e as CustomEvent<{ appId?: number } | null>).detail;
      if (detail?.appId !== appId) return;
      const ov = appStore.GetAppOverviewByAppID(appId);
      if (!ov) return;
      setInfo((prev) => ({
        ...prev,
        playtime: formatPlaytime(ov.minutes_playtime_forever ?? 0),
        lastPlayed: formatLastPlayed(ov.rt_last_time_played ?? 0),
      }));
    };
    globalThis.addEventListener("romm_playtime_changed", onPlaytimeChanged);
    return () => {
      globalThis.removeEventListener("romm_playtime_changed", onPlaytimeChanged);
    };
  }, [appId]);

  // Helper: create an info item with header and value (Steam's two-line pattern)
  const infoItem = (key: string, header: string, value: string, extraClass?: string) =>
    createElement(
      "div",
      {
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
    const romId = info.romId;
    setActionPending("artwork");
    try {
      // Step 1: re-download the RomM cover, rename to {app_id}p.png, and
      // patch cover_path on the registry row so the game info panel can
      // render the refreshed image.
      const coverResult = await refreshCoverArtwork(romId).catch(
        (e): { success: boolean; reason?: string; message: string; cover_path?: string } => {
          detach(debugLog(`refreshCoverArtwork rejected: ${e}`));
          return { success: false, reason: "exception", message: String(e) };
        },
      );
      if (coverResult.success) {
        // Notify the game info panel so it can re-render the cover image.
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "cover_refreshed", rom_id: romId },
          }),
        );
      } else {
        detach(debugLog(`refreshCoverArtwork failed: ${coverResult.reason} — ${coverResult.message}`));
      }

      // Step 2: resolve which SGDB game id to use. The backend either picks
      // one automatically (RomM/IGDB) or hands back manual candidates.
      const resolution = await getSgdbResolution(romId).catch((e): null => {
        detach(debugLog(`getSgdbResolution rejected: ${e}`));
        return null;
      });
      if (!resolution) {
        toaster.toast({ title: "RomM Sync", body: "Failed to refresh artwork" });
        return;
      }

      switch (resolution.decision) {
        case "no_api_key":
          toaster.toast({ title: "RomM Sync", body: "Set a SteamGridDB API key in settings first" });
          break;
        case "resolved": {
          const applied = await applyArtwork(romId, appId);
          if (applied === -1) {
            toaster.toast({ title: "RomM Sync", body: "Set a SteamGridDB API key in settings first" });
          } else if (applied > 0) {
            toaster.toast({ title: "RomM Sync", body: `Artwork refreshed (${applied}/4 images applied)` });
          } else {
            toaster.toast({ title: "RomM Sync", body: "No artwork available for this game" });
          }
          break;
        }
        case "needs_pick":
          showModal(
            createElement(SgdbGamePickerModalContent, {
              romId,
              appId,
              romName: info.romName,
              candidates: resolution.candidates,
              onApplied: () => {},
            }),
          );
          break;
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
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "metadata", rom_id: info.romId } }),
      );
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
        const n = result.synced;
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
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: info.romId } }),
        );
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
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", { detail: { type: "bios", platform_slug: info.platformSlug } }),
        );
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
        strDescription:
          "This will delete local save files for this game. Make sure saves are synced to RomM first — the next sync will re-download them from the server.",
        strOKButtonText: "Delete",
        strCancelButtonText: "Cancel",
        onOK: () => {
          detach(
            (async () => {
              setActionPending("deletesaves");
              try {
                const result = await deleteLocalSaves(romId);
                if (result.success) {
                  toaster.toast({ title: "RomM Sync", body: result.message });
                  // Directly update PlaySection status — no local saves remain
                  setInfo((prev) => ({ ...prev, saveSyncStatus: "none" as const, saveSyncLabel: "No saves" }));
                  globalThis.dispatchEvent(
                    new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }),
                  );
                } else {
                  toaster.toast({ title: "RomM Sync", body: result.message || "Failed to delete saves" });
                }
              } catch {
                toaster.toast({ title: "RomM Sync", body: "Failed to delete saves" });
              } finally {
                setActionPending(null);
              }
            })(),
          );
        },
      }),
    );
  };

  /** Refresh the core badge + BIOS state from their dedicated paths and notify
   *  sibling components after a successful override pin/clear. */
  const refreshCoreDisplay = async (romId: number, platformSlug: string) => {
    // Core data comes from the dedicated core-info path (#923), keyed on rom_id
    // so the per-game DB override (epic #945) reads back as the active core.
    // BIOS level/label still come from getBiosStatus — the active core just
    // switched, so the BIOS requirements may have changed.
    const [coreInfo, refreshed] = await Promise.all([
      getPlatformCoreInfo(romId),
      getBiosStatus(romId).catch(() => ({
        bios_status: null as BiosStatus | null,
        bios_level: null as "ok" | "partial" | "missing" | null,
        bios_label: null as string | null,
      })),
    ]);
    setInfo((prev) => ({
      ...prev,
      ...extractCoreInfo(coreInfo),
      // Re-derive biosNeeded from the refreshed status so the missing-BIOS
      // badge keys off the now-active core (#923).
      biosNeeded: !!refreshed.bios_status,
      biosStatus: refreshed.bios_level,
      biosLabel: refreshed.bios_label ?? "",
    }));
    // Invalidate the frontend cache and notify other components (e.g. GameInfoPanel)
    invalidateCachedGameDetail(appId);
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", { detail: { type: "core_changed", platform_slug: platformSlug } }),
    );
  };

  /** Apply the result of a set/clear override call. The backend re-bakes the
   *  launch_options + returns the bound app_id for an installed ROM; we
   *  confirm-set them on the Steam shortcut BEFORE toasting success (R1). An
   *  unconfirmed bake gets a DISTINCT "restart Steam" toast and the DB row is
   *  KEPT — migration/re-sync re-bake from the pin. Uninstalled/unbound ROMs
   *  carry no launch_options/app_id: persist-only, no SetAppLaunchOptions. */
  const applyCoreResult = async (
    result: Awaited<ReturnType<typeof setGameCore>>,
    romId: number,
    platformSlug: string,
    successBody: string,
  ) => {
    if (!result.success) {
      toaster.toast({ title: "RomM Sync", body: result.message || "Failed to set core" });
      return;
    }
    // Installed + bound: confirm the re-baked launch_options landed before
    // claiming success. app_id can be null/undefined for an unbound ROM.
    if (result.launch_options !== undefined && result.app_id != null) {
      const confirmed = await setLaunchOptionsConfirmed(result.app_id, result.launch_options);
      if (!confirmed) {
        // Never toast success on an unconfirmed bake. Keep the DB row — a Steam
        // restart (or the next migration/re-sync) re-bakes from the override.
        toaster.toast({ title: "RomM Sync", body: "Core saved — restart Steam to apply" });
        return;
      }
    }
    // Confirmed (or uninstalled/unbound: nothing to confirm) → success.
    toaster.toast({ title: "RomM Sync", body: successBody });
    await refreshCoreDisplay(romId, platformSlug);
  };

  const handleChangeGameCore = async (coreLabel: string) => {
    const romId = info.romId;
    if (!romId || !info.platformSlug) return;
    const platformSlug = info.platformSlug;
    detach(debugLog(`handleChangeGameCore: romId=${romId} coreLabel=${coreLabel}`));
    try {
      const result = await setGameCore(romId, coreLabel);
      detach(debugLog(`handleChangeGameCore: result success=${result.success}`));
      await applyCoreResult(result, romId, platformSlug, `Core set to ${coreLabel}`);
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Failed to set core" });
    }
  };

  const handleResetGameCore = async () => {
    const romId = info.romId;
    if (!romId || !info.platformSlug) return;
    const platformSlug = info.platformSlug;
    detach(debugLog(`handleResetGameCore: romId=${romId}`));
    try {
      const result = await clearGameCore(romId);
      detach(debugLog(`handleResetGameCore: result success=${result.success}`));
      await applyCoreResult(result, romId, platformSlug, "Now following the system core");
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Failed to reset core" });
    }
  };

  const showCoreMenu = (e: Event) => {
    showContextMenu(
      createElement(
        Menu,
        { label: "Emulator Core" },
        createElement(
          MenuItem,
          { key: "core-compat", disabled: true },
          "Switching cores may affect save compatibility",
        ),
        createElement(MenuSeparator, { key: "core-sep" }),
        // "Use System Override" is the dedicated reset item: selecting it CLEARS
        // the per-game pin so the game follows the per-platform/system core. The
        // fallback label (the core the game falls back to with no pin) is the
        // per-platform override when set, else the es_systems default. The
        // checkmark sits here when there is NO per-game override — i.e. the
        // game already follows the system. "System Override" deliberately
        // differs from the "(default)" core marker so the menu never shows two
        // "defaults" (#211).
        (() => {
          const defaultLabel = info.availableCores.find((c) => c.is_default)?.label;
          const fallbackLabel = info.platformCoreLabel ?? defaultLabel ?? null;
          const fallbackSuffix = fallbackLabel ? ` (${fallbackLabel})` : "";
          // ✓ when the game already follows the system (no per-game pin).
          const followsSystemMark = info.hasGameOverride ? "" : " ✓";
          return createElement(
            MenuItem,
            {
              key: "core-follow-system",
              onClick: () => {
                detach(handleResetGameCore());
              },
            },
            `Use System Override${fallbackSuffix}${followsSystemMark}`,
          );
        })(),
        createElement(MenuSeparator, { key: "core-follow-sep" }),
        ...info.availableCores.map((c) => {
          // The active marker sits on the ACTIVE core: the default-marked entry
          // when no override is pinned, otherwise the pinned core (#945).
          const isActive = info.activeCoreIsDefault ? c.is_default : info.activeCoreLabel === c.label;
          // The (system) marker sits on the per-platform override set on the
          // System page (settings.json platform_cores). A core can carry both
          // "(default) (system)" and "(system) ✓" — all three roles are
          // independent (#954).
          const isPlatformCore = info.platformCoreLabel !== null && c.label === info.platformCoreLabel;
          return createElement(
            MenuItem,
            {
              key: `core-${c.core_so}`,
              // Every core PINS the per-game override, including the
              // default-marked one. The dedicated "Use System Override" item
              // above is the only clear path (#211).
              onClick: () => {
                detach(handleChangeGameCore(c.label));
              },
            },
            `${c.label}${c.is_default ? " (default)" : ""}${isPlatformCore ? " (system)" : ""}${isActive ? " \u2713" : ""}`,
          );
        }),
      ),
      getEventTarget(e),
    );
  };

  const showRomMMenu = (e: Event) => {
    showContextMenu(
      createElement(
        Menu,
        { label: "RomM Actions" },
        createElement(
          MenuItem,
          {
            key: "refresh-artwork",
            onClick: () => {
              detach(handleRefreshArtwork());
            },
          },
          "Refresh Artwork",
        ),
        createElement(
          MenuItem,
          {
            key: "refresh-metadata",
            onClick: () => {
              detach(handleRefreshMetadata());
            },
          },
          "Refresh Metadata",
        ),
        createElement(
          MenuItem,
          {
            key: "sync-saves",
            onClick: () => {
              detach(handleSyncSaves());
            },
          },
          "Sync Save Files",
        ),
        createElement(
          MenuItem,
          {
            key: "download-bios",
            onClick: () => {
              detach(handleDownloadBios());
            },
          },
          "Download BIOS",
        ),
        createElement(MenuSeparator, { key: "sep" }),
        createElement(
          MenuItem,
          { key: "delete-saves", tone: "destructive", onClick: handleDeleteSaves },
          "Delete Local Saves",
        ),
        createElement(
          MenuItem,
          {
            key: "uninstall",
            tone: "destructive",
            onClick: () => {
              detach(handleUninstall());
            },
          },
          "Uninstall",
        ),
      ),
      getEventTarget(e),
    );
  };

  const showSteamMenu = (e: Event) => {
    showContextMenu(
      createElement(
        Menu,
        { label: "Steam" },
        createElement(
          MenuItem,
          {
            key: "properties",
            onClick: () => {
              SteamClient.Apps.OpenAppSettingsDialog(appId, "general");
            },
          },
          "Properties",
        ),
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
      createElement(
        "div",
        {
          key: "offline-indicator",
          className: "romm-info-item",
        },
        createElement(
          "div",
          { className: "romm-info-header" },
          createElement(FaExclamationTriangle, { size: 12, color: "#ff8800" }),
        ),
        createElement(
          "div",
          {
            className: "romm-info-value",
            style: { color: "#ff8800" },
          },
          "RomM offline",
        ),
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
    const countLabel =
      info.achievementTotal > 0 ? `${info.achievementEarned}/${info.achievementTotal}` : `${info.achievementEarned}`;

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
    const sparkleDots = hasEarned
      ? sparklePositions.map((pos, i) =>
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
        )
      : [];

    infoItems.push(
      createElement(
        "div",
        {
          key: "achievements",
          className: "romm-info-item romm-cheevo-badge",
          onClick: () => {
            globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
          },
        },
        createElement("div", { className: "romm-info-header" }, "ACHIEVEMENTS"),
        createElement(
          "div",
          {
            className: "romm-cheevo-badge-sparkle",
          },
          // Trophy icon with sparkle container
          createElement(
            "span",
            { style: { position: "relative", display: "inline-block" } },
            createElement(
              "span",
              {
                className: hasEarned ? "romm-cheevo-trophy" : "romm-cheevo-trophy-none",
              },
              "\uD83C\uDFC6",
            ),
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
      createElement(
        "div",
        {
          key: "legacy-slot-warning",
          className: "romm-info-item",
        },
        createElement("div", { className: "romm-info-header" }, "SAVE SYNC"),
        createElement(
          "div",
          {
            style: { fontSize: "11px", color: "#ff8800", marginTop: "4px" },
          },
          "\u26A0 Legacy save slot",
        ),
      ),
    );
  }

  // BIOS warning (only when files are missing — OK status moved to tab)
  if (info.biosNeeded && info.biosStatus && info.biosStatus !== "ok") {
    const biosColor = biosColorForLevel(info.biosStatus);
    infoItems.push(
      createElement(
        "div",
        {
          key: "bios",
          className: "romm-info-item",
          onClick: () => {
            globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
          },
          style: { cursor: "pointer" },
        },
        createElement("div", { className: "romm-info-header" }, "BIOS"),
        createElement(
          "div",
          {
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

  return createElement(
    Focusable,
    {
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
    createElement(
      "div",
      {
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
    createElement(
      "div",
      {
        style: {
          marginLeft: "auto",
          display: "flex",
          alignItems: "center",
          gap: "8px",
          flexShrink: 0,
        },
      },
      // RomM actions button
      createElement(
        DialogButton,
        {
          className: "romm-gear-btn",
          onClick: showRomMMenu,
          onFocus: scrollToTop,
          title: "RomM Actions",
        },
        createElement(FaGamepad, { size: 18, color: "#553e98" }),
      ),
      // Core selection button (only when multiple cores available)
      ...(info.availableCores.length > 1
        ? [
            createElement(
              DialogButton,
              {
                key: "core-btn",
                className: "romm-gear-btn",
                onClick: showCoreMenu,
                onFocus: scrollToTop,
                title: "Emulator Core",
              },
              createElement(FaMicrochip, { size: 18, color: info.activeCoreIsDefault ? "#8f98a0" : "#d4a72c" }),
            ),
          ]
        : []),
      // Steam properties button
      createElement(
        DialogButton,
        {
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
