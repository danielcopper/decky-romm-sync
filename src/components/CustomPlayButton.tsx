/**
 * Custom Play button that replaces the native Steam Play button on RomM game
 * detail pages. Handles 3 primary states:
 * - Download: ROM not installed, click to download
 * - Play: ROM installed, launches the game (with pre-launch save sync)
 * - Syncing: Save sync in progress before launch
 *
 * Includes a dropdown menu button (arrow) to the right of the Play button
 * with action: Uninstall.
 */

import { useState, useEffect, useRef, FC } from "react";
import { addEventListener, removeEventListener, toaster } from "@decky/api";
import {
  Focusable,
  DialogButton,
  ConfirmModal,
  Menu,
  MenuItem,
  showContextMenu,
  showModal,
  appActionButtonClasses,
  basicAppDetailsSectionStylerClasses,
} from "@decky/ui";
import { hideNativePlaySection, showNativePlaySection } from "../utils/styleInjector";
import {
  getCachedGameDetail,
  startDownload,
  removeRom,
  debugLog,
  preLaunchSync,
  logError,
  isSaveTrackingConfigured,
  getSaveSetupInfo,
  confirmSlotChoice,
  checkCoreChange,
} from "../api/backend";
import { getRommConnectionState } from "../utils/connectionState";
import { scrollToTop } from "../utils/scrollHelpers";
import { showConflictResolutionModal } from "./ConflictModal";
import { showCoreChangeModal } from "./CoreChangeModal";
import { showNewerInSlotModal } from "./NewerInSlotModal";
import type { DownloadProgressEvent, DownloadCompleteEvent, PendingConflict, NewerInSlotConflict } from "../types";
import { isNewerInSlotConflict } from "../types";

type PlayButtonState = "loading" | "not_romm" | "download" | "conflict" | "syncing" | "play" | "launching" | "dl_complete" | "uninstalling";

async function handleConflicts(conflicts: (PendingConflict | NewerInSlotConflict)[]): Promise<"cancel" | "resolved"> {
  const newerInSlot = conflicts.filter(isNewerInSlotConflict);
  const regularConflicts = conflicts.filter((c): c is PendingConflict => !isNewerInSlotConflict(c));

  for (const nis of newerInSlot) {
    const resolution = await showNewerInSlotModal(nis);
    if (resolution === "cancel") return "cancel";
  }

  if (regularConflicts.length > 0) {
    const resolution = await showConflictResolutionModal(regularConflicts);
    if (resolution === "cancel") return "cancel";
  }

  return "resolved";
}

interface DownloadProgress {
  bytesDownloaded: number;
  totalBytes: number;
}

