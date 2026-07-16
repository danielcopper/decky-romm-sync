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
import { estimateApplySeconds } from "./utils/syncEstimate";
import { beginEtaRun } from "./utils/syncEta";
import { updateDownload, getDownloadState, removeDownload } from "./utils/downloadStore";
import { handleGlobalDownloadFailure } from "./utils/downloadFailure";
import { registerGameDetailPatch, unregisterGameDetailPatch, registerRomMAppId } from "./patches/gameDetailPatch";
import {
  registerMetadataPatches,
  unregisterMetadataPatches,
  applyAllPlaytime,
  applyAllMetadata,
} from "./patches/metadataPatches";
import { registerLaunchInterceptor, unregisterLaunchInterceptor } from "./utils/launchInterceptor";
import { hasAnySaveConflict } from "./utils/saveStatus";
import {
  getMetadataCachePage,
  getAppIdRomIdMap,
  ensureDeviceRegistered,
  getSaveSyncSettings,
  getAllPlaytime,
  getMigrationStatus,
  getSaveSortMigrationStatus,
  getInstalledRelaunchOptions,
  testConnection,
  logError,
  logInfo,
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
  RomMetadata,
} from "./types";
import { removeShortcut, setLaunchOptionsConfirmed } from "./utils/steamShortcuts";
import { batchConfirmLaunchOptions } from "./utils/launchOptionsReconcile";
import { withTimeout } from "./utils/withTimeout";

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

/** Register every appId in *map* (values are per-key appId arrays) as RomM-owned. */
function registerAppIds(map: Record<string, number[]>): void {
  for (const appIds of Object.values(map)) {
    for (const appId of appIds) {
      registerRomMAppId(appId);
    }
  }
}

