import { definePlugin, addEventListener, removeEventListener, toaster } from "@decky/api";
import { useState, useRef, useEffect, FC, type ReactNode } from "react";
import { FaGamepad } from "react-icons/fa";
import { MainPage } from "./components/MainPage";
import { SettingsPage } from "./components/SettingsPage";
import { LibraryPage } from "./components/LibraryPage";
import { SystemPage } from "./components/SystemPage";
import { DangerZone } from "./components/DangerZone";
import { DownloadQueue } from "./components/DownloadQueue";
import { initUnitSyncManager, resetSyncCancel } from "./utils/syncManager";
import { setSyncProgress, getSyncProgress, updateSyncProgress } from "./utils/syncProgress";
import { estimatePlanSeconds } from "./utils/syncEstimate";
import { beginEtaRun } from "./utils/syncEta";
import { updateDownload, getDownloadState, removeDownload } from "./utils/downloadStore";
import { handleGlobalDownloadFailure } from "./utils/downloadFailure";
import {
  registerGameDetailPatch,
  unregisterGameDetailPatch,
  registerRomMAppId,
  unregisterRomMAppId,
} from "./patches/gameDetailPatch";
import {
  registerMetadataPatches,
  unregisterMetadataPatches,
  applyAllPlaytime,
  applyAllMetadata,
} from "./patches/metadataPatches";
import { registerLaunchInterceptor, unregisterLaunchInterceptor } from "./utils/launchInterceptor";
import { showCoreChangeModal } from "./components/CoreChangeModal";
import { handleConflicts } from "./components/SyncConflictModal";
import { showOfflineDriftModal } from "./components/OfflineDriftModal";
import { showFallbackLaunchModal } from "./components/FallbackLaunchModal";
import { hasAnySaveConflict } from "./utils/saveStatus";
import {
  getAppIdRomIdMap,
  ensureDeviceRegistered,
  getSaveSyncSettings,
  getAllPlaytime,
  getMigrationStatus,
  getSaveSortMigrationStatus,
  getInstalledRelaunchOptions,
  testConnection,
  invalidateCachedGameDetail,
  logError,
  logInfo,
  waitForPruneRelease,
} from "./api/backend";
import {
  createOrUpdateCollections,
  createOrUpdateRomMCollections,
  clearPlatformCollection,
  getHostname,
} from "./utils/collections";
import { setMigrationStatus } from "./utils/migrationStore";
import { fetchSettingsResetState } from "./utils/settingsResetStore";
import { resetSyncDelta, recordSyncRemoved, getSyncDelta } from "./utils/syncDeltaStore";
import { setSaveSortMigrationStatus } from "./utils/saveSortMigrationStore";
import { setVersionError, setServerRetryProgress } from "./utils/connectionState";
import { initSessionManager, destroySessionManager } from "./utils/sessionManager";
import { findOutermostScrollParent } from "./utils/scrollHelpers";
import { detach } from "./utils/detach";
import type {
  SyncProgress,
  DownloadProgressEvent,
  DownloadCompleteEvent,
  DownloadFailedEvent,
  SaveStatus,
  SyncPlanData,
  SyncStaleData,
  SyncCollectionsData,
  ServerRetryProgressEvent,
} from "./types";
import { setLaunchOptionsConfirmed } from "./utils/steamShortcuts";
import { removeShortcutsPaced } from "./utils/shortcutRemoval";
import { batchConfirmLaunchOptions } from "./utils/launchOptionsReconcile";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseCancelled,
  mountPruneLeasePlugin,
  releaseAllPruneLeases,
  releasePruneLease,
  withPruneLease,
} from "./utils/pruneLease";
import { withTimeout } from "./utils/withTimeout";
import { fetchMetadataCachePages } from "./utils/metadataCache";
import { cancelPruneActions, handlePruneAction } from "./utils/pruneActions";
import type { PruneActionRequired } from "./utils/pruneActions";
import { admitPruneFrame, setPruneComplete, setPruneProgress } from "./utils/pruneStore";
import type { PruneComplete, PruneProgress } from "./utils/pruneStore";
import { publishCommittedVersionSwitch } from "./utils/versionSwitchApplication";

type Page = "main" | "settings" | "library" | "data" | "downloads" | "system";

// Module-level page state survives QAM remounts (e.g. after modal close)
let currentPage: Page = "main";

const QAMPanel: FC = () => {
  const [page, setPageState] = useState<Page>(currentPage); // NOSONAR(typescript:S6754) — setter intentionally renamed; setPage wraps it below to provide custom navigation behavior.
  const rootRef = useRef<HTMLDivElement>(null);
  const setPage = (p: Page) => {
    currentPage = p;
    setPageState(p);
  };

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const container = findOutermostScrollParent(el);
    // rAF lets the new page mount/measure before we set scrollTop.
    const rafHandle = requestAnimationFrame(() => {
      if (container) container.scrollTop = 0;
    });
    // Steam's gamepad nav retains a focus pointer across page swaps and
    // resolves it on the next input — landing on a button at the old page's
    // position. Force focus to the first button so navigation starts at the
    // top. Same querySelector + .focus() + gpfocus pattern as CustomPlayButton.
    const focusTimer = setTimeout(() => {
      const btn = el.querySelector("button");
      if (btn) {
        btn.focus();
        btn.classList.add("gpfocus");
      }
    }, 50);
    return () => {
      cancelAnimationFrame(rafHandle);
      clearTimeout(focusTimer);
    };
  }, [page]);

  let content: ReactNode;
  switch (page) {
    case "settings":
      content = <SettingsPage onBack={() => setPage("main")} />;
      break;
    case "library":
      content = <LibraryPage onBack={() => setPage("main")} />;
      break;
    case "data":
      content = <DangerZone onBack={() => setPage("main")} />;
      break;
    case "downloads":
      content = <DownloadQueue onBack={() => setPage("main")} />;
      break;
    case "system":
      content = <SystemPage onBack={() => setPage("main")} />;
      break;
    default:
      content = <MainPage onNavigate={(p) => setPage(p)} />;
  }

  return <div ref={rootRef}>{content}</div>;
};

