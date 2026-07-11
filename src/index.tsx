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
import { NEW_ITEM_SEC } from "./utils/syncEstimate";
import { beginEtaRun } from "./utils/syncEta";
import { updateDownload, getDownloadState } from "./utils/downloadStore";
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
import { resetSyncDelta, recordSyncRemoved, getSyncDelta, getAckedCreatedAppIds } from "./utils/syncDeltaStore";
import { stampCoverMtimes, healCoverMtimes } from "./utils/coverMtime";
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

// Cover heal poll (see startCoverHealPoll). Steam re-materializes some fresh
// shortcuts' overviews seconds after creation and wipes the JS-set stamp; a
// verify-and-heal poll catches the ~1% victims within a round or two instead of a
// blind fixed wait. Poll every 15s; exit after 2 consecutive clean rounds or 8
// rounds (~2 min), whichever first.
const COVER_HEAL_INTERVAL_MS = 15_000;
const COVER_HEAL_STABLE_ROUNDS = 2;
const COVER_HEAL_MAX_ROUNDS = 8;

// At most one heal poll across the plugin's lifetime (chained setTimeout): a fresh
// sync supersedes a running poll, and onDismount clears it so a reload can't fire a
// stale callback.
let coverHealTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Start (or restart) the post-sync cover-heal poll over *appIds*. Steam name-matches
 * some fresh shortcuts against its own catalog and re-materializes their app overview
 * seconds after creation, wiping the JS-set ``rt_custom_image_mtime`` (~1% of creates;
 * census on-device 2026-07-10). Each round reads every created appId's overview (cheap
 * in-memory) and re-stamps exactly the ones whose stamp went missing via the shared
 * micro-batched helper. Exits after {@link COVER_HEAL_STABLE_ROUNDS} consecutive clean
 * rounds (the wipe is a single early event, so this settles in ~15-30s) or
 * {@link COVER_HEAL_MAX_ROUNDS} total (~2 min) as a pathological-case cap. A non-empty
 * call cancels any running poll first — a fresh sync supersedes it — and onDismount
 * cancels it too; a zero-created ("Library up to date") sync is a no-op that leaves a
 * running poll alone (#L6).
 */
function startCoverHealPoll(appIds: number[]): void {
  // Zero created → nothing to heal. Return BEFORE cancelling, so a "Library up to
  // date" sync never aborts the previous run's still-active heal window (#L6).
  if (appIds.length === 0) return;
  if (coverHealTimer !== null) {
    clearTimeout(coverHealTimer);
    coverHealTimer = null;
  }
  let round = 0;
  let consecutiveClean = 0;
  const tick = () => {
    coverHealTimer = null;
    round++;
    try {
      // A null overview can't be healed this round — only present-but-unstamped counts.
      const missing = appIds.filter((appId) => {
        const overview = appStore.GetAppOverviewByAppID(appId);
        return overview !== null && overview.rt_custom_image_mtime === undefined;
      });
      if (missing.length > 0) {
        consecutiveClean = 0;
        logInfo(`[FE] cover mtime heal: ${missing.length} re-stamped (round ${round})`);
        void healCoverMtimes(missing);
      } else {
        consecutiveClean++;
      }
    } catch (e) {
      // Fail-soft: a throwing read must not kill the poll. Treat the round as
      // inconclusive (not clean, so it can't exit "stable") and continue — the
      // round still counts toward the cap, so a persistently throwing read exits.
      consecutiveClean = 0;
      logError(`[FE] cover mtime heal: round ${round} read failed: ${e}`);
    }
    if (consecutiveClean >= COVER_HEAL_STABLE_ROUNDS) {
      logInfo(`[FE] cover mtime heal: stable after ${round} rounds`);
      return;
    }
    if (round >= COVER_HEAL_MAX_ROUNDS) {
      logInfo(`[FE] cover mtime heal: capped at ${round} rounds`);
      return;
    }
    coverHealTimer = setTimeout(tick, COVER_HEAL_INTERVAL_MS);
  };
  coverHealTimer = setTimeout(tick, COVER_HEAL_INTERVAL_MS);
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
  }) => {
    logInfo(`sync_complete received: ${data.total_games} games, cancelled=${data.cancelled ?? false}`);

    // Report the TRUE delta, not the total processed set. The library applies
    // whole platforms, so total_games is not a real delta — the exact, honest
    // counts are the shortcuts the frontend actually created and removed this
    // run (tracked in syncDeltaStore, deduplicated across platform/collection
    // units). Omit a zero part; "Library up to date." when nothing changed.
    const { added, removed } = getSyncDelta();
    const parts: string[] = [];
    if (added > 0) parts.push(`${added} added`);
    if (removed > 0) parts.push(`${removed} removed`);
    const summary = parts.join(", ");
    let body: string;
    if (data.cancelled) {
      body = summary ? `Sync cancelled — ${summary} so far.` : "Sync cancelled.";
    } else {
      body = summary ? `Sync complete — ${summary}.` : "Library up to date.";
    }
    toaster.toast({ title: "RomM Sync", body });

    // Drive the terminal UI teardown from ``sync_complete`` — the guaranteed
    // terminal signal. The backend ALSO emits a separate stage:"done"/"cancelled"
    // sync_progress frame, but that second frame can be dropped or raced (e.g. a
    // failure in the post-complete bound-count read between the two emits),
    // leaving the QAM stuck on the optimistic "Applying" frame. Flip the store to
    // a terminal stage here (merge — keeps the fine fields) so MainPage's
    // onSyncProgressChange tears the in-progress UI down regardless.
    updateSyncProgress({ running: false, stage: data.cancelled ? "cancelled" : "done" });

    // Make each freshly-created shortcut's cover appear on its tile's next render
    // without a client restart. Covers are written server-side at each chunk's
    // commit (ADR-0021 lazy model), but Steam resolves a fresh shortcut's tile to
    // the default capsule at creation and caches that resolution OUTSIDE the JS
    // context — a JS-context reload does NOT re-resolve it; only a full client
    // restart does. The tile URL is `/customimage/{appid}?v={mtime}`, keyed on the
    // overview's `rt_custom_image_mtime` (the field a restart normally stamps), so
    // stamping it ourselves per created appId is the per-app cache-buster: the tile
    // picks the cover up on its NEXT render (scrolling the row out/in, revisiting
    // the library) — no forced global re-render. Read the acked-created set BEFORE
    // the resetSyncDelta() below. Only ACKED creates are stamped: a cancelled run's
    // final in-flight chunk creates shortcuts frontend-side whose ack was skipped,
    // so their covers were never written server-side — stamping them would point the
    // tile at a 404 (#M1). ReportLibraryAssetCacheMiss(appId, 0) was tried and is a
    // no-op for non-erroring default tiles (on-device 2026-07-10). Fail-soft: a
    // missing overview or a throw must never break the teardown/toast above.
    //
    // Per-chunk stamping (syncManager, after each chunk's ack) is the PRIMARY path
    // now — covers appear progressively during the run. This end-of-run sweep is
    // the belt-and-braces net that re-stamps the whole acked set.
    const ackedAppIds = getAckedCreatedAppIds();
    // Fire-and-forget (micro-batched inside): must not block the teardown below.
    void stampCoverMtimes(ackedAppIds, "");
    // Then verify-and-heal poll: Steam re-materializes some fresh shortcuts'
    // overviews seconds after creation and wipes the stamp (~1% of creates,
    // on-device 2026-07-10). The poll re-reads and re-stamps only the missing ones
    // over the SAME acked set, healing them within a round or two instead of a blind
    // fixed wait, and supersedes any poll still running (a zero-created sync leaves
    // a running poll alone).
    startCoverHealPoll(ackedAppIds);

    // Defensive reset; sync_plan also resets at the start of the next run.
    resetSyncDelta();

    // Update RomM app ID set with newly synced shortcuts. The unit-ack
    // registration in syncManager is the primary path (registers each appId the
    // moment it resolves); this is the redundant, no-op-safe net for any appId
    // the unit loop didn't reach. Iterate BOTH maps: a collection-only sync
    // touches only romm_collection_app_ids, so skipping it would leave those
    // shortcuts unregistered until a Steam restart (#1205).
    for (const appIds of Object.values(data.platform_app_ids)) {
      for (const appId of appIds) {
        registerRomMAppId(appId);
      }
    }
    for (const appIds of Object.values(data.romm_collection_app_ids ?? {})) {
      for (const appId of appIds) {
        registerRomMAppId(appId);
      }
    }

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
    // Seed the applying-phase estimate: an honest upper bound that prices every
    // planned ROM as a new shortcut. Incremental runs skip unchanged units and
    // update-path items are cheaper, so the real duration only ever undershoots.
    // Merged (not replaced) so the running/stage the click set survives, and the
    // sync_progress listener below preserves it across backend frames. Shown as
    // "up to ~X min" only until the live rate estimator (below) has measured the
    // real apply speed, which then replaces it with a "~X min left" countdown.
    //
    // Only seed the bound when the store has NO etaSeconds yet: the preview path
    // (handleApply) already seeded a tighter delta-based estimate into the store,
    // and both click paths (handleSync / handleApply) FULL-REPLACE the store at
    // click time — so an etaSeconds present here is always this run's preview
    // seed, never a stale prior-run value. The skip-preview path never sets one,
    // so it still gets this bound.
    if (getSyncProgress().etaSeconds === undefined) {
      updateSyncProgress({ etaSeconds: data.total_roms * NEW_ITEM_SEC });
    }
    // Begin the run-scoped live-ETA estimator with the plan's per-unit weights
    // (rom_count in plan order) and total. MainPage samples the applying stage
    // against this to derive the countdown; a fresh plan resets any prior slope.
    beginEtaRun(
      data.run_id,
      data.units.map((u) => u.rom_count),
      data.total_roms,
    );
    logInfo(`sync_plan received: ${data.total_units} units, ${data.total_roms} ROMs total`);
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
      // Cancel any running cover-heal poll so a reload can't fire a stale round.
      if (coverHealTimer !== null) {
        clearTimeout(coverHealTimer);
        coverHealTimer = null;
      }
    },
  };
});
