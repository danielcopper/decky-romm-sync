/**
 * Session manager — detects game start/stop for RomM shortcuts and triggers
 * save sync + playtime tracking via backend callables.
 *
 * Uses SteamClient.GameSessions.RegisterForAppLifetimeNotifications to detect
 * game lifecycle events and Router.MainRunningApp for reliable app ID resolution.
 */

import { toaster } from "@decky/api";
import {
  preLaunchSync,
  postExitSync,
  recordSessionStart,
  recordSessionEnd,
  getAppIdRomIdMap,
  getSaveSyncSettings,
  getPendingConflicts,
} from "../api/backend";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import { showConflictResolutionModal } from "../components/ConflictModal";

declare var Router: {
  MainRunningApp: { appid: number; display_name: string } | null;
};

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Active session tracking
let activeRomId: number | null = null;
let sessionStartTime: number | null = null;
let suspendedAt: number | null = null;
let totalPausedMs = 0;

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
    console.error("[RomM] Failed to refresh app ID map:", e);
  }
}

async function handleGameStart(appId: number): Promise<void> {
  const romId = getRomIdForApp(appId);
  if (!romId) return; // Not a RomM shortcut

  console.log(`[RomM] Session start: romId=${romId}, appId=${appId}`);
  activeRomId = romId;
  sessionStartTime = Date.now();
  totalPausedMs = 0;

  // Record session start for playtime tracking
  try {
    await recordSessionStart(romId);
  } catch (e) {
    console.error("[RomM] Failed to record session start:", e);
  }

  // Pre-launch save sync (if enabled)
  try {
    const settings = await getSaveSyncSettings();
    if (settings.save_sync_enabled && settings.sync_before_launch) {
      const result = await preLaunchSync(romId);
      if (result.success) {
        if (result.synced && result.synced > 0) {
          toaster.toast({ title: "RomM Save Sync", body: "Saves downloaded from RomM" });
        }
      } else {
        console.warn("[RomM] Pre-launch sync issue:", result.message);
        toaster.toast({ title: "RomM Save Sync", body: "Failed to sync saves \u2014 playing with local saves" });
      }

      // Check for pending conflicts (ask_me mode shows modal, others just toast)
      try {
        const conflictsResult = await getPendingConflicts();
        if (conflictsResult.conflicts && conflictsResult.conflicts.length > 0) {
          if (settings.conflict_mode === "ask_me") {
            await showConflictResolutionModal(conflictsResult.conflicts);
          } else {
            toaster.toast({
              title: "RomM Save Sync",
              body: "Save conflict detected \u2014 resolve in Save Sync settings",
            });
          }
        }
      } catch {
        // non-critical
      }
    }
  } catch (e) {
    console.error("[RomM] Pre-launch sync failed:", e);
    toaster.toast({ title: "RomM Save Sync", body: "Failed to sync saves \u2014 playing with local saves" });
  }
}

async function handleGameStop(): Promise<void> {
  if (!activeRomId) return;

  const romId = activeRomId;
  console.log(`[RomM] Session end: romId=${romId}`);

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
    console.error("[RomM] Failed to record session end:", e);
  }

  // Post-exit save sync (if enabled)
  try {
    const settings = await getSaveSyncSettings();
    if (settings.save_sync_enabled && settings.sync_after_exit) {
      const result = await postExitSync(romId);
      if (result.success) {
        if (result.synced && result.synced > 0) {
          toaster.toast({ title: "RomM Save Sync", body: "Saves uploaded to RomM" });
        }
      } else {
        toaster.toast({ title: "RomM Save Sync", body: "Failed to sync saves after exit" });
      }

      // Check for pending conflicts (ask_me mode)
      try {
        const conflictsResult = await getPendingConflicts();
        if (conflictsResult.conflicts && conflictsResult.conflicts.length > 0) {
          notifyConflicts(conflictsResult.conflicts.length);
        }
      } catch {
        // non-critical
      }
    }
  } catch (e) {
    console.error("[RomM] Post-exit sync failed:", e);
  }
}

function notifyConflicts(count: number): void {
  toaster.toast({
    title: "RomM Save Sync",
    body: `${count} save conflict${count !== 1 ? "s" : ""} need resolution`,
  });
}

function handleSuspend(): void {
  if (activeRomId && sessionStartTime) {
    suspendedAt = Date.now();
    console.log("[RomM] Device suspended during session, pausing playtime");
  }
}

function handleResume(): void {
  if (activeRomId && suspendedAt) {
    const pauseDuration = Date.now() - suspendedAt;
    totalPausedMs += pauseDuration;
    console.log(`[RomM] Device resumed, paused for ${Math.round(pauseDuration / 1000)}s`);
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
    async (update) => {
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
    },
  );

  // Suspend/resume for accurate playtime
  suspendHook = SteamClient.System.RegisterForOnSuspendRequest(handleSuspend);
  resumeHook = SteamClient.System.RegisterForOnResumeFromSuspend(handleResume);

  console.log("[RomM] Session manager initialized");
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

  console.log("[RomM] Session manager destroyed");
}