/**
 * Build the completion-toast body (and optional on-screen duration) for a
 * finished sync run from the run outcome and the true created/removed delta.
 */
function buildSyncCompleteToast(
  data: { interrupt_reason?: string; interrupted?: boolean; cancelled?: boolean; restart_recommended?: boolean },
  delta: { added: number; removed: number },
): { body: string; duration?: number } {
  // Report the TRUE delta, not the total processed set. The library applies
  // whole platforms, so total_games is not a real delta — the exact, honest
  // counts are the shortcuts the frontend actually created and removed this
  // run (tracked in syncDeltaStore, deduplicated across platform/collection
  // units). Omit a zero part; "Library up to date." when nothing changed.
  const parts: string[] = [];
  if (delta.added > 0) parts.push(`${delta.added} added`);
  if (delta.removed > 0) parts.push(`${delta.removed} removed`);
  const summary = parts.join(", ");
  if (data.interrupt_reason) {
    // A session-budget pause carries its own resume-friendly guidance — show it
    // verbatim, appending the delta so the user sees what did get saved. Strip
    // the reason's trailing period so the parenthetical reads as one sentence:
    // "…then Resume Sync (2 added so far)." not "…Resume Sync. (2 added so far.)".
    const body = summary ? `${data.interrupt_reason.replace(/\.$/, "")} (${summary} so far).` : data.interrupt_reason;
    // A session-budget pause needs the full guidance readable — persistent QAM
    // banners carry the numbers, but the toast is the immediate cue, so give it a
    // longer on-screen duration than the default so the guidance isn't truncated
    // away before it is read (#1383).
    return { body, duration: 15000 };
  }
  if (data.interrupted) {
    // A heartbeat-timeout run (an external death — frontend crash/reload) rides
    // the cancelled finalize with this additive flag; word the toast honestly
    // instead of blaming a Cancel the user never pressed (#1384).
    return { body: summary ? `Sync interrupted — ${summary} so far.` : "Sync interrupted." };
  }
  if (data.cancelled) {
    return { body: summary ? `Sync cancelled — ${summary} so far.` : "Sync cancelled." };
  }
  let body = summary ? `Sync complete — ${summary}.` : "Library up to date.";
  if (data.restart_recommended) {
    body += " Steam restart recommended before further large operations.";
  }
  return { body };
}

const ROMM_COLLECTION_NAME = /^RomM: \[([^\]]+)\]/;
const COLLECTION_HOST_SUFFIX = /\s\([^)]+\)$/;

/** The platform a "RomM: <platform> (host)" collection is named after. */
function platformOfCollection(displayName: string): string {
  return displayName.slice(6).replace(COLLECTION_HOST_SUFFIX, "");
}

/** Whether a platform collection on this host no longer has a platform behind it. */
function isStalePlatformCollection(displayName: string, suffix: string, activePlatforms: Set<string>): boolean {
  if (!displayName.startsWith("RomM: ") || displayName.slice(6).startsWith("[")) return false;
  if (!displayName.endsWith(suffix)) return false;
  return !activePlatforms.has(platformOfCollection(displayName).toLowerCase());
}

/** Whether a "RomM: [name] (host)" collection on this host no longer has a RomM collection behind it. */
function isStaleRomMCollection(displayName: string, suffix: string, activeNames: Set<string>): boolean {
  if (!displayName.startsWith("RomM: [") || !displayName.endsWith(suffix)) return false;
  const match = ROMM_COLLECTION_NAME.exec(displayName);
  return match ? !activeNames.has(match[1]!.toLowerCase()) : false;
}

/**
 * Drop this host's RomM collections whose source is gone. Scoped by the hostname
 * suffix so a Steam-Cloud-synced collection belonging to another device is never
 * touched.
 */
async function removeStaleCollections(
  activePlatformNames: string[],
  activeCollectionNames: string[],
  signal: AbortSignal,
): Promise<void> {
  const hostname = await getHostname();
  if (isPruneLeaseCancelled(signal)) return;
  const suffix = ` (${hostname})`;
  const activePlatforms = new Set(activePlatformNames.map((name) => name.toLowerCase()));
  for (const collection of collectionStore.userCollections.filter((candidate) =>
    isStalePlatformCollection(candidate.displayName, suffix, activePlatforms),
  )) {
    const platformName = platformOfCollection(collection.displayName);
    logInfo(`Removing stale platform collection "${collection.displayName}"`);
    await clearPlatformCollection(platformName, signal);
    if (isPruneLeaseCancelled(signal)) return;
  }
  const activeNames = new Set(activeCollectionNames.map((name) => name.toLowerCase()));
  for (const collection of collectionStore.userCollections.filter((candidate) =>
    isStaleRomMCollection(candidate.displayName, suffix, activeNames),
  )) {
    logInfo(`Removing stale RomM collection "${collection.displayName}"`);
    if (isPruneLeaseCancelled(signal)) return;
    await collection.Delete();
  }
}