export default definePlugin(() => {
  registerGameDetailPatch();
  registerLaunchInterceptor();

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

  async function loadAppIdsAndMetadata() {
    const appIdMap = await withTimeout(getAppIdRomIdMap(), CALLABLE_TIMEOUT);

    // Page the metadata cache until every row is collected. A failed page throws
    // out of the loop (each raced against the same per-callable deadline) and
    // the outer retry loop restarts init from offset 0. The empty-page guard
    // stops the loop even if ``total`` overshoots the rows actually returned.
    const cache: Record<string, RomMetadata> = {};
    let collected = 0;
    let total = Number.POSITIVE_INFINITY;
    let offset = 0;
    while (collected < total) {
      const page = await withTimeout(getMetadataCachePage(offset, METADATA_PAGE_SIZE), CALLABLE_TIMEOUT);
      total = page.total;
      const keys = Object.keys(page.items);
      if (keys.length === 0) break;
      for (const key of keys) cache[key] = page.items[key]!;
      collected += keys.length;
      offset += METADATA_PAGE_SIZE;
    }

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
      // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- `initDone` is flipped to true inside the awaited `loadAppIdsAndMetadata()`; TS's control-flow analysis can't see that cross-function mutation and narrows it to the `false` literal here. The guard is real: it gates the reconcile on the loop having actually reached a reachable backend.
      if (initDone) {
        try {
          const items = await getInstalledRelaunchOptions();
          await batchConfirmLaunchOptions(items, "startup_reconcile");
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
  }) => {
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

    // Re-confirm launch_options for every installed+bound ROM after a sync. A
    // normal sync skips unchanged platforms (no per-unit sync_apply_unit emit),
    // so a shortcut whose launch_options drifted on an unchanged platform is
    // otherwise healed only on the next plugin reload (startup reconcile) or
    // Play press — never by the sync itself (#1151). Reuse the startup-reconcile
    // mechanism: idempotent + appId-safe, runs regardless of cancellation.
    detach(
      (async () => {
        try {
          const items = await getInstalledRelaunchOptions();
          await batchConfirmLaunchOptions(items, "sync_reconcile");
        } catch (e) {
          logError(`sync_reconcile: failed to reconcile launch options: ${e}`);
        }
      })(),
    );

    // Create/update platform and RomM Steam collections + clean stale ones
    detach(
      (async () => {
        try {
          // Create/update platform collections
          if (Object.keys(data.platform_app_ids).length > 0) {
            await createOrUpdateCollections(data.platform_app_ids);
          }

          if (data.romm_collection_app_ids && Object.keys(data.romm_collection_app_ids).length > 0) {
            await createOrUpdateRomMCollections(data.romm_collection_app_ids);
          }

          // Stale-collection cleanup deletes any RomM collection not in
          // data.platform_app_ids / romm_collection_app_ids. On a cancelled run
          // those maps are PARTIAL (only the platforms reached before the cancel,
          // empty if the cancel fired before unit 1), so treating them as the
          // authoritative active-set would delete collections for unreached
          // platforms — wiping library organization. Only run cleanup on a
          // completed sync. The additive create/update above stays safe on a
          // partial run. (#1040)
          if (!data.cancelled && typeof collectionStore !== "undefined") {
            const hostname = await getHostname();
            const suffix = ` (${hostname})`;

            // Clean stale platform collections
            const activePlatforms = new Set(Object.keys(data.platform_app_ids));
            const stalePlatform = collectionStore.userCollections.filter((c) => {
              if (!c.displayName.startsWith("RomM: ")) return false;
              const afterPrefix = c.displayName.slice(6);
              if (afterPrefix.startsWith("[")) return false; // Skip RomM collections
              if (!c.displayName.endsWith(suffix)) return false; // Only this machine
              const platformName = afterPrefix.replace(/\s\([^)]+\)$/, "");
              return !activePlatforms.has(platformName);
            });
            for (const c of stalePlatform) {
              const afterPrefix = c.displayName.slice(6);
              const platformName = afterPrefix.replace(/\s\([^)]+\)$/, "");
              logInfo(`Removing stale platform collection "${c.displayName}"`);
              await clearPlatformCollection(platformName);
            }

            // Clean stale RomM collection-based collections
            const activeNames = new Set(Object.keys(data.romm_collection_app_ids ?? {}));
            const rommCollectionPattern = /^RomM: \[([^\]]+)\]/;
            const staleRomm = collectionStore.userCollections.filter((c) => {
              if (!c.displayName.startsWith("RomM: [")) return false;
              if (!c.displayName.endsWith(suffix)) return false;
              const match = rommCollectionPattern.exec(c.displayName);
              return match ? !activeNames.has(match[1]!) : false; // group 1 present whenever match is non-null
            });
            for (const c of staleRomm) {
              logInfo(`Removing stale RomM collection "${c.displayName}"`);
              await c.Delete();
            }
          }
        } catch (e) {
          logError(`Failed to manage RomM collections: ${e}`);
        }
      })(),
    );

    // Re-apply playtime to Steam UI (app IDs may have changed after re-sync)
    detach(
      (async () => {
        try {
          const [{ playtime }, appIdMap] = await Promise.all([getAllPlaytime(), getAppIdRomIdMap()]);
          await applyAllPlaytime(playtime, appIdMap);
        } catch (e) {
          logError(`Failed to re-apply playtime after sync: ${e}`);
        }
      })(),
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
    // sync_plan fires once per run, before any unit — reset the per-run delta
    // so the terminal toast counts only this run's created/removed shortcuts.
    resetSyncDelta();
    // Clear the per-run cancel flag once per run, before any unit. Doing it
    // here (not only in the per-unit handler) keeps the flag fresh even on a
    // skip-only run, where no unit handler ever fires (#1198). Run identity for
    // a Cancel click comes from the backend-fed sync_progress store now (#1202).
    resetSyncCancel();
    // Seed the applying-phase estimate on the walk-cost model (the same
    // ``estimateApplySeconds`` the preview row uses): an honest upper bound that
    // prices every planned item as a new shortcut plus the flat fetch allowance.
    // The plan is skip-aware (#1382): ``total_estimated_items`` zero-weights the
    // platforms the backend predicts its wholesale-skip gate will skip and prices
    // the rest at their persisted collapsed (post-sibling-group) shortcut count,
    // so an incremental re-sync no longer seeds a whole-library ceiling. The raw
    // ``total_roms`` stays the fallback for an older backend that doesn't send
    // the field. The delta-restricted apply skips unchanged items and the live
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
      updateSyncProgress({ etaSeconds: estimateApplySeconds(data.total_estimated_items ?? data.total_roms, 0) });
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
    for (const { app_id } of data.remove) {
      if (app_id) {
        removeShortcut(app_id);
        // Record the removal as a real "removed" delta. The store dedups across
        // the per-unit sync_stale emits so a shortcut removed once is counted once.
        recordSyncRemoved(app_id);
      }
    }
    logInfo(`sync_stale: removed ${data.remove.length} stale shortcuts`);
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
              const ok = await setLaunchOptionsConfirmed(appId, data.launch_options);
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
  const migrationRelaunchListener = addEventListener<[{ items: { app_id: number; launch_options: string }[] }]>(
    "migration_relaunch_options",
    (data) => {
      detach(batchConfirmLaunchOptions(data.items, "migration_relaunch_options"));
    },
  );

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

  return {
    name: "RomM Sync",
    icon: <FaGamepad />,
    content: <QAMPanel />,
    alwaysRender: true,
    onDismount() {
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
    },
  };
});
