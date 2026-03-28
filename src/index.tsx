import {
  definePlugin,
  addEventListener,
  removeEventListener,
  toaster,
} from "@decky/api";
import { useState, FC } from "react";
import { FaGamepad } from "react-icons/fa";
import { MainPage } from "./components/MainPage";
import { SettingsPage } from "./components/SettingsPage";
import { LibraryPage } from "./components/LibraryPage";
import { DangerZone } from "./components/DangerZone";
import { DownloadQueue } from "./components/DownloadQueue";
import { initSyncManager } from "./utils/syncManager";
import { setSyncProgress } from "./utils/syncProgress";
import { updateDownload, getDownloadState } from "./utils/downloadStore";
import { registerGameDetailPatch, unregisterGameDetailPatch, registerRomMAppId } from "./patches/gameDetailPatch";
import { registerMetadataPatches, unregisterMetadataPatches, applyAllPlaytime } from "./patches/metadataPatches";
import { registerLaunchInterceptor, unregisterLaunchInterceptor } from "./utils/launchInterceptor";
import { getAllMetadataCache, getAppIdRomIdMap, ensureDeviceRegistered, getSaveSyncSettings, getAllPlaytime, getMigrationStatus, getSaveSortMigrationStatus, logError, logInfo } from "./api/backend";
import { createOrUpdateCollections, createOrUpdateRomMCollections, clearPlatformCollection, getHostname } from "./utils/collections";
import { setMigrationStatus } from "./utils/migrationStore";
import { setSaveSortMigrationStatus } from "./utils/saveSortMigrationStore";
import { initSessionManager, destroySessionManager } from "./utils/sessionManager";
import type { SyncProgress, DownloadProgressEvent, DownloadCompleteEvent, SaveStatus } from "./types";

type Page = "main" | "settings" | "library" | "data" | "downloads";

// Module-level page state survives QAM remounts (e.g. after modal close)
let currentPage: Page = "main";

const QAMPanel: FC = () => {
  const [page, setPageState] = useState<Page>(currentPage);
  const setPage = (p: Page) => { currentPage = p; setPageState(p); };

  switch (page) {
    case "settings":
      return <SettingsPage onBack={() => setPage("main")} />;
    case "library":
      return <LibraryPage onBack={() => setPage("main")} />;
    case "data":
      return <DangerZone onBack={() => setPage("main")} />;
    case "downloads":
      return <DownloadQueue onBack={() => setPage("main")} />;
    default:
      return <MainPage onNavigate={(p) => setPage(p)} />;
  }
};