/** Publish each committed version switch in order, stopping the moment the lease is cancelled. */
async function publishSwitchesUntilCancelled(
  pending: Array<{ appId: number; romId: number }>,
  signal: AbortSignal,
): Promise<void> {
  for (const item of pending) {
    if (isPruneLeaseCancelled(signal)) return;
    await publishCommittedVersionSwitch(item.appId, item.romId, undefined, signal);
  }
}

/** Name a committed shortcut action the way the completion toast says it. */
function shortcutActionLabel(committedAction: string | undefined): string {
  return committedAction === "remove_shortcut" ? "Shortcut removal" : "Shortcut repoint";
}

/**
 * Say what a finished cleanup run did, leading with anything that committed a
 * Steam change without finishing: an uncertain outcome and a committed-but-
 * incomplete one both matter more to the user than the count of rows removed.
 */
function buildPruneCompleteToast(completed: PruneComplete): { body: string; subtext: string | undefined } {
  const ambiguousPartial = completed.results.find((item) => item.status === "partial" && item.action_ambiguous);
  const committedPartial = completed.results.find((item) => item.status === "partial" && item.committed_action);
  const subtext = ambiguousPartial?.message ?? committedPartial?.message ?? completed.message;
  if (ambiguousPartial) {
    const label = shortcutActionLabel(ambiguousPartial.committed_action);
    return { body: `${label} outcome is uncertain; source data was retained.`, subtext };
  }
  if (committedPartial) {
    const label = shortcutActionLabel(committedPartial.committed_action);
    return { body: `${label} committed; local cleanup incomplete.`, subtext };
  }
  const removed = completed.removed_count ?? completed.removed_rom_ids.length;
  if (!removed) return { body: completed.message || "No removed RomM games were cleaned up.", subtext };
  const skipped =
    completed.problem_count ??
    completed.results.filter((item) => ["failed", "skipped", "partial"].includes(item.status)).length;
  const skippedNote = skipped ? `; ${skipped} group(s) skipped` : "";
  const plural = removed === 1 ? "y" : "ies";
  return { body: `Removed ${removed} local entr${plural}${skippedNote}.`, subtext };
}

/** Register every appId in *map* (values are per-key appId arrays) as RomM-owned. */
function registerAppIds(map: Record<string, number[]>): void {
  for (const appIds of Object.values(map)) {
    for (const appId of appIds) {
      registerRomMAppId(appId);
    }
  }
}