function lerpColor(a: [number, number, number], b: [number, number, number], t: number): string {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r}, ${g}, ${bl})`;
}

// Download button blue gradient stops
const BLUE_LEFT: [number, number, number] = [26, 159, 255];   // #1a9fff
const BLUE_RIGHT: [number, number, number] = [0, 120, 212];   // #0078d4
// Play button visible green (computed from gradient + backgroundSize 330% + backgroundPosition 25%)
const GREEN_LEFT: [number, number, number] = [80, 200, 47];   // #50c82f
const GREEN_RIGHT: [number, number, number] = [24, 177, 78];  // #18b14e

function formatProgress(downloaded: number, total: number): string {
  // Show "x / y MB" with unit only on the total
  if (total < 1024) return `${downloaded} / ${total} B`;
  if (total < 1024 * 1024) return `${(downloaded / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} KB`;
  if (total < 1024 * 1024 * 1024) return `${(downloaded / (1024 * 1024)).toFixed(1)} / ${(total / (1024 * 1024)).toFixed(1)} MB`;
  return `${(downloaded / (1024 * 1024 * 1024)).toFixed(2)} / ${(total / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

interface CustomPlayButtonProps {
  appId: number;
}

function showLaunchConfirmation(title: string, message: string): Promise<boolean> {
  return new Promise((resolve) => {
    showModal(
      <ConfirmModal
        strTitle={title}
        strDescription={message}
        strOKButtonText="Launch Anyway"
        strCancelButtonText="Cancel"
        onOK={() => resolve(true)}
        onCancel={() => resolve(false)}
      />,
    );
  });
}

export const CustomPlayButton: FC<CustomPlayButtonProps> = ({ appId }) => {
  const [state, setState] = useState<PlayButtonState>("loading");
  const [romId, setRomId] = useState<number | null>(null);
  const [romName, setRomName] = useState<string>("");
  const [actionPending, setActionPending] = useState(false);
  const [dlProgress, setDlProgress] = useState<DownloadProgress | null>(null);
  const [isOffline, setIsOffline] = useState(getRommConnectionState() === "offline");
  const romIdRef = useRef<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const transitionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Hide the native PlaySection via CSS while this component is mounted
  useEffect(() => {
    const cls = basicAppDetailsSectionStylerClasses?.PlaySection;
    if (cls) hideNativePlaySection(cls);
    return () => { showNativePlaySection(); };
  }, []);

  // Clear transition timers (dl_complete→play, uninstalling→download) on unmount
  useEffect(() => {
    return () => {
      if (transitionTimerRef.current) clearTimeout(transitionTimerRef.current);
    };
  }, []);

  // Initial load: determine ROM status from cache (instant, no network calls)
  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        const cached = await getCachedGameDetail(appId);
        debugLog(`CustomPlayButton init: appId=${appId} cached.found=${cached.found} cancelled=${cancelled}`);
        if (cancelled) return;
        if (!cached.found) {
          debugLog(`CustomPlayButton: -> not_romm (not in cache)`);
          setState("not_romm");
          return;
        }

        const rid = cached.rom_id!;
        setRomId(rid);
        romIdRef.current = rid;
        if (cached.rom_name) setRomName(cached.rom_name);

        if (!cached.installed) {
          debugLog(`CustomPlayButton: -> download`);
          setState("download");
        } else {
          // Check for conflicts from cached save status
          const hasConflict = cached.save_status?.files?.some((f) => f.status === "conflict") ?? false;
          if (hasConflict) {
            debugLog(`CustomPlayButton: -> conflict (from cache)`);
            setState("conflict");
          } else {
            debugLog(`CustomPlayButton: -> play`);
            setState("play");
          }
        }
      } catch (e) {
        logError(`CustomPlayButton init error: ${e}`);
        if (!cancelled) {
          setState("not_romm");
        }
      }
    }

    init();
    return () => { cancelled = true; };
  }, [appId]);

  // Listen for download events
  useEffect(() => {
    const progressListener = addEventListener<[DownloadProgressEvent]>(
      "download_progress",
      (evt: DownloadProgressEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        if (evt.status === "failed" || evt.status === "cancelled") {
          setState("download");
          setActionPending(false);
          setDlProgress(null);
        } else {
          setDlProgress({ bytesDownloaded: evt.bytes_downloaded, totalBytes: evt.total_bytes });
        }
      },
    );

    const completeListener = addEventListener<[DownloadCompleteEvent]>(
      "download_complete",
      (evt: DownloadCompleteEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        // Brief completion flash before transitioning to Play
        setDlProgress(null);
        setActionPending(false);
        setState("dl_complete");
        transitionTimerRef.current = setTimeout(() => setState("play"), 1100);
      },
    );

    const onUninstall = (e: Event) => {
      const romId = (e as CustomEvent).detail?.rom_id;
      if (romId !== romIdRef.current) return;
      // Don't override uninstalling animation if we triggered it ourselves
      setState((prev) => prev === "uninstalling" ? prev : "download");
      setActionPending(false);
    };
    window.addEventListener("romm_rom_uninstalled", onUninstall);

    // Listen for save sync updates (e.g. background check found a conflict)
    const onDataChanged = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.type !== "save_sync") return;
      if (detail.rom_id && detail.rom_id !== romIdRef.current) return;
      // Update button state based on conflict info from the event
      if (detail.has_conflict !== undefined) {
        setState((prev) => {
          if (prev === "syncing" || prev === "launching" || prev === "download") return prev;
          return detail.has_conflict ? "conflict" : "play";
        });
      }
    };
    window.addEventListener("romm_data_changed", onDataChanged);

    const onConnectionChanged = (e: Event) => {
      const connState = (e as CustomEvent).detail?.state;
      setIsOffline(connState === "offline");
    };
    window.addEventListener("romm_connection_changed", onConnectionChanged);

    return () => {
      removeEventListener("download_progress", progressListener);
      removeEventListener("download_complete", completeListener);
      window.removeEventListener("romm_rom_uninstalled", onUninstall);
      window.removeEventListener("romm_data_changed", onDataChanged);
      window.removeEventListener("romm_connection_changed", onConnectionChanged);
    };
  }, []);

  // Programmatically focus our Play/Download button after mount.
  // This beats HLTB and other plugins that also compete for initial focus.
  useEffect(() => {
    if (state !== "play" && state !== "download" && state !== "conflict") return;
    const timer = setTimeout(() => {
      if (containerRef.current) {
        const btn = containerRef.current.querySelector("button");
        if (btn) {
          btn.focus();
          btn.classList.add("gpfocus");
        }
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [state]);

  const handlePlay = async () => {
    if (state === "syncing" || state === "launching") return; // debounce
    const overview = appStore.GetAppOverviewByAppID(appId);
    const gameId = overview?.GetGameID?.() ?? String(appId);
    debugLog(`CustomPlayButton: handlePlay appId=${appId} gameId=${gameId}`);

    // Pre-launch save sync
    if (romId) {
      if (getRommConnectionState() === "offline") {
        // RomM offline — warn user, skip sync attempt entirely
        const proceed = await showLaunchConfirmation(
          "RomM Offline",
          "Can't sync saves — RomM server is unreachable. Launch with local saves? Saves will sync after exit when the server is back, but may produce conflicts.",
        );
        if (!proceed) {
          setState("play");
          return;
        }
      } else {
        // Check save slot tracking is configured
        const trackingResult = await isSaveTrackingConfigured(romId).catch(() => ({ configured: true }));
        if (!trackingResult.configured) {
          // Check if server has saves — if not, auto-configure
          try {
            const setupInfo = await getSaveSetupInfo(romId);
            if (!setupInfo.has_local_saves && setupInfo.server_slots.length === 0) {
              // No saves anywhere — auto-configure with default, proceed
              await confirmSlotChoice(romId, setupInfo.default_slot, null);
            } else if (setupInfo.has_local_saves && setupInfo.server_slots.length === 0) {
              // Scenario B: local saves, no server — auto-configure
              await confirmSlotChoice(romId, setupInfo.default_slot, null);
            } else {
              // Server has saves — user must configure in saves tab
              toaster.toast({
                title: "RomM Save Sync",
                body: "Configure save sync in the Saves tab first",
              });
              // Switch to saves tab
              globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
              setState("play");
              return;
            }
          } catch {
            // If check fails, proceed with launch anyway
          }
        }

        // Check for core change before syncing
        const coreCheck = await checkCoreChange(romId).catch((): { changed: boolean; old_core?: string; new_core?: string; old_label?: string; new_label?: string } => ({ changed: false }));
        if (coreCheck.changed) {
          const proceed = await showCoreChangeModal(
            coreCheck.old_label ?? coreCheck.old_core ?? "Unknown",
            coreCheck.new_label ?? coreCheck.new_core ?? "Unknown",
          );
          if (!proceed) {
            setState("play");
            return;
          }
        }

        setState("syncing");
        try {
          const result = await Promise.race([
            preLaunchSync(romId),
            new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), 15000)),
          ]);

          debugLog(`CustomPlayButton: preLaunchSync result: synced=${result.synced} conflicts=${result.conflicts?.length ?? 0} success=${result.success}`);

          if (result.conflicts && result.conflicts.length > 0) {
            const conflictResult = await handleConflicts(result.conflicts);
            if (conflictResult === "cancel") {
              setState("conflict");
              return;
            }
            // Conflicts resolved — notify sibling components to refresh
            globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
          }

          if (!result.success && result.errors && result.errors.length > 0) {
            debugLog(`CustomPlayButton: pre-launch sync errors: ${result.errors.join(", ")}`);
            const proceed = await showLaunchConfirmation(
              "Save Sync Unavailable",
              "Couldn't sync saves with RomM server. Launch with local saves?",
            );
            if (!proceed) {
              setState("play");
              return;
            }
          } else if (result.synced && result.synced > 0) {
            toaster.toast({ title: "RomM Save Sync", body: "Saves downloaded from RomM" });
          }
        } catch (e) {
          debugLog(`CustomPlayButton: pre-launch sync failed: ${e}`);
          const proceed = await showLaunchConfirmation(
            "Save Sync Unavailable",
            "Couldn't sync saves with RomM server. Launch with local saves?",
          );
          if (!proceed) {
            setState("play");
            return;
          }
        }
      }
    }

    setState("launching");
    SteamClient.Apps.RunGame(gameId, "", -1, 100);
  };

  const handleResolveConflict = async () => {
    if (!romId) return;
    setState("syncing");
    try {
      const result = await Promise.race([
        preLaunchSync(romId),
        new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), 15000)),
      ]);

      if (result.conflicts && result.conflicts.length > 0) {
        const conflictResult = await handleConflicts(result.conflicts);
        if (conflictResult === "cancel") {
          setState("conflict");
          return;
        }
      }
      // Resolved or no conflicts left — notify siblings and go back to play
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
      setState("play");
    } catch (e) {
      debugLog(`CustomPlayButton: resolve conflict failed: ${e}`);
      toaster.toast({ title: "RomM Sync", body: "Couldn't reach server to resolve conflict" });
      setState("conflict");
    }
  };

  const handleDownload = async () => {
    if (!romId || actionPending) return;
    setActionPending(true);
    try {
      const result = await startDownload(romId);
      if (!result.success) {
        toaster.toast({ title: "RomM Sync", body: result.message || "Download failed" });
        setActionPending(false);
      }
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Download failed — is RomM server running?" });
      setActionPending(false);
    }
  };

  const handleUninstall = async () => {
    if (!romId) return;
    debugLog(`CustomPlayButton: uninstalling romId=${romId}`);
    try {
      const result = await removeRom(romId);
      if (result.success) {
        globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: romId } }));
        toaster.toast({ title: "RomM Sync", body: `${romName || "ROM"} uninstalled` });
        // Dark pulse transition before showing Download button
        setState("uninstalling");
        transitionTimerRef.current = setTimeout(() => setState("download"), 500);
        return;
      } else {
        toaster.toast({ title: "RomM Sync", body: result.message || "Uninstall failed" });
      }
    } catch {
      toaster.toast({ title: "RomM Sync", body: "Uninstall failed" });
    }
  };

  const showDropdownMenu = (e: MouseEvent) => {
    showContextMenu(
      <Menu label="RomM Actions">
        <MenuItem key="uninstall" tone="destructive" onClick={handleUninstall}>
          Uninstall
        </MenuItem>
      </Menu>,
      e.currentTarget as EventTarget,
    );
  };

  // Don't render for non-RomM games
  if (state === "not_romm" || state === "loading") {
    debugLog(`CustomPlayButton: returning null (state=${state})`);
    return null;
  }
  debugLog(`CustomPlayButton: rendering state=${state}`);

  // Dropdown arrow button style
  const dropdownArrowStyle: React.CSSProperties = {
    height: "48px",
    width: "36px",
    minWidth: "36px",
    padding: 0,
    border: "none",
    borderRadius: "0 2px 2px 0",
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    borderLeft: "1px solid rgba(0, 0, 0, 0.2)",
  };

  // Consistent button container size across all states (Play has dropdown = 36px extra)
  const btnContainerStyle: React.CSSProperties = {
    display: "flex",
    flexDirection: "row",
    width: "200px",
    height: "48px",
  };

  const mainBtnStyle: React.CSSProperties = {
    height: "100%",
    flex: "1 1 auto",
    padding: "4px 12px",
    border: "none",
    color: "#fff",
    fontSize: "16px",
    fontWeight: "bold",
  };

  if (state === "dl_complete") {
    // "Ready!" state — must match the Play button exactly (same classes + Green tint)
    return (
      <Focusable
        className={[appActionButtonClasses?.PlayButtonContainer, appActionButtonClasses?.Green].filter(Boolean).join(" ")}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", "romm-dl-complete-flash"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #80e62a, #01b866)",
            filter: "brightness(1.2)",
          }}
          disabled
        >
          <span className="romm-dl-label">Ready!</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "download") {
    const t = dlProgress && dlProgress.totalBytes > 0
      ? dlProgress.bytesDownloaded / dlProgress.totalBytes
      : 0;
    const downloading = actionPending && dlProgress;

    // Fill color shifts from blue to green as download progresses
    const fillColor = downloading
      ? `linear-gradient(to right, ${lerpColor(BLUE_LEFT, GREEN_LEFT, t)}, ${lerpColor(BLUE_RIGHT, GREEN_RIGHT, t)})`
      : "linear-gradient(to right, #1a9fff, #0078d4)";

    // Pulse color shifts from blue to green with progress
    const pulseColor = downloading
      ? lerpColor(BLUE_LEFT, GREEN_LEFT, t)
      : "rgba(26,159,255,0.7)";

    const dlLabel = downloading
      ? formatProgress(dlProgress.bytesDownloaded, dlProgress.totalBytes)
      : actionPending
        ? "Starting..."
        : "Download";

    // Unfilled portion: darker shade of the current fill color
    const baseBg = isOffline
      ? "linear-gradient(to right, #6b7b8b, #5a6a7a)"
      : downloading
        ? `linear-gradient(to right, ${lerpColor([10, 50, 90], [5, 35, 65], t)}, ${lerpColor([5, 35, 65], [5, 50, 30], t)})`
        : "linear-gradient(to right, #1a9fff, #0078d4)";

    return (
      <Focusable
        ref={containerRef}
        className={appActionButtonClasses?.PlayButtonContainer}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[
            appActionButtonClasses?.PlayButton,
            "romm-btn-download",
            downloading && "romm-dl-active",
          ].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: baseBg,
            "--romm-pulse-color": pulseColor,
          } as React.CSSProperties}
          onClick={handleDownload}
          disabled={actionPending || isOffline}
        >
          {/* Progress fill bar */}
          {downloading && (
            <div
              className="romm-dl-fill"
              style={{
                width: `${t * 100}%`,
                background: fillColor,
              }}
            />
          )}
          <span className="romm-dl-label">{dlLabel}</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "uninstalling") {
    return (
      <Focusable
        className={appActionButtonClasses?.PlayButtonContainer}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-download", "romm-dl-uninstall-flash"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #47b3ff, #1a9fff)",
            filter: "brightness(1.3)",
          }}
          disabled
        >
          <span className="romm-dl-label">Uninstalled</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "launching") {
    return (
      <Focusable
        className={appActionButtonClasses?.PlayButtonContainer}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #70d61d 0%, #01a75b 60%)",
            backgroundPosition: "25%",
            backgroundSize: "330% 100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
          }}
          disabled
        >
          <span className={`${appActionButtonClasses?.Throbber || ""} romm-throbber`.trim()} />
          Launching...
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "syncing") {
    return (
      <Focusable
        className={appActionButtonClasses?.PlayButtonContainer}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #70d61d 0%, #01a75b 60%)",
            backgroundPosition: "25%",
            backgroundSize: "330% 100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "8px",
          }}
          disabled
        >
          <span className={`${appActionButtonClasses?.Throbber || ""} romm-throbber`.trim()} />
          Syncing saves...
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "conflict") {
    return (
      <Focusable
        ref={containerRef}
        className={appActionButtonClasses?.PlayButtonContainer}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-conflict"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #d4a72c, #b8941f)",
          }}
          onClick={handleResolveConflict}
        >
          Resolve Conflict
        </DialogButton>
      </Focusable>
    );
  }

  // state === "play"
  const playBg = isOffline
    ? "linear-gradient(to right, #6b7b6b 0%, #5a6a5a 60%)"
    : "linear-gradient(to right, #70d61d 0%, #01a75b 60%)";
  const dropdownBg = isOffline
    ? "linear-gradient(to right, #5a6a5a, #4d5d4d)"
    : "linear-gradient(to right, #4da636, #3f8a2b)";
  return (
    <Focusable
      ref={containerRef}
      className={[appActionButtonClasses?.PlayButtonContainer, !isOffline && appActionButtonClasses?.Green].filter(Boolean).join(" ")}
      style={btnContainerStyle}
    >
      <DialogButton
        className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"].filter(Boolean).join(" ")}
        style={{
          ...mainBtnStyle,
          borderRadius: "2px 0 0 2px",
          background: playBg,
          backgroundPosition: "25%",
          backgroundSize: "330% 100%",
        }}
        onClick={handlePlay}
        // @ts-expect-error onFocus works at runtime; not in Decky's DialogButton types
        onFocus={scrollToTop}
      >
        Play
      </DialogButton>
      <DialogButton
        className="romm-btn-dropdown"
        style={{
          ...dropdownArrowStyle,
          background: dropdownBg,
        }}
        onClick={showDropdownMenu}
      >
        <svg width="12" height="8" viewBox="0 0 12 8" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M1 1.5L6 6.5L11 1.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </DialogButton>
    </Focusable>
  );
};