export default definePlugin(() => {
  registerGameDetailPatch();
  registerLaunchInterceptor();

  // Load metadata cache, register store patches, and populate RomM app ID set.
  // Retries with backoff if the backend isn't ready yet (e.g. boot without network).
  // callable() has no timeout — hangs forever if backend isn't ready — so we race
  // each attempt against a deadline to ensure retries actually fire.
  const RETRY_DELAYS = [2000, 5000, 10000, 15000, 20000];
  const CALLABLE_TIMEOUT = 5000;
  let initAttempt = 0;
  let initDone = false;

  function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
    return Promise.race([
      promise,
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error(`callable timed out after ${ms}ms`)), ms),
      ),
    ]);
  }

  async function loadAppIdsAndMetadata() {
    const [cache, appIdMap] = await withTimeout(
      Promise.all([getAllMetadataCache(), getAppIdRomIdMap()]),
      CALLABLE_TIMEOUT,
    );
    registerMetadataPatches(cache, appIdMap);

    for (const appIdStr of Object.keys(appIdMap)) {
      const appId = parseInt(appIdStr, 10);
      if (!isNaN(appId)) {
        registerRomMAppId(appId);
      }
    }

    try {
      const { playtime } = await withTimeout(getAllPlaytime(), CALLABLE_TIMEOUT);
      applyAllPlaytime(playtime, appIdMap);
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

  (async () => {
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
  })();

  // Check for pending RetroDECK path migration on startup
  (async () => {
    try {
      const status = await getMigrationStatus();
      if (status.pending) {
        setMigrationStatus(status);
        toaster.toast({
          title: "RomM Sync",
          body: "RetroDECK location changed. Go to Settings to migrate files.",
        });
      }
    } catch (e) {
      logError(`Failed to check migration status: ${e}`);
    }
  })();

  // Check for pending save sort migration on startup
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
  })();

  // Register device and initialize session manager for save sync (if enabled)
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
  })();

  const onSyncComplete = (data: {
    platform_app_ids: Record<string, number[]>;
    romm_collection_app_ids?: Record<string, number[]>;
    total_games: number;
    cancelled?: boolean;
  }) => {
    logInfo(`sync_complete received: ${data.total_games} games, cancelled=${data.cancelled ?? false}`);
    toaster.toast({
      title: "RomM Sync",
      body: data.cancelled
        ? `Sync cancelled. ${data.total_games} games processed.`
        : `Sync complete! ${data.total_games} games added.`,
    });

    // Update RomM app ID set with newly synced shortcuts
    for (const appIds of Object.values(data.platform_app_ids)) {
      for (const appId of appIds) {
        registerRomMAppId(appId);
      }
    }

    // Create/update platform and RomM Steam collections + clean stale ones
    (async () => {
      try {
        // Create/update platform collections
        if (data.platform_app_ids && Object.keys(data.platform_app_ids).length > 0) {
          await createOrUpdateCollections(data.platform_app_ids);
        }

        if (data.romm_collection_app_ids && Object.keys(data.romm_collection_app_ids).length > 0) {
          await createOrUpdateRomMCollections(data.romm_collection_app_ids);
        }

        if (typeof collectionStore !== "undefined") {
          const hostname = await getHostname();
          const suffix = ` (${hostname})`;

          // Clean stale platform collections
          const activePlatforms = new Set(Object.keys(data.platform_app_ids ?? {}));
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
            return match ? !activeNames.has(match[1]) : false;
          });
          for (const c of staleRomm) {
            logInfo(`Removing stale RomM collection "${c.displayName}"`);
            await c.Delete();
          }
        }
      } catch (e) {
        logError(`Failed to manage RomM collections: ${e}`);
      }
    })();

    // Re-apply playtime to Steam UI (app IDs may have changed after re-sync)
    (async () => {
      try {
        const [{ playtime }, appIdMap] = await Promise.all([
          getAllPlaytime(),
          getAppIdRomIdMap(),
        ]);
        applyAllPlaytime(playtime, appIdMap);
      } catch (e) {
        logError(`Failed to re-apply playtime after sync: ${e}`);
      }
    })();
  };

  const syncCompleteListener = addEventListener<
    [{ platform_app_ids: Record<string, number[]>; romm_collection_app_ids?: Record<string, number[]>; total_games: number }]
  >("sync_complete", onSyncComplete);

  const syncApplyListener = initSyncManager();

  // Backend emits sync_progress events throughout _do_sync — update the module-level store
  const syncProgressListener = addEventListener<[SyncProgress]>(
    "sync_progress",
    (progress: SyncProgress) => {
      setSyncProgress(progress);
    }
  );

  const downloadProgressListener = addEventListener<[DownloadProgressEvent]>(
    "download_progress",
    (data: DownloadProgressEvent) => {
      updateDownload({
        rom_id: data.rom_id,
        rom_name: data.rom_name,
        platform_name: data.platform_name ?? "",
        file_name: data.file_name ?? "",
        status: data.status as "queued" | "downloading" | "completed" | "failed" | "cancelled",
        progress: data.progress,
        bytes_downloaded: data.bytes_downloaded,
        total_bytes: data.total_bytes,
      });
    }
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
      });
      toaster.toast({
        title: "RomM Sync",
        body: `Downloaded ${data.rom_name}`,
      });
    }
  );

  const pathChangedListener = addEventListener<
    [{ old_path: string; new_path: string }]
  >("retrodeck_path_changed", (data) => {
    setMigrationStatus({
      pending: true,
      old_path: data.old_path,
      new_path: data.new_path,
    });
    toaster.toast({
      title: "RomM Sync",
      body: "RetroDECK location changed. Go to Settings to migrate files.",
    });
  });

  const saveSortChangedListener = addEventListener<
    [{ old_settings: { sort_by_content: boolean; sort_by_core: boolean }; new_settings: { sort_by_content: boolean; sort_by_core: boolean } }]
  >("save_sort_changed", (data) => {
    setSaveSortMigrationStatus({
      pending: true,
      old_settings: data.old_settings,
      new_settings: data.new_settings,
    });
    const coreChanged = data.old_settings.sort_by_core !== data.new_settings.sort_by_core;
    toaster.toast({
      title: "RomM Sync",
      body: coreChanged
        ? "RetroArch save sorting changed. Manual migration recommended — check Settings."
        : "RetroArch save sorting changed. Go to Settings to migrate save files.",
    });
  });

  const saveStatusListener = addEventListener<[SaveStatus]>(
    "save_status_updated",
    (data: SaveStatus) => {
      const hasConflict = data.files?.some((f) => f.status === "conflict") ?? false;
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", {
        detail: { type: "save_sync", rom_id: data.rom_id, save_status: data, has_conflict: hasConflict },
      }));
    }
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
      removeEventListener("sync_apply", syncApplyListener);
      removeEventListener("sync_progress", syncProgressListener);
      removeEventListener("download_progress", downloadProgressListener);
      removeEventListener("download_complete", downloadCompleteListener);
      removeEventListener("retrodeck_path_changed", pathChangedListener);
      removeEventListener("save_sort_changed", saveSortChangedListener);
      removeEventListener("save_status_updated", saveStatusListener);
    },
  };
});
