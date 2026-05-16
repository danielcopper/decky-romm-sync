/**
 * Session manager — detects game start/stop for RomM shortcuts and triggers
 * save sync + playtime tracking via backend callables.
 *
 * Uses SteamClient.GameSessions.RegisterForAppLifetimeNotifications to detect
 * game lifecycle events and Router.MainRunningApp for reliable app ID resolution.
 */

import { toaster } from "@decky/api";
import {
  recordSessionStart,
  getAppIdRomIdMap,
  finalizeGameSession,
  logInfo,
  logError,
} from "../api/backend";
import { setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
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

  try {
    const result = await finalizeGameSession(romId);

    // Playtime display update — appStore mutation must stay frontend.
    if (result.total_seconds != null) {
      const appId = getAppIdForRom(romId);
      if (appId) {
        updatePlaytimeDisplay(appId, result.total_seconds);
      }
    }

    // Post-exit sync toast (backend rendered).
    if (result.sync.toast_title && result.sync.toast_body) {
      toaster.toast({ title: result.sync.toast_title, body: result.sync.toast_body });
    }

    // Save-sync event dispatch — fires for offline OR success (pre-PR parity:
    // offline branch dispatched, success branch dispatched, failure did not).
    if (result.sync.offline || result.sync.success) {
      window.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
    }

    // Additive conflicts toast — backend renders the count string.
    if (result.sync.conflicts_toast) {
      toaster.toast({ title: "RomM Save Sync", body: result.sync.conflicts_toast });
    }

    // Migration store updates — backend ran refresh_state, frontend just
    // feeds the typed payloads into the stores. When backend refresh
    // failed (``migration == null``) leave the stores untouched, matching
    // the pre-PR ``refreshMigrationState().catch`` behavior where a
    // refresh failure logged a warning without clearing any stale
    // "pending" badge.
    if (result.migration) {
      setMigrationStatus(result.migration.retrodeck);
      setSaveSortMigrationStatus(result.migration.save_sort);
    }
  } catch (e) {
    logError(`Failed to finalize game session: ${e}`);
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
