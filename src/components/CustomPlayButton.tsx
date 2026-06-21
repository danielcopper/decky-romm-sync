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
import { Focusable, DialogButton, ConfirmModal, Menu, MenuItem, showContextMenu, showModal } from "@decky/ui";
import { appActionButtonClasses, basicAppDetailsSectionStylerClasses } from "../utils/deckyUiInternals";
import { hideNativePlaySection, showNativePlaySection } from "../utils/styleInjector";
import { hasAnySaveConflict } from "../utils/saveStatus";
import {
  getCachedGameDetail,
  startDownload,
  cancelDownload,
  pauseDownload,
  resumeDownload,
  getDownloadQueue,
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
import { getEventTarget } from "../utils/events";
import { applyLaunchGateSetupOutcome, resolveSaveSetupOutcome } from "../utils/saveSetup";
import { handleButtonDownloadFailure } from "../utils/downloadFailure";
import { showCoreChangeModal } from "./CoreChangeModal";
import { showSyncConflictModal } from "./SyncConflictModal";
import type { DownloadProgressEvent, DownloadCompleteEvent, DownloadFailedEvent, SyncConflict } from "../types";
import { SAVEFILES_IN_CONTENT_DIR_REASON } from "../types";
import { detach } from "../utils/detach";

type PlayButtonState =
  | "loading"
  | "not_romm"
  | "download"
  | "conflict"
  | "syncing"
  | "play"
  | "launching"
  | "dl_complete"
  | "uninstalling";

async function handleConflicts(conflicts: SyncConflict[]): Promise<"cancel" | "resolved"> {
  // Backend now emits exactly one conflict type (sync_conflict). Walk them
  // sequentially — bail on first cancel so the caller can decide what to do.
  for (const conflict of conflicts) {
    const resolution = await showSyncConflictModal(conflict);
    if (resolution === "cancel") return "cancel";
  }
  return "resolved";
}

interface DownloadProgress {
  bytesDownloaded: number;
  totalBytes: number;
  /** Server honoured the Range probe — Pause/Resume is offered. */
  resumable: boolean;
  /** True once a paused frame arrives; the transfer is frozen, awaiting Resume. */
  paused: boolean;
}

function lerpColor(a: [number, number, number], b: [number, number, number], t: number): string {
  const r = Math.round(a[0] + (b[0] - a[0]) * t);
  const g = Math.round(a[1] + (b[1] - a[1]) * t);
  const bl = Math.round(a[2] + (b[2] - a[2]) * t);
  return `rgb(${r}, ${g}, ${bl})`;
}

// Download button blue gradient stops
const BLUE_LEFT: [number, number, number] = [26, 159, 255]; // #1a9fff
const BLUE_RIGHT: [number, number, number] = [0, 120, 212]; // #0078d4
// Play button visible green (computed from gradient + backgroundSize 330% + backgroundPosition 25%)
const GREEN_LEFT: [number, number, number] = [80, 200, 47]; // #50c82f
const GREEN_RIGHT: [number, number, number] = [24, 177, 78]; // #18b14e

function formatProgress(downloaded: number, total: number): string {
  // Show "x / y MB" with unit only on the total
  if (total < 1024) return `${downloaded} / ${total} B`;
  if (total < 1024 * 1024) return `${(downloaded / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} KB`;
  if (total < 1024 * 1024 * 1024)
    return `${(downloaded / (1024 * 1024)).toFixed(1)} / ${(total / (1024 * 1024)).toFixed(1)} MB`;
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

// S3776 is raised on the declaration line, so its NOSONAR must stay there. prettier-ignore stops
// Prettier from relocating the trailing comment into the body (which would break the suppression).
// prettier-ignore
export const CustomPlayButton: FC<CustomPlayButtonProps> = ({ appId }) => { // NOSONAR(typescript:S3776) — handlePlay gate chain; decomposed into gate-chain helpers in #389. Remaining cc is inherent to gate logic.
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
    return () => {
      showNativePlaySection();
    };
  }, []);

  // Clear transition timers (dl_complete→play, uninstalling→download) on unmount
  useEffect(() => {
    return () => {
      if (transitionTimerRef.current) clearTimeout(transitionTimerRef.current);
    };
  }, []);

  // Rehydrate an in-flight or paused download on remount. The cached detail
  // only knows installed-or-not, so without this a paused (or still-running)
  // download shows a plain "Download" button — and a click would `start_download`
  // → truncate the partial .tmp → restart from 0, discarding the paused progress
  // the user expected to resume. Seed from the live queue so the Pause/Resume
  // state survives navigating away and back (#1124).
  const rehydrateInflightDownload = async (rid: number): Promise<void> => {
    try {
      const queue = await getDownloadQueue();
      // No post-await `cancelled` guard needed: React 18 no-ops a setState on an
      // unmounted component, and a remount keeps its own state.
      const entry = queue.downloads.find((d) => d.rom_id === rid);
      if (entry && (entry.status === "downloading" || entry.status === "queued" || entry.status === "paused")) {
        setActionPending(true);
        setDlProgress({
          bytesDownloaded: entry.bytes_downloaded,
          totalBytes: entry.total_bytes,
          resumable: entry.resumable,
          paused: entry.status === "paused",
        });
      }
    } catch (e) {
      logError(`CustomPlayButton: failed to rehydrate download state: ${e}`);
    }
  };

  // Initial load: determine ROM status from cache (instant, no network calls)
  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        const cached = await getCachedGameDetail(appId);
        detach(debugLog(`CustomPlayButton init: appId=${appId} cached.found=${cached.found} cancelled=${cancelled}`));
        if (cancelled) return;
        if (!cached.found) {
          detach(debugLog(`CustomPlayButton: -> not_romm (not in cache)`));
          setState("not_romm");
          return;
        }

        const rid = cached.rom_id!;
        setRomId(rid);
        romIdRef.current = rid;
        if (cached.rom_name) setRomName(cached.rom_name);

        if (cached.installed) {
          // Check for conflicts from cached save status
          const hasConflict = hasAnySaveConflict(cached.save_status);
          if (hasConflict) {
            detach(debugLog(`CustomPlayButton: -> conflict (from cache)`));
            setState("conflict");
          } else {
            detach(debugLog(`CustomPlayButton: -> play`));
            setState("play");
          }
        } else {
          detach(debugLog(`CustomPlayButton: -> download`));
          setState("download");
          await rehydrateInflightDownload(rid);
        }
      } catch (e) {
        logError(`CustomPlayButton init error: ${e}`);
        if (!cancelled) {
          setState("not_romm");
        }
      }
    }

    detach(init());
    return () => {
      cancelled = true;
    };
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
          // A frame that omits resumable (older shape / progress tick before
          // the headers land) keeps the prior verdict instead of resetting it.
          setDlProgress((prev) => ({
            bytesDownloaded: evt.bytes_downloaded,
            totalBytes: evt.total_bytes,
            resumable: evt.resumable ?? prev?.resumable ?? false,
            paused: evt.status === "paused",
          }));
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

    /* istanbul ignore next -- delegation line; end-to-end wiring tested in CustomPlayButton.test.tsx */
    const failedListener = addEventListener<[DownloadFailedEvent]>(
      "download_failed",
      // The global listener in index.tsx owns the failure toast; here we only
      // reset local UI so the user can retry.
      (evt: DownloadFailedEvent) =>
        handleButtonDownloadFailure(evt, romIdRef.current, () => {
          setDlProgress(null);
          setActionPending(false);
          setState("download");
        }),
    );

    const onUninstall = (e: Event) => {
      const romId = (e as CustomEvent).detail?.rom_id;
      if (romId !== romIdRef.current) return;
      // Don't override uninstalling animation if we triggered it ourselves
      setState((prev) => (prev === "uninstalling" ? prev : "download"));
      setActionPending(false);
    };
    globalThis.addEventListener("romm_rom_uninstalled", onUninstall);

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
    globalThis.addEventListener("romm_data_changed", onDataChanged);

    const onConnectionChanged = (e: Event) => {
      const connState = (e as CustomEvent).detail?.state;
      setIsOffline(connState === "offline");
    };
    globalThis.addEventListener("romm_connection_changed", onConnectionChanged);

    return () => {
      removeEventListener("download_progress", progressListener);
      removeEventListener("download_complete", completeListener);
      removeEventListener("download_failed", failedListener);
      globalThis.removeEventListener("romm_rom_uninstalled", onUninstall);
      globalThis.removeEventListener("romm_data_changed", onDataChanged);
      globalThis.removeEventListener("romm_connection_changed", onConnectionChanged);
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

  // Offline confirm: skip sync attempt, ask user to launch with local saves.
  // Returns true when launch should proceed, false to bail back to "play".
  const confirmOfflineLaunch = async (): Promise<boolean> => {
    return showLaunchConfirmation(
      "RomM Offline",
      "Can't sync saves — RomM server is unreachable. Launch with local saves? Saves will sync after exit when the server is back, but may produce conflicts.",
    );
  };

  // Save-slot tracking gate. Delegates branch handling to applyLaunchGateSetupOutcome
  // so the per-outcome side effects (toast + saves-tab switch vs auto-confirm) stay
  // testable without rendering this component.
  //
  // The try only guards the network call (getSaveSetupInfo). Post-result branching
  // (resolveSaveSetupOutcome + applyLaunchGateSetupOutcome) sits OUTSIDE the try so
  // that an exception in a side-effect callback (toast / dispatchEvent / confirm)
  // cannot silently flip "abort" → "proceed" — the abort-propagation bug pattern
  // #619 was opened to prevent.
  const ensureTrackingConfigured = async (rid: number): Promise<"proceed" | "abort"> => {
    const trackingResult = await isSaveTrackingConfigured(rid).catch(() => ({ configured: true }));
    if (trackingResult.configured) return "proceed";

    let setupInfo;
    /* istanbul ignore next -- network-IO + defer-to-launch fallback; behavior tested at service layer */
    try {
      setupInfo = await getSaveSetupInfo(rid);
    } catch {
      // Network/backend failure — defer to launch rather than blocking the user.
      return "proceed";
    }

    /* istanbul ignore next -- delegates to applyLaunchGateSetupOutcome; logic covered in src/utils/saveSetup.test.ts */
    return applyLaunchGateSetupOutcome(resolveSaveSetupOutcome(setupInfo), {
      rid,
      confirmSlotChoice,
      toast: (body) => toaster.toast({ title: "RomM Save Sync", body }),
      dispatchSavesTab: () =>
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } })),
    });
  };

  // Detects emulator core change since last launch; if changed, surfaces the
  // core-change confirm modal. Returns true to proceed, false to bail.
  const confirmCoreChangeIfNeeded = async (rid: number): Promise<boolean> => {
    const coreCheck = await checkCoreChange(rid).catch(
      (): { changed: boolean; old_core?: string; new_core?: string; old_label?: string; new_label?: string } => ({
        changed: false,
      }),
    );
    if (!coreCheck.changed) return true;
    return showCoreChangeModal(
      coreCheck.old_label ?? coreCheck.old_core ?? "Unknown",
      coreCheck.new_label ?? coreCheck.new_core ?? "Unknown",
    );
  };

  // Sync-error confirmation: ask the user whether to launch despite a failed
  // pre-launch sync. Centralises the toast strings so timeout vs result-errors
  // share copy.
  const confirmFallbackLaunch = async (): Promise<boolean> => {
    return showLaunchConfirmation(
      "Save Sync Unavailable",
      "Couldn't sync saves with RomM server. Launch with local saves?",
    );
  };

  // Runs preLaunchSync with the 15s timeout, walks conflicts, and posts the
  // success toast on a non-empty sync. Returns:
  //   "proceed" — launch may continue
  //   "conflict" — user cancelled a conflict modal; button state set to "conflict"
  //   "abort" — user cancelled the fallback launch confirm; caller bails to "play"
  const runPreLaunchSync = async (rid: number): Promise<"proceed" | "conflict" | "abort"> => {
    setState("syncing");
    try {
      const result = await Promise.race([
        preLaunchSync(rid),
        new Promise<never>((_, reject) => setTimeout(() => reject(new Error("timeout")), 15000)),
      ]);

      detach(
        debugLog(
          `CustomPlayButton: preLaunchSync result: synced=${result.synced} conflicts=${result.conflicts?.length ?? 0} success=${result.success}`,
        ),
      );

      // Benign skip (#239): RetroArch writes saves to the content dir, so sync
      // is unsupported. NOT a failure — proceed to launch silently (no toast,
      // no fallback-launch confirm). The "Save sync off" banner in
      // RomMPlaySection already informs the user; nagging on every launch would
      // be noise.
      if (result.reason === SAVEFILES_IN_CONTENT_DIR_REASON) {
        detach(debugLog("CustomPlayButton: pre-launch sync skipped (savefiles_in_content_dir) — launching"));
        return "proceed";
      }

      if (result.conflicts && result.conflicts.length > 0) {
        const conflictResult = await handleConflicts(result.conflicts);
        if (conflictResult === "cancel") {
          setState("conflict");
          return "conflict";
        }
        // Conflicts resolved — notify sibling components to refresh
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: rid } }));
      }

      if (!result.success && result.errors && result.errors.length > 0) {
        detach(debugLog(`CustomPlayButton: pre-launch sync errors: ${result.errors.join(", ")}`));
        const proceed = await confirmFallbackLaunch();
        return proceed ? "proceed" : "abort";
      }
      if (result.synced && result.synced > 0) {
        toaster.toast({ title: "RomM Save Sync", body: "Saves synced with RomM" });
      }
      return "proceed";
    } catch (e) {
      detach(debugLog(`CustomPlayButton: pre-launch sync failed: ${e}`));
      const proceed = await confirmFallbackLaunch();
      return proceed ? "proceed" : "abort";
    }
  };

  // Final launch step — set state and hand off to Steam.
  const dispatchLaunch = (gameId: string) => {
    setState("launching");
    SteamClient.Apps.RunGame(gameId, "", -1, 100);
  };

  // Coordinator: runs each pre-launch gate in sequence, bailing on the first
  // cancel. All branch-specific UI lives in the helpers above.
  // S3776 is raised on the declaration line, so its NOSONAR must stay there. prettier-ignore stops
  // Prettier from relocating the trailing comment into the body (which would break the suppression).
  // prettier-ignore
  const handlePlay = async () => { // NOSONAR(typescript:S3776) — gate chain coordinator; decomposed into gate helpers in #389. Remaining cc is inherent to gate logic.
    if (state === "syncing" || state === "launching") return; // debounce
    const overview = appStore.GetAppOverviewByAppID(appId);
    const gameId = overview?.GetGameID?.() ?? String(appId);
    detach(debugLog(`CustomPlayButton: handlePlay appId=${appId} gameId=${gameId}`));

    // Pre-launch save sync
    if (romId) {
      if (getRommConnectionState() === "offline") {
        // RomM offline — warn user, skip sync attempt entirely
        const proceed = await confirmOfflineLaunch();
        if (!proceed) {
          setState("play");
          return;
        }
      } else {
        // Check save slot tracking is configured
        const trackingOutcome = await ensureTrackingConfigured(romId);
        if (trackingOutcome === "abort") {
          setState("play");
          return;
        }

        // Check for core change before syncing
        const coreOk = await confirmCoreChangeIfNeeded(romId);
        if (!coreOk) {
          setState("play");
          return;
        }

        const syncOutcome = await runPreLaunchSync(romId);
        if (syncOutcome === "conflict") return; // state already set to "conflict"
        if (syncOutcome === "abort") {
          setState("play");
          return;
        }
      }
    }

    dispatchLaunch(gameId);
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
      detach(debugLog(`CustomPlayButton: resolve conflict failed: ${e}`));
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

  // Cancel an in-flight download. Fire-and-forget: the backend emits a
  // cancelled download_progress frame that the progress listener reacts to
  // (resets to "download"). The inline .catch keeps the click non-throwing.
  const handleCancelDownload = () => {
    if (romId == null) return;
    detach(cancelDownload(romId).catch(() => {}));
  };

  // Pause an in-flight (resumable) download. Fire-and-forget: the backend
  // freezes the transfer and emits a "paused" download_progress frame the
  // listener reacts to (sets dlProgress.paused). .catch keeps the click safe.
  const handlePause = () => {
    if (romId == null) return;
    detach(pauseDownload(romId).catch(() => {}));
  };

  // Resume a paused download. Fire-and-forget: the backend re-begins the
  // transfer from the partial .tmp and emits "downloading" frames the listener
  // reacts to (clears the paused flag). .catch keeps the click safe.
  const handleResume = () => {
    if (romId == null) return;
    detach(resumeDownload(romId).catch(() => {}));
  };

  const handleUninstall = async () => {
    if (!romId) return;
    detach(debugLog(`CustomPlayButton: uninstalling romId=${romId}`));
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
        <MenuItem
          key="uninstall"
          tone="destructive"
          onClick={() => {
            detach(handleUninstall());
          }}
        >
          Uninstall
        </MenuItem>
      </Menu>,
      getEventTarget(e),
    );
  };

  // Pause/Resume + Cancel menu for a resumable download. When the transfer is
  // paused the primary entry is Resume; otherwise it's Pause. Cancel is always
  // offered.
  const showDownloadActionsMenu = (e: MouseEvent, paused: boolean) => {
    showContextMenu(
      <Menu label="Download Actions">
        {paused ? (
          <MenuItem key="resume" onClick={handleResume}>
            Resume
          </MenuItem>
        ) : (
          <MenuItem key="pause" onClick={handlePause}>
            Pause
          </MenuItem>
        )}
        <MenuItem key="cancel" tone="destructive" onClick={handleCancelDownload}>
          Cancel
        </MenuItem>
      </Menu>,
      getEventTarget(e),
    );
  };

  // Don't render for non-RomM games
  if (state === "not_romm" || state === "loading") {
    detach(debugLog(`CustomPlayButton: returning null (state=${state})`));
    return null;
  }
  detach(debugLog(`CustomPlayButton: rendering state=${state}`));

  // Dropdown arrow button style. Shared shape for the play-state chevron and
  // the download-state cancel X — both are 36px side actions on the right.
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
        className={[appActionButtonClasses?.PlayButtonContainer, appActionButtonClasses?.Green]
          .filter(Boolean)
          .join(" ")}
        style={btnContainerStyle}
      >
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", "romm-dl-complete-flash"]
            .filter(Boolean)
            .join(" ")}
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
    const t = dlProgress && dlProgress.totalBytes > 0 ? dlProgress.bytesDownloaded / dlProgress.totalBytes : 0;
    const downloading = actionPending && dlProgress;
    const paused = downloading ? dlProgress.paused : false;
    const resumable = downloading ? dlProgress.resumable : false;

    // Fill color shifts from blue to green as download progresses
    const fillColor = downloading
      ? `linear-gradient(to right, ${lerpColor(BLUE_LEFT, GREEN_LEFT, t)}, ${lerpColor(BLUE_RIGHT, GREEN_RIGHT, t)})`
      : "linear-gradient(to right, #1a9fff, #0078d4)";

    // Pulse color shifts from blue to green with progress; a paused download
    // freezes to a dim amber so the whole group reads as "halted, not running".
    let pulseColor: string;
    if (paused) {
      pulseColor = "rgba(212,167,44,0.7)";
    } else if (downloading) {
      pulseColor = lerpColor(BLUE_LEFT, GREEN_LEFT, t);
    } else {
      pulseColor = "rgba(26,159,255,0.7)";
    }

    let dlLabel: string;
    if (paused) {
      dlLabel = "Paused";
    } else if (downloading) {
      dlLabel = formatProgress(dlProgress.bytesDownloaded, dlProgress.totalBytes);
    } else if (actionPending) {
      dlLabel = "Starting...";
    } else {
      dlLabel = "Download";
    }

    // Unfilled portion: darker shade of the current fill color
    let baseBg: string;
    if (isOffline) {
      baseBg = "linear-gradient(to right, #6b7b8b, #5a6a7a)";
    } else if (downloading) {
      baseBg = `linear-gradient(to right, ${lerpColor([10, 50, 90], [5, 35, 65], t)}, ${lerpColor([5, 35, 65], [5, 50, 30], t)})`;
    } else {
      baseBg = "linear-gradient(to right, #1a9fff, #0078d4)";
    }

    // While a download is actively running, the main button shares the row
    // with a right-side action section (the cancel X or a Pause/Resume
    // dropdown). Square off its right edge so it butts cleanly against that
    // section; idle/starting keeps the full pill radius. The pulse animation
    // lives on the container (romm-dl-active-group) so it spans the whole
    // control — button + action — as one cohesive pulsing group.
    const downloadBtn = (
      <DialogButton
        className={[appActionButtonClasses?.PlayButton, "romm-btn-download"].filter(Boolean).join(" ")}
        style={{
          ...mainBtnStyle,
          borderRadius: downloading ? "2px 0 0 2px" : "2px",
          background: baseBg,
        }}
        onClick={() => {
          detach(handleDownload());
        }}
        disabled={actionPending || isOffline}
      >
        {/* Progress fill bar — kept at its frozen width while paused. */}
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
    );

    if (!downloading) {
      // Idle ("Download") or "Starting..." — single full-width button, no action.
      return (
        <Focusable ref={containerRef} className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
          {downloadBtn}
        </Focusable>
      );
    }

    const cancelX = (
      <DialogButton
        className="romm-btn-cancel"
        aria-label="Cancel download"
        title="Cancel download"
        style={{
          ...dropdownArrowStyle,
          background: "linear-gradient(to right, #0a3a5a, #062a45)",
          color: "#fff",
        }}
        onClick={handleCancelDownload}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M1 1L11 11M11 1L1 11"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </svg>
      </DialogButton>
    );

    // Resumable downloads (live or paused) get a dropdown chevron whose menu
    // offers Pause/Resume + Cancel; non-resumable downloads keep the direct
    // cancel X (the #1122 behavior — multi-file zips and Cloudflare can't
    // resume, so there's nothing to pause).
    const dropdown = (
      <DialogButton
        className="romm-btn-cancel"
        aria-label="Download actions"
        title="Download actions"
        style={{
          ...dropdownArrowStyle,
          background: paused
            ? "linear-gradient(to right, #6b5a1f, #4d4015)"
            : "linear-gradient(to right, #0a3a5a, #062a45)",
          color: "#fff",
        }}
        onClick={(e: MouseEvent) => showDownloadActionsMenu(e, paused)}
      >
        <svg width="12" height="8" viewBox="0 0 12 8" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M1 1.5L6 6.5L11 1.5"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </DialogButton>
    );

    // Active download: button + a right-side action section. The section is a
    // flex sub-container so the dropdown-vs-X choice is a clean conditional.
    // The pulse runs on the container so it spans the whole group.
    return (
      <Focusable
        ref={containerRef}
        className={[appActionButtonClasses?.PlayButtonContainer, "romm-dl-active-group"].filter(Boolean).join(" ")}
        style={{ ...btnContainerStyle, "--romm-pulse-color": pulseColor } as React.CSSProperties}
      >
        {downloadBtn}
        <div style={{ display: "flex", flexDirection: "row", height: "100%" }}>{resumable ? dropdown : cancelX}</div>
      </Focusable>
    );
  }

  if (state === "uninstalling") {
    return (
      <Focusable className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-download", "romm-dl-uninstall-flash"]
            .filter(Boolean)
            .join(" ")}
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
      <Focusable className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"]
            .filter(Boolean)
            .join(" ")}
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
          <span>Launching...</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "syncing") {
    return (
      <Focusable className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"]
            .filter(Boolean)
            .join(" ")}
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
          <span>Syncing saves...</span>
        </DialogButton>
      </Focusable>
    );
  }

  if (state === "conflict") {
    return (
      <Focusable ref={containerRef} className={appActionButtonClasses?.PlayButtonContainer} style={btnContainerStyle}>
        <DialogButton
          className={[appActionButtonClasses?.PlayButton, "romm-btn-conflict"].filter(Boolean).join(" ")}
          style={{
            ...mainBtnStyle,
            borderRadius: "2px",
            background: "linear-gradient(to right, #d4a72c, #b8941f)",
          }}
          onClick={() => {
            detach(handleResolveConflict());
          }}
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
      className={[appActionButtonClasses?.PlayButtonContainer, !isOffline && appActionButtonClasses?.Green]
        .filter(Boolean)
        .join(" ")}
      style={btnContainerStyle}
    >
      <DialogButton
        className={[appActionButtonClasses?.PlayButton, "romm-btn-play", isOffline && "romm-offline"]
          .filter(Boolean)
          .join(" ")}
        style={{
          ...mainBtnStyle,
          borderRadius: "2px 0 0 2px",
          background: playBg,
          backgroundPosition: "25%",
          backgroundSize: "330% 100%",
        }}
        onClick={() => {
          detach(handlePlay());
        }}
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
        onFocus={scrollToTop}
      >
        <svg width="12" height="8" viewBox="0 0 12 8" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M1 1.5L6 6.5L11 1.5"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </DialogButton>
    </Focusable>
  );
};
