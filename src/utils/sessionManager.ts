/**
 * Session manager — detects game start/stop for RomM shortcuts and triggers
 * save sync + playtime tracking via backend callables.
 *
 * Uses SteamClient.GameSessions.RegisterForAppLifetimeNotifications to detect
 * game lifecycle events and Router.MainRunningApp for reliable app ID resolution.
 */

import { toaster } from "@decky/api";
import {
  postExitSync,
  recordSessionStart,
  recordSessionEnd,
  getAppIdRomIdMap,
  syncAchievementsAfterSession,
  refreshMigrationState,
  logInfo,
  logError,
} from "../api/backend";
import { setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
import { isNewerInSlotConflict, isPendingConflict } from "../types";
import type { PendingConflict, NewerInSlotConflict } from "../types";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";

declare var Router: {
  MainRunningApp: { appid: number; display_name: string } | null;
};

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Active session tracking
let activeRomId: number | null = null;
let sessionStartTime: number | null = null;
let suspendedAt: number | null = null;
let totalPausedMs = 0;

// Serialization chain — ensures lifecycle events don't interleave
let lifecycleChain: Promise<void> = Promise.resolve();

// Hook handles for cleanup
let lifetimeHook: { unregister: () => void } | null = null;
let suspendHook: { unregister: () => void } | null = null;
let resumeHook: { unregister: () => void } | null = null;

// Cached app ID -> rom ID map (refreshed on init and periodically)
let appIdToRomId: Record<string, number> = {};

function getRomIdForApp(appId: number): number | null {
  const romId = appIdToRomId[String(appId)];
  return romId ?? null;
}

function getAppIdForRom(romId: number): number | null {
  for (const [appIdStr, rid] of Object.entries(appIdToRomId)) {
    if (rid === romId) return Number(appIdStr);
  }
  return null;
}

async function refreshAppIdMap(): Promise<void> {
  try {
    appIdToRomId = await getAppIdRomIdMap();
  } catch (e) {
    logError(`Failed to refresh app ID map: ${e}`);
  }
}

async function handleGameStart(appId: number): Promise<void> {
  const romId = getRomIdForApp(appId);
  if (!romId) return; // Not a RomM shortcut

  logInfo(`Session start: romId=${romId}, appId=${appId}`);
  activeRomId = romId;
  sessionStartTime = Date.now();
  totalPausedMs = 0;

  // Record session start for playtime tracking
  try {
    await recordSessionStart(romId);
  } catch (e) {
    logError(`Failed to record session start: ${e}`);
  }
  // Pre-launch sync moved to CustomPlayButton.handlePlay
}

async function handleGameStop(): Promise<void> {
  if (!activeRomId) return;

  const romId = activeRomId;
  logInfo(`Session end: romId=${romId}`);

  // Clear active session immediately to avoid double-processing
  activeRomId = null;
  sessionStartTime = null;
  totalPausedMs = 0;

  // Record session end for playtime tracking
  try {
    const result = await recordSessionEnd(romId);
    if (result.success && result.total_seconds) {
      // Find the Steam app ID for this rom and update the display
      const appId = getAppIdForRom(romId);
      if (appId) {
        updatePlaytimeDisplay(appId, result.total_seconds);
      }
    }
  } catch (e) {
    logError(`Failed to record session end: ${e}`);
  }

  // Post-session achievement sync (fire-and-forget, non-blocking)
  syncAchievementsAfterSession(romId)
    .then(() => logInfo(`Achievement sync complete for romId=${romId}`))
    .catch((e) => logError(`Achievement sync failed for romId=${romId}: ${e}`));

  // Post-exit save sync — backend handles settings check + connectivity.
  // IMPORTANT: save-sync MUST run before refreshMigrationState below. If the
  // user changed RetroArch sort settings mid-game and we migrate first, the
  // newest-wins resolver would delete the stale orphan locally — but since
  // save-sync has not yet run, the newest progress would only live on disk.
  // Uploading to RomM first gives us a safety net: even if local migration
  // goes sideways, the server holds the authoritative latest version.
  try {
    const result = await postExitSync(romId);
    if (result.offline) {
      toaster.toast({ title: "RomM Save Sync", body: "Server offline — saves will sync next time" });
      window.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
    } else if (result.success) {
      if (result.synced && result.synced > 0) {
        toaster.toast({ title: "RomM Save Sync", body: "Saves uploaded to RomM" });
      }
      window.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
    } else {
      toaster.toast({ title: "RomM Save Sync", body: "Failed to sync saves after exit" });
    }
    if (result.conflicts && result.conflicts.length > 0) {
      notifyConflicts(result.conflicts);
    }
  } catch (e) {
    logError(`Post-exit sync failed: ${e}`);
  }

  // Post-game migration detection — runs AFTER save-sync above (ordering is
  // load-bearing, see comment on the save-sync block). Runs unconditionally:
  // refreshMigrationState only reads config files + state and emits events on
  // genuine change — it does not touch user save files. Actual migration runs
  // only when the user explicitly clicks the migrate button in Settings.
  refreshMigrationState()
    .then(({ retrodeck, save_sort }) => {
      setMigrationStatus(retrodeck);
      setSaveSortMigrationStatus(save_sort);
    })
    .catch((e) => logError(`Post-exit migration refresh failed: ${e}`));
}

function notifyConflicts(conflicts: (PendingConflict | NewerInSlotConflict)[]): void {
  const regularCount = conflicts.filter(isPendingConflict).length;
  const newerCount = conflicts.filter(isNewerInSlotConflict).length;

  if (regularCount > 0 && newerCount > 0) {
    toaster.toast({
      title: "RomM Save Sync",
      body: `${regularCount} save conflict${regularCount === 1 ? "" : "s"} + ${newerCount} newer save${newerCount === 1 ? "" : "s"} detected`,
    });
  } else if (regularCount > 0) {
    toaster.toast({
      title: "RomM Save Sync",
      body: `${regularCount} save conflict${regularCount === 1 ? "" : "s"} need resolution`,
    });
  } else if (newerCount > 0) {
    toaster.toast({
      title: "RomM Save Sync",
      body: `${newerCount} newer save${newerCount === 1 ? "" : "s"} detected from another device`,
    });
  }
}

function handleSuspend(): void {
  if (activeRomId && sessionStartTime) {
    suspendedAt = Date.now();
    logInfo("Device suspended during session, pausing playtime");
  }
}

function handleResume(): void {
  if (activeRomId && suspendedAt) {
    const pauseDuration = Date.now() - suspendedAt;
    totalPausedMs += pauseDuration;
    logInfo(`Device resumed, paused for ${Math.round(pauseDuration / 1000)}s`);
    suspendedAt = null;
  }
}

/**
 * Initialize session manager — registers all lifecycle hooks.
 * Call once during plugin load.
 */
export async function initSessionManager(): Promise<void> {
  // Load initial app ID map
  await refreshAppIdMap();

  // Game lifecycle notifications
  lifetimeHook = SteamClient.GameSessions.RegisterForAppLifetimeNotifications(
    (update) => {
      lifecycleChain = lifecycleChain
        .then(async () => {
          if (update.bRunning) {
            // Game started — wait for Router.MainRunningApp to populate
            await delay(500);
            const running = typeof Router !== "undefined" ? Router.MainRunningApp : null;
            const appId = running?.appid ?? update.unAppID;
            if (appId) {
              // Refresh map in case a sync happened since init
              await refreshAppIdMap();
              await handleGameStart(appId);
            }
          } else {
            // Game stopped
            await handleGameStop();
          }
        })
        .catch((e) => {
          logError(`Lifecycle event error: ${e}`);
        });
    },
  );

  // Suspend/resume for accurate playtime
  try {
    suspendHook = SteamClient.System.RegisterForOnSuspendRequest(handleSuspend);
    resumeHook = SteamClient.System.RegisterForOnResumeFromSuspend(handleResume);
  } catch (e) {
    console.warn("[romm-sync] Suspend/resume hooks unavailable:", e);
  }

  logInfo("Session manager initialized");
}

/**
 * Destroy session manager — unregisters all hooks.
 * Call during plugin unload.
 */
export function destroySessionManager(): void {
  if (lifetimeHook) {
    lifetimeHook.unregister();
    lifetimeHook = null;
  }
  if (suspendHook) {
    suspendHook.unregister();
    suspendHook = null;
  }
  if (resumeHook) {
    resumeHook.unregister();
    resumeHook = null;
  }

  activeRomId = null;
  sessionStartTime = null;
  suspendedAt = null;
  totalPausedMs = 0;
  lifecycleChain = Promise.resolve();

  logInfo("Session manager destroyed");
}