export default definePlugin(() => {
  mountPruneLeasePlugin();
  const pluginAdmission = capturePruneLeaseAdmission();
  registerGameDetailPatch();
  registerLaunchInterceptor({
    confirmCoreChange: showCoreChangeModal,
    resolveConflicts: handleConflicts,
    askOfflineDrift: showOfflineDriftModal,
    confirmFallbackLaunch: showFallbackLaunchModal,
  });

  // Load metadata cache, register store patches, and populate RomM app ID set.
  // Retries with backoff if the backend isn't ready yet (e.g. boot without network).
  // callable() has no timeout — hangs forever if backend isn't ready — so we race
  // each attempt against a deadline to ensure retries actually fire.
  const RETRY_DELAYS = [2000, 5000, 10000, 15000, 20000];
  const CALLABLE_TIMEOUT = 5000;
  // Metadata is paged so a large library never sends a multi-MB dump through the
  // size-limited WebSocket bridge in a single callable response (#1025).
  const METADATA_PAGE_SIZE = 500;
  let initAttempt = 0;
  let initDone = false;
  let syncContinuationController = new AbortController();
  let staleRemovalTail: Promise<void> = Promise.resolve();

  async function loadAppIdsAndMetadata() {
    const appIdMap = await withTimeout(getAppIdRomIdMap(), CALLABLE_TIMEOUT);

    // Page the metadata cache until every row is collected. A failed page throws
    // out of the shared loop (each raced against the per-callable deadline) and
    // the outer retry loop restarts init from offset 0.
    const cache = await fetchMetadataCachePages(METADATA_PAGE_SIZE, CALLABLE_TIMEOUT);

    registerMetadataPatches(cache, appIdMap);

    for (const appIdStr of Object.keys(appIdMap)) {
      const appId = Number.parseInt(appIdStr, 10);
      if (!Number.isNaN(appId)) {
        registerRomMAppId(appId);
      }
    }

    // Apply the overview mutations (controller badge / rating / categories) with a
    // readiness retry — appStore is rebuilt each mount and may not hold our
    // overviews yet, so a single pass silently no-ops on a cold boot (#1203).
    // Detached so the retry's backoff doesn't delay init completion.
    detach(applyAllMetadata());

    try {
      const { playtime } = await withTimeout(getAllPlaytime(), CALLABLE_TIMEOUT);
      await applyAllPlaytime(playtime, appIdMap);
    } catch (e) {
      // Use console — logError is a callable that may also hang
      console.warn("[RomM] Failed to apply playtime:", e);
    }

    initDone = true;
    // Backend is now reachable — log via callable so it appears in plugin log
    const attempts = initAttempt + 1;
    if (attempts > 1) {
      logInfo(`App ID init succeeded after ${attempts} attempts (backend was slow to start)`);
    } else {
      logInfo(`App ID init succeeded (attempt 1)`);
    }
  }

  detach(
    (async () => {
      // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- `initDone` is flipped to true inside the awaited `loadAppIdsAndMetadata()`; TS's control-flow analysis can't see that cross-function mutation and narrows it to the `false` literal here. The guard is the real loop-exit: on success `initAttempt` is never incremented (only the catch bumps it), so without `!initDone` the loop would spin forever.
      while (!initDone && initAttempt < RETRY_DELAYS.length + 1) {
        try {
          await loadAppIdsAndMetadata();
        } catch {
          if (initAttempt < RETRY_DELAYS.length) {
            await new Promise((r) => setTimeout(r, RETRY_DELAYS[initAttempt]));
          }
          initAttempt++;
        }
      }
      // After backend reachability is confirmed, reconcile launch_options for
      // all installed+bound ROMs to heal any drift from a missed bake (#1043).
      // This lease-issuing call must stay behind the awaited init round-trips:
      // the orphan-lease disown dispatched at definePlugin entry has to land
      // before any lease is issued to this mount — a lease issued earlier
      // would be disowned while live, and its refused renewal would abort the
      // continuation's Steam work mid-flight.
      // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- `initDone` is flipped to true inside the awaited `loadAppIdsAndMetadata()`; TS's control-flow analysis can't see that cross-function mutation and narrows it to the `false` literal here. The guard is real: it gates the reconcile on the loop having actually reached a reachable backend.
      if (initDone) {
        try {
          const result = await getInstalledRelaunchOptions();
          if (result.success) {
            await withPruneLease(
              result.prune_lease_token,
              "Startup reconcile",
              (signal) => batchConfirmLaunchOptions(result.items, "startup_reconcile", signal),
              "Startup reconcile",
              pluginAdmission,
            );
          }
        } catch (e) {
          logError(`startup_reconcile: failed to reconcile launch options: ${e}`);
        }
      }
    })(),
  );

  // Early version check — populate version error state before any game detail page renders.
  // Retries are handled by MainPage and RomMPlaySection via their own testConnection() calls.
  detach(
    (async () => {
      try {
        const result = await withTimeout(testConnection(), CALLABLE_TIMEOUT);
        if (result.reason === "version_error") {
          setVersionError(result.message);
        } else if (result.success) {
          setVersionError(null);
        }
      } catch {
        // Silent — other components will retry; don't block startup on connection failure
      }
    })(),
  );

  // Check for pending RetroDECK path migration on startup. The QAM block page
  // and game-detail card surface this to the user — no toast needed.
  detach(
    (async () => {
      try {
        const status = await getMigrationStatus();
        if (status.pending) {
          setMigrationStatus(status);
        }
      } catch (e) {
        logError(`Failed to check migration status: ${e}`);
      }
    })(),
  );

  // Check for pending save sort migration on startup
  detach(
    (async () => {
      try {
        const status = await getSaveSortMigrationStatus();
        if (status.pending) {
          setSaveSortMigrationStatus(status);
          toaster.toast({
            title: "RomM Sync",
            body: "RetroArch save sorting changed. Go to Settings to migrate save files.",
          });
        }
      } catch (e) {
        logError(`Failed to check save sort migration status: ${e}`);
      }
    })(),
  );

  // Surface a corrupt-settings reset that happened at boot. The backend backs
  // up an unparseable settings.json to settings.json.corrupt-<ts>, resets to
  // defaults, and persists a marker that survives reloads. The QAM banner and
  // game-detail card surface this to the user — no toast needed; the notice
  // stays up until the next successful sign-in clears the marker.
  detach(
    (async () => {
      try {
        await fetchSettingsResetState();
      } catch (e) {
        logError(`Failed to check settings reset notice: ${e}`);
      }
    })(),
  );

  // Register device and initialize session manager for save sync (if enabled)
  detach(
    (async () => {
      try {
        const syncSettings = await getSaveSyncSettings();
        if (syncSettings.save_sync_enabled) {
          await ensureDeviceRegistered();
        }
        // Always init session manager — it handles playtime tracking too
        await initSessionManager();
      } catch (e) {
        logError(`Failed to init save sync: ${e}`);
      }
    })(),
  );

  const onSyncComplete = (data: {
    platform_app_ids: Record<string, number[]>;
    romm_collection_app_ids?: Record<string, number[]>;
    total_games: number;
    cancelled?: boolean;
    /**
     * Set alongside `cancelled` when the run ended on a heartbeat timeout — an
     * external death (frontend crash/reload), not the user's Cancel — so the
     * completion toast says "interrupted" instead of "cancelled" (#1384).
     */
    interrupted?: boolean;
    /**
     * Present only when the run paused itself at a chunk boundary because
     * Steam's renderer is near its per-session heap budget (#1383). A
     * full-sentence, resume-friendly guidance string shown verbatim instead of
     * the generic "cancelled" wording.
     */
    interrupt_reason?: string;
    /**
     * Set on a CLEAN run whose post-run renderer RSS is high enough that the
     * next large operation would likely pause or crash — the UI appends a
     * "restart Steam" nudge to the completion toast (#1383).
     */
    restart_recommended?: boolean;
    prune_lease_token?: string;
  }) => {
    const syncAdmission = capturePruneLeaseAdmission();
    logInfo(`sync_complete received: ${data.total_games} games, cancelled=${data.cancelled ?? false}`);

    const { body, duration } = buildSyncCompleteToast(data, getSyncDelta());
    toaster.toast({ title: "RomM Sync", body, ...(duration !== undefined ? { duration } : {}) });

    // Drive the terminal UI teardown from ``sync_complete`` — the guaranteed
    // terminal signal. The backend ALSO emits a separate stage:"done"/"cancelled"
    // sync_progress frame, but that second frame can be dropped or raced (e.g. a
    // failure in the post-complete bound-count read between the two emits),
    // leaving the QAM stuck on the optimistic "Applying" frame. Flip the store to
    // a terminal stage here (merge — keeps the fine fields) so MainPage's
    // onSyncProgressChange tears the in-progress UI down regardless.
    updateSyncProgress({ running: false, stage: data.cancelled ? "cancelled" : "done" });

    // Covers are applied per created shortcut during the run through Steam's
    // artwork API (syncManager.applyCoverArtwork), so tiles show their real cover
    // in-session as they are created — no end-of-run sweep or client restart
    // needed. The backend also writes each {app_id}p.png grid file at commit as the
    // durability net.

    // Defensive reset; sync_plan also resets at the start of the next run.
    resetSyncDelta();

    // Update RomM app ID set with newly synced shortcuts. The unit-ack
    // registration in syncManager is the primary path (registers each appId the
    // moment it resolves); this is the redundant, no-op-safe net for any appId
    // the unit loop didn't reach. Iterate BOTH maps: a collection-only sync
    // touches only romm_collection_app_ids, so skipping it would leave those
    // shortcuts unregistered until a Steam restart (#1205).
    registerAppIds(data.platform_app_ids);
    registerAppIds(data.romm_collection_app_ids ?? {});

    const reconcileLaunchOptions = async (signal: AbortSignal): Promise<void> => {
      try {
        const result = await getInstalledRelaunchOptions();
        if (result.success) {
          await withPruneLease(
            result.prune_lease_token,
            "Installed reconcile",
            (innerSignal) => {
              if (isPruneLeaseCancelled(innerSignal)) return Promise.resolve();
              return batchConfirmLaunchOptions(result.items, "sync_reconcile", signal);
            },
            "Installed reconcile",
            syncAdmission,
          );
        }
      } catch (e) {
        logError(`sync_reconcile: failed to reconcile launch options: ${e}`);
      }
    };

    const reconcileCollections = async (signal: AbortSignal): Promise<void> => {
      try {
        if (Object.keys(data.platform_app_ids).length > 0) {
          await createOrUpdateCollections(data.platform_app_ids, undefined, signal);
        }
        if (isPruneLeaseCancelled(signal)) return;
        if (data.romm_collection_app_ids && Object.keys(data.romm_collection_app_ids).length > 0) {
          await createOrUpdateRomMCollections(data.romm_collection_app_ids, undefined, signal);
        }
        if (isPruneLeaseCancelled(signal)) return;
        if (!data.cancelled && typeof collectionStore !== "undefined") {
          await removeStaleCollections(
            Object.keys(data.platform_app_ids),
            Object.keys(data.romm_collection_app_ids ?? {}),
            signal,
          );
        }
      } catch (e) {
        logError(`Failed to manage RomM collections: ${e}`);
      }
    };

    const reconcilePlaytime = async (signal: AbortSignal): Promise<void> => {
      try {
        const [{ playtime }, appIdMap] = await Promise.all([getAllPlaytime(), getAppIdRomIdMap()]);
        if (isPruneLeaseCancelled(signal)) return;
        await applyAllPlaytime(playtime, appIdMap, signal);
      } catch (e) {
        logError(`Failed to re-apply playtime after sync: ${e}`);
      }
    };

    const reconcileMetadata = async (signal: AbortSignal): Promise<void> => {
      try {
        const [cache, appIdMap] = await Promise.all([
          fetchMetadataCachePages(METADATA_PAGE_SIZE, CALLABLE_TIMEOUT),
          getAppIdRomIdMap(),
        ]);
        if (isPruneLeaseCancelled(signal)) return;
        registerMetadataPatches(cache, appIdMap);
        await applyAllMetadata(signal);
      } catch (e) {
        logError(`Failed to re-apply metadata after sync: ${e}`);
      }
    };

    const continuationController = syncContinuationController;
    const staleRemovals = staleRemovalTail;
    detach(
      withPruneLease(data.prune_lease_token, "Sync completion", async (leaseSignal) => {
        const abort = () => continuationController.abort();
        if (leaseSignal.aborted) abort();
        else leaseSignal.addEventListener("abort", abort, { once: true });
        try {
          const signal = continuationController.signal;
          // Settle semantics, not Promise.all: one rejecting sibling must not
          // release this lease while the others are still writing to Steam.
          await Promise.allSettled([
            staleRemovals,
            reconcileLaunchOptions(signal),
            reconcileCollections(signal),
            reconcilePlaytime(signal),
            reconcileMetadata(signal),
          ]);
        } finally {
          leaseSignal.removeEventListener("abort", abort);
        }
      }),
    );
  };

  const syncCompleteListener = addEventListener<
    [
      {
        platform_app_ids: Record<string, number[]>;
        romm_collection_app_ids?: Record<string, number[]>;
        total_games: number;
      },
    ]
  >("sync_complete", onSyncComplete);

  const syncApplyUnitListener = initUnitSyncManager();

  // Per-unit pipeline: planning + stale + collections events.
  // ``sync_plan`` arrives once per run with the full work queue (info only
  // for now — future PR adds a per-platform progress view).
  const syncPlanListener = addEventListener<[SyncPlanData]>("sync_plan", (data: SyncPlanData) => {
    syncContinuationController.abort();
    syncContinuationController = new AbortController();
    // sync_plan fires once per run, before any unit — reset the per-run delta
    // so the terminal toast counts only this run's created/removed shortcuts.
    resetSyncDelta();
    // Clear the per-run cancel flag once per run, before any unit. Doing it
    // here (not only in the per-unit handler) keeps the flag fresh even on a
    // skip-only run, where no unit handler ever fires (#1198). Run identity for
    // a Cancel click comes from the backend-fed sync_progress store now (#1202).
    resetSyncCancel();
    // Seed the applying-phase estimate on the walk-cost model (the same
    // constants the preview row uses), priced per unit by COMPOSITION rather
    // than as one blended item count (#1511). The plan is skip-aware (#1382):
    // a predicted-skip unit costs nothing and the rest are weighed at their
    // persisted collapsed (post-sibling-group) shortcut count — and of those
    // items, the ones already bound to a Steam shortcut (``bound_count``) take
    // the cheap update path. Pricing every planned item as a create over-read
    // by ~4x on the common case: any re-sync, and — for platforms — every Force
    // Full Sync (which clears the completion stamps but unbinds nothing, so it
    // is all updates). A unit that omits ``bound_count`` still prices as all
    // creates: an older backend, or a collection with no stamped member set,
    // which is every collection on a forced run since that clears the stamps
    // membership is read from. The delta-restricted apply skips unchanged items and the live
    // rate estimator corrects the readout within seconds of applying (#1382-M3).
    // Merged (not replaced) so the running/stage the click set survives, and the
    // sync_progress listener below preserves it across backend frames. Shown as
    // "up to X min" only until the live estimator replaces it with a "X min
    // left" countdown.
    //
    // Only seed the bound when the store has NO etaSeconds yet: the preview path
    // (handleApply) already seeded a tighter delta-based estimate into the store,
    // and both click paths (handleSync / handleApply) FULL-REPLACE the store at
    // click time — so an etaSeconds present here is always this run's preview
    // seed, never a stale prior-run value. The skip-preview path never sets one,
    // so it still gets this bound.
    if (getSyncProgress().etaSeconds === undefined) {
      updateSyncProgress({ etaSeconds: estimatePlanSeconds(data.units) });
    }
    // Begin the run-scoped live-ETA estimator with the plan's per-unit weights
    // and total. Weights are skip-aware (#1382): 0 for a predicted-skip unit,
    // else the persisted collapsed count, falling back to the raw rom_count
    // when the backend doesn't know it (never-synced platform, collections,
    // old backend). A mis-predicted skip that actually dispatches re-corrects
    // via observeUnitTotal on its first chunk. MainPage samples the applying
    // stage against this to derive the countdown; a fresh plan resets any
    // prior slope.
    beginEtaRun(
      data.run_id,
      data.units.map((u) => (u.predicted_skip ? 0 : (u.collapsed_count ?? u.rom_count))),
      data.total_estimated_items ?? data.total_roms,
    );
    logInfo(
      `sync_plan received: ${data.total_units} units, ${data.total_roms} ROMs total` +
        (data.total_estimated_items !== undefined ? ` (${data.total_estimated_items} estimated items)` : ""),
    );
  });

  // ``sync_stale`` arrives after every unit finishes — remove each stale
  // shortcut by the ``app_id`` the backend captured BEFORE unbinding the
  // row. Resolving rom_id→app_id here (via getExistingRomMShortcuts) would
  // race the backend unbind and find nothing, orphaning the shortcut.
  const syncStaleListener = addEventListener<[SyncStaleData]>("sync_stale", (data: SyncStaleData) => {
    if (!Array.isArray(data.remove) || data.remove.length === 0) return;
    // Collect the valid app_ids and record the "removed" delta for each UP FRONT —
    // synchronously, before the first paced breather. recordSyncRemoved is a cheap,
    // deduped in-memory write; keeping it here rather than paired inside the paced
    // loop keeps the terminal sync_complete toast accurate. sync_complete reads
    // getSyncDelta(), and now that the removal yields between chunks it can
    // interleave during a breather — recording the delta progressively would let it
    // read a partial "removed" count. Only the Steam removal (the renderer-blocking
    // part) needs pacing (#977); chunk-paced (25 items / 50ms breather) so a large
    // platform-deletion / full re-sync teardown never blocks the CEF renderer or
    // corrupts Steam's in-memory shortcut store via removal churn.
    const appIds: number[] = [];
    for (const { app_id } of data.remove) {
      if (app_id) {
        appIds.push(app_id);
        recordSyncRemoved(app_id);
      }
    }
    const continuationController = syncContinuationController;
    const previousTail = staleRemovalTail;
    // The tail is stored and awaited later by the sync-completion continuation, so
    // it carries its own catch: an unhandled rejection would otherwise sit on it
    // for the whole window, and a rejected tail must not abort its awaiters.
    staleRemovalTail = withPruneLease(data.prune_lease_token, "Sync stale removal", async (leaseSignal) => {
      const abort = () => continuationController.abort();
      if (leaseSignal.aborted) abort();
      else leaseSignal.addEventListener("abort", abort, { once: true });
      try {
        await previousTail;
        const signal = continuationController.signal;
        if (isPruneLeaseCancelled(signal)) return;
        await removeShortcutsPaced(appIds, undefined, signal);
        if (!isPruneLeaseCancelled(signal)) logInfo(`sync_stale: removed ${data.remove.length} stale shortcuts`);
      } finally {
        leaseSignal.removeEventListener("abort", abort);
      }
    }).catch((e: unknown) => logError(`sync_stale: stale shortcut removal failed: ${e}`));
  });

  // ``sync_collections`` arrives at the end of the per-unit run with the
  // full platform / RomM-collection app-id maps. The monolithic path
  // routes these through ``sync_complete``; the per-unit path emits them
  // separately so the frontend can apply collection updates before the
  // terminal "done" toast.
  const syncCollectionsListener = addEventListener<[SyncCollectionsData]>(
    "sync_collections",
    (data: SyncCollectionsData) => {
      logInfo(`sync_collections received: ${Object.keys(data.platform_app_ids).length} platforms`);
    },
  );

  // Backend emits sync_progress events throughout the sync run — update the
  // module-level store. The backend frame carries no etaSeconds (that ceiling is
  // frontend-computed from sync_plan), so carry the current value across each
  // frame rather than letting the full-object replace wipe it.
  const syncProgressListener = addEventListener<[SyncProgress]>("sync_progress", (progress: SyncProgress) => {
    const { etaSeconds } = getSyncProgress();
    setSyncProgress(etaSeconds !== undefined ? { ...progress, etaSeconds } : progress);
  });

  const downloadProgressListener = addEventListener<[DownloadProgressEvent]>(
    "download_progress",
    (data: DownloadProgressEvent) => {
      // A cancel is an explicit discard — drop the entry entirely so no
      // "Cancelled" row lingers in the queue view or the QAM summary count
      // (#149 downloads-round). Both the running-cancel and the paused-cancel
      // backend paths emit this terminal frame, so this is the single place the
      // store drops a cancelled download. Every other status updates in place.
      if (data.status === "cancelled") {
        removeDownload(data.rom_id);
        return;
      }
      // Carry the server's resumability verdict from the frame; a frame that
      // omits it (older shape) keeps the prior value instead of clobbering it.
      const prev = getDownloadState().find((d) => d.rom_id === data.rom_id);
      updateDownload({
        rom_id: data.rom_id,
        rom_name: data.rom_name,
        platform_name: data.platform_name,
        file_name: data.file_name,
        status: data.status as "queued" | "downloading" | "completed" | "failed" | "cancelled" | "paused",
        progress: data.progress,
        bytes_downloaded: data.bytes_downloaded,
        total_bytes: data.total_bytes,
        resumable: data.resumable ?? prev?.resumable ?? false,
      });
    },
  );

  const downloadCompleteListener = addEventListener<[DownloadCompleteEvent]>(
    "download_complete",
    (data: DownloadCompleteEvent) => {
      const prev = getDownloadState().find((d) => d.rom_id === data.rom_id);
      updateDownload({
        rom_id: data.rom_id,
        rom_name: data.rom_name,
        platform_name: data.platform_name,
        file_name: prev?.file_name ?? "",
        status: "completed",
        progress: 1,
        bytes_downloaded: prev?.bytes_downloaded ?? 0,
        total_bytes: prev?.total_bytes ?? 0,
        resumable: data.resumable ?? prev?.resumable ?? false,
      });
      toaster.toast({
        title: "RomM Sync",
        body: `Downloaded ${data.rom_name}`,
      });

      // The ROM is now installed — its shortcut's launch options must carry the
      // full launch command (was "" while uninstalled). The backend resolved
      // the bound appId for this rom_id and put it on the payload, so confirm-set
      // the new launch options directly. ``app_id`` is null when the ROM isn't
      // synced yet (no shortcut) — no-op; the next sync writes the command at
      // creation time.
      if (data.app_id !== null) {
        const appId = data.app_id;
        detach(
          (async () => {
            try {
              const ok = await withPruneLease(data.prune_lease_token, "Download completion", async (signal) => {
                if (isPruneLeaseCancelled(signal)) return false;
                return setLaunchOptionsConfirmed(appId, data.launch_options);
              });
              if (!ok) {
                logError(`download_complete: failed to confirm launch options for rom ${data.rom_id} (appId ${appId})`);
              }
            } catch (e) {
              logError(`download_complete: failed to set launch options for rom ${data.rom_id}: ${e}`);
            }
          })(),
        );
      }
    },
  );

  const downloadFailedListener = addEventListener<[DownloadFailedEvent]>(
    "download_failed",
    (data: DownloadFailedEvent) => handleGlobalDownloadFailure(data, { getDownloadState, updateDownload }, toaster),
  );

  const pathChangedListener = addEventListener<[{ old_path: string; new_path: string; cleared?: boolean }]>(
    "retrodeck_path_changed",
    (data) => {
      // Backend auto-clears the migration when the new path matches a previous
      // RetroDECK home (round-trip / branch reset). Drop the pending block so
      // all subscribers re-render without the migration UI.
      if (data.cleared) {
        setMigrationStatus({ pending: false });
        return;
      }
      // Path actually changed — refetch authoritative status (file counts).
      getMigrationStatus()
        .then((status) => setMigrationStatus(status))
        .catch((e) => logError(`Failed to refresh migration status: ${e}`));
    },
  );

  const saveSortChangedListener = addEventListener<
    [
      {
        old_settings: { sort_by_content: boolean; sort_by_core: boolean };
        new_settings: { sort_by_content: boolean; sort_by_core: boolean };
      },
    ]
  >("save_sort_changed", () => {
    toaster.toast({
      title: "RomM Sync",
      body: "RetroArch save sorting changed. Go to Settings to migrate save files.",
    });
  });

  const saveStatusListener = addEventListener<[SaveStatus]>("save_status_updated", (data: SaveStatus) => {
    const hasConflict = hasAnySaveConflict(data);
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", {
        detail: { type: "save_sync", rom_id: data.rom_id, save_status: data, has_conflict: hasConflict },
      }),
    );
  });

  // After a RetroDECK-home migration the backend rewrites each installed ROM's
  // launch command to the new path and emits the new command per shortcut.
  // Confirm-set each so existing shortcuts launch from the migrated location.
  const migrationRelaunchListener = addEventListener<
    [{ items: { app_id: number; launch_options: string }[]; prune_lease_token?: string }]
  >("migration_relaunch_options", (data) => {
    detach(
      withPruneLease(data.prune_lease_token, "RetroDECK migration", (signal) =>
        batchConfirmLaunchOptions(data.items, "migration_relaunch_options", signal),
      ),
    );
  });

  // Server retry-ladder progress (#1345): the backend emits one frame per HTTP
  // retry so the saves surfaces can show "Connecting to RomM… (attempt N/M)".
  // The consuming surface clears the store once its own load settles — the
  // ladder itself emits no terminal "done" frame.
  const serverRetryListener = addEventListener<[ServerRetryProgressEvent]>(
    "server_retry_progress",
    (data: ServerRetryProgressEvent) => {
      setServerRetryProgress({ attempt: data.attempt, maxAttempts: data.max_attempts });
    },
  );

  // Destructive cleanup actions must keep running even when the Danger Zone or
  // game-detail picker unmounts. The backend emits one tokenized action at a
  // time; this root handler owns every Steam API mutation and reports the exact
  // token outcome before backend filesystem/SQLite finalization can proceed.
  const publishPruneSwitches = async (
    runId: string,
    pending: Array<{ appId: number; romId: number }>,
    leaseToken: string | undefined,
  ): Promise<void> => {
    if (!leaseToken) {
      logError("Cleanup publication was skipped because its continuation lease was missing.");
      return;
    }
    await withPruneLease(
      leaseToken,
      "Cleanup repoint publication",
      async (signal) => {
        let lastMessage = "Cleanup claim release was not confirmed.";
        for (let attempt = 1; attempt <= 3; attempt++) {
          try {
            const released = await withTimeout(waitForPruneRelease(runId), 6000);
            if (released.success) {
              await publishSwitchesUntilCancelled(pending, signal);
              return;
            }
            lastMessage = released.message;
          } catch (e) {
            lastMessage = e instanceof Error ? e.message : String(e);
          }
          if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, attempt * 100));
        }
        logError(`Cleanup publication could not confirm claim release: ${lastMessage}`);
      },
      "root",
      pluginAdmission,
    );
  };

  const pruneActionListener = addEventListener<[PruneActionRequired]>(
    "prune_action_required",
    (action: PruneActionRequired) => {
      if (!admitPruneFrame(action.preview_id, action.run_id)) return;
      detach(handlePruneAction(action));
    },
  );

  const pruneProgressListener = addEventListener<[PruneProgress]>("prune_progress", (progress: PruneProgress) =>
    setPruneProgress(progress),
  );

  const pruneCompleteListener = addEventListener<[PruneComplete]>("prune_complete", (result: PruneComplete) => {
    const completed = setPruneComplete(result);
    if (!completed) return;
    for (const appId of completed.affected_app_ids) invalidateCachedGameDetail(appId);
    for (const appId of completed.removed_app_ids ?? []) unregisterRomMAppId(appId);
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", {
        detail: {
          type: "rom_pruned",
          app_ids: completed.affected_app_ids,
          rom_ids: completed.removed_rom_ids,
        },
      }),
    );
    cancelPruneActions();
    const publications: Array<{ appId: number; romId: number }> = [];
    for (const item of completed.results) {
      if (
        item.committed_action === "repoint_shortcut" &&
        !item.action_ambiguous &&
        item.app_id !== undefined &&
        item.target_rom_id !== undefined
      ) {
        publications.push({ appId: item.app_id, romId: item.target_rom_id });
      }
    }
    if (publications.length) {
      detach(publishPruneSwitches(completed.run_id, publications, completed.prune_lease_token));
    } else if (completed.prune_lease_token) {
      // The terminal frame carried a continuation lease but committed no repoint
      // to publish, so nothing downstream will ever release it. Handing it back
      // now keeps it from pinning the admission gate until its TTL and refusing
      // every conflicting callable in the meantime (#1570 F13).
      detach(releasePruneLease(completed.prune_lease_token, "Cleanup completion with nothing to publish"));
    }
    toaster.toast({ title: "RomM Sync", ...buildPruneCompleteToast(completed) });
  });

  return {
    name: "RomM Sync",
    icon: <FaGamepad />,
    content: <QAMPanel />,
    alwaysRender: true,
    onDismount() {
      syncContinuationController.abort();
      destroySessionManager();
      unregisterLaunchInterceptor();
      unregisterGameDetailPatch();
      unregisterMetadataPatches();
      removeEventListener("sync_complete", syncCompleteListener);
      removeEventListener("sync_apply_unit", syncApplyUnitListener);
      removeEventListener("sync_plan", syncPlanListener);
      removeEventListener("sync_stale", syncStaleListener);
      removeEventListener("sync_collections", syncCollectionsListener);
      removeEventListener("sync_progress", syncProgressListener);
      removeEventListener("download_progress", downloadProgressListener);
      removeEventListener("download_complete", downloadCompleteListener);
      removeEventListener("download_failed", downloadFailedListener);
      removeEventListener("retrodeck_path_changed", pathChangedListener);
      removeEventListener("save_sort_changed", saveSortChangedListener);
      removeEventListener("save_status_updated", saveStatusListener);
      removeEventListener("migration_relaunch_options", migrationRelaunchListener);
      removeEventListener("server_retry_progress", serverRetryListener);
      removeEventListener("prune_action_required", pruneActionListener);
      cancelPruneActions();
      detach(releaseAllPruneLeases());
      removeEventListener("prune_progress", pruneProgressListener);
      removeEventListener("prune_complete", pruneCompleteListener);
    },
  };
});
