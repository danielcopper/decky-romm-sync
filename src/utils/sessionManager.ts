/**
 * Session manager — detects game start/stop for RomM shortcuts and triggers
 * save sync + playtime tracking via backend callables.
 *
 * Uses SteamClient.GameSessions.RegisterForAppLifetimeNotifications to detect
 * game lifecycle events and the defensive `runningApps` reader (multiple Steam
 * surfaces, not just `Router.MainRunningApp`) for reliable app-ID resolution.
 */

import { toaster } from "@decky/api";
import { recordSessionStart, getAppIdRomIdMap, finalizeGameSession, logInfo, logError, debugLog } from "../api/backend";
import { setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import { detach } from "./detach";
import { readPrimaryRunningApp, readRunningApps, type RunningApp } from "./runningApps";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Active session tracking
let activeRomId: number | null = null;
let sessionStartTime: number | null = null;

// Serialization chain — ensures lifecycle events don't interleave
let lifecycleChain: Promise<void> = Promise.resolve();

// Bumped by destroySessionManager. The adoption poll can run for up to 15s, past
// a teardown; adoption captures this at entry and re-checks it after the poll so a
// destroy mid-poll aborts before mutating any module state, the breadcrumb, or the
// backend.
let sessionEpoch = 0;

// Hook handles for cleanup
let lifetimeHook: { unregister: () => void } | null = null;

// Cached app ID -> rom ID map (refreshed on init and periodically)
let appIdToRomId: Record<string, number> = {};

function getRomIdForApp(appId: number): number | null {
  const romId = appIdToRomId[String(appId)];
  return romId ?? null;
}

/**
 * Snapshot of the cached appId -> romId map (the same shape the backend's
 * `get_app_id_rom_id_map` callable returns — string-keyed appIds). The global
 * launch watcher reads this synchronously to resolve a launching app's romId
 * without an await, so its cancel-then-gate path never races the map refresh.
 * Returns the live reference; callers treat it as read-only.
 */
export function getAppIdRomIdMapSnapshot(): Record<string, number> {
  return appIdToRomId;
}

/**
 * The romId of the session this manager currently tracks as live, or `null` when
 * none is open. The launch interceptor reads it synchronously to skip its
 * cancel-then-gate funnel for a Play press on an already-running game (#1148
 * round 2) — re-syncing mid-session would upload the save while the emulator
 * holds the file open, and Steam blocks the relaunch as "already running" anyway.
 */
export function getActiveSessionRomId(): number | null {
  return activeRomId;
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

// Durable attestation of the open session — survives a plugin reload so the
// re-initialized manager can adopt the still-running game and finalize its
// stop. A single versioned localStorage row; every access is wrapped so a
// storage failure degrades to the no-attestation path instead of throwing.
const SESSION_BREADCRUMB_KEY = "decky-romm-sync:active-session";
const SESSION_BREADCRUMB_VERSION = 1;

interface SessionBreadcrumb {
  v: number;
  appId: number;
  romId: number;
  startMs: number;
}

function isSessionBreadcrumb(value: unknown): value is SessionBreadcrumb {
  if (typeof value !== "object" || value === null) return false;
  const c = value as Record<string, unknown>;
  return (
    c.v === SESSION_BREADCRUMB_VERSION &&
    typeof c.appId === "number" &&
    typeof c.romId === "number" &&
    typeof c.startMs === "number"
  );
}

function readSessionBreadcrumb(): SessionBreadcrumb | null {
  try {
    const raw = localStorage.getItem(SESSION_BREADCRUMB_KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    return isSessionBreadcrumb(parsed) ? parsed : null;
  } catch (e) {
    logError(`Failed to read session breadcrumb: ${e}`);
    return null;
  }
}

function writeSessionBreadcrumb(crumb: SessionBreadcrumb): void {
  try {
    localStorage.setItem(SESSION_BREADCRUMB_KEY, JSON.stringify(crumb));
  } catch (e) {
    logError(`Failed to write session breadcrumb: ${e}`);
  }
}

function clearSessionBreadcrumb(): void {
  try {
    localStorage.removeItem(SESSION_BREADCRUMB_KEY);
  } catch (e) {
    logError(`Failed to clear session breadcrumb: ${e}`);
  }
}

/**
 * Notify open surfaces that a RomM play session started or ended, so they can
 * flip to/from the state-aware Resume button without polling (#1313). A frontend
 * DOM CustomEvent — `CustomPlayButton` matches it on `romId`.
 */
function dispatchSessionChanged(running: boolean, appId: number, romId: number): void {
  globalThis.dispatchEvent(new CustomEvent("romm_session_changed", { detail: { running, appId, romId } }));
}

async function handleGameStart(appId: number): Promise<void> {
  const romId = getRomIdForApp(appId);
  if (!romId) return; // Not a RomM shortcut

  logInfo(`Session start: romId=${romId}, appId=${appId}`);
  activeRomId = romId;
  sessionStartTime = Date.now();
  dispatchSessionChanged(true, appId, romId);

  // Attest the open session so a reload mid-game can adopt and finalize it.
  writeSessionBreadcrumb({ v: SESSION_BREADCRUMB_VERSION, appId, romId, startMs: sessionStartTime });

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

  // Flip any open Resume button back to Play before we lose the appId mapping
  // (#1313). Best-effort — a missing reverse-map entry leaves the button to
  // self-heal on remount; the button keys on romId regardless.
  const stoppedAppId = getAppIdForRom(romId);
  if (stoppedAppId !== null) dispatchSessionChanged(false, stoppedAppId, romId);

  // Clear active session immediately to avoid double-processing. The breadcrumb
  // goes with it — the stop is observed, so there is nothing left to adopt.
  activeRomId = null;
  sessionStartTime = null;
  clearSessionBreadcrumb();

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

    // Save-sync event dispatch — fires unconditionally so open surfaces refresh
    // to the honest post-sync state. A failed post-exit sync must refresh too
    // (#1334): the panel would otherwise keep showing a stale green "synced" for
    // a file that is now pending upload.
    globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));

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

// Adoption polls Steam's running-app before deciding a session's fate: after a
// full `plugin_loader` restart the running-app surfaces are not populated for
// several seconds even though the game is still running (#1054 / #1148 round 2
// device evidence), so a single early read wrongly orphaned a live session. Poll
// the defensive multi-source reader until a running app appears or the window
// elapses.
const ADOPTION_POLL_INTERVAL_MS = 500;
export const ADOPTION_POLL_MAX_MS = 15_000;

/**
 * Poll every running-app source until one reports a running app or the window
 * elapses. Returns the primary app (any app, RomM or not — the caller applies
 * the adoption matrix) or `null` on timeout, alongside the last round's
 * per-source `diagnostics` so a timed-out adoption logs what EVERY candidate
 * reported. Each round's diagnostics are also emitted at debug level.
 */
async function pollForRunningApp(): Promise<{ app: RunningApp | null; diagnostics: string }> {
  const started = Date.now();
  for (;;) {
    const reading = readRunningApps();
    detach(debugLog(`adoption poll round: ${reading.diagnostics}`));
    if (reading.apps.length > 0) return { app: reading.apps[0]!, diagnostics: reading.diagnostics };
    if (Date.now() - started >= ADOPTION_POLL_MAX_MS) return { app: null, diagnostics: reading.diagnostics };
    await delay(ADOPTION_POLL_INTERVAL_MS);
  }
}

/**
 * Adopt a play session orphaned by a plugin reload mid-game.
 *
 * `destroySessionManager` wipes the in-memory session on unload, so the
 * game-stop after a reload would otherwise never finalize — the pre-reload
 * playtime is lost and the post-exit sync never runs. Steam's running-state
 * (`Router.MainRunningApp`) is the liveness authority; the localStorage
 * breadcrumb is the attestation of a start we actually observed. Every finalize
 * fold thus stays anchored to a marker stamped by an observed start.
 *
 * The liveness read is POLLED (not a single read) and consults MULTIPLE Steam
 * surfaces: after a loader restart `Router.MainRunningApp` stays null for seconds
 * and never repopulates without a fresh lifecycle event our reloaded context
 * missed (#1054 / #1148 round 2), so a one-shot single-source read raced the
 * restart and wrongly orphaned a still-running session.
 */
async function adoptOrphanedSession(): Promise<void> {
  const epoch = sessionEpoch;
  const pollStart = Date.now();
  const { app: running, diagnostics } = await pollForRunningApp();
  if (epoch !== sessionEpoch) {
    // destroySessionManager ran while the poll was in flight — abort before
    // touching module state, the breadcrumb, or the backend.
    detach(debugLog("adoption: cancelled by destroy"));
    return;
  }
  const waitedMs = Date.now() - pollStart;
  logInfo(
    running
      ? `adoption: running app appeared after ${waitedMs}ms [${diagnostics}]`
      : `adoption: no running app after ${waitedMs}ms [${diagnostics}]`,
  );
  const runningRomId = running ? getRomIdForApp(running.appid) : null;
  const crumb = readSessionBreadcrumb();

  if (running && runningRomId !== null) {
    const appId = running.appid;
    if (crumb && crumb.appId === appId && crumb.romId === runningRomId) {
      // (a) The breadcrumb attests this exact running session. Restore the
      // in-memory state and leave the durable marker untouched — re-stamping it
      // would discard the pre-reload playtime the backend already holds.
      activeRomId = runningRomId;
      sessionStartTime = crumb.startMs;
      dispatchSessionChanged(true, appId, runningRomId);
      logInfo(`Adopted running session from breadcrumb: romId=${runningRomId}, appId=${appId}`);
    } else {
      // (a′) A game is running but no usable breadcrumb (missing / mismatched /
      // corrupt). Adopt it and re-stamp the marker to a truthful lower bound
      // from now, then attest with a fresh breadcrumb so a later reload adopts
      // via (a) rather than re-stamping again.
      activeRomId = runningRomId;
      sessionStartTime = Date.now();
      writeSessionBreadcrumb({
        v: SESSION_BREADCRUMB_VERSION,
        appId,
        romId: runningRomId,
        startMs: sessionStartTime,
      });
      dispatchSessionChanged(true, appId, runningRomId);
      try {
        await recordSessionStart(runningRomId);
      } catch (e) {
        logError(`Failed to record session start on adoption: ${e}`);
      }
      logInfo(`Adopted running session without breadcrumb, re-stamped marker: romId=${runningRomId}, appId=${appId}`);
    }
    return;
  }

  // No RomM game is running (nothing running, or a non-RomM app). A breadcrumb
  // here names a session whose stop we never saw — a truthful finalize is
  // impossible without an observed end, so drop it rather than fabricate one.
  if (crumb) {
    clearSessionBreadcrumb();
    logInfo(`Session orphaned — playtime not recorded (romId=${crumb.romId})`);
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
  lifetimeHook = SteamClient.GameSessions.RegisterForAppLifetimeNotifications((update) => {
    lifecycleChain = lifecycleChain
      .then(async () => {
        if (update.bRunning) {
          // Game started — wait for the running-app surfaces to populate
          await delay(500);
          const running = readPrimaryRunningApp().app;
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
  });

  // Adopt a session orphaned by a plugin reload mid-game. Serialized on the
  // lifecycle chain so a stop notification arriving during adoption finalizes
  // after it rather than interleaving with the in-memory state it restores.
  const adoption = lifecycleChain
    .then(() => adoptOrphanedSession())
    .catch((e) => {
      logError(`Session adoption error: ${e}`);
    });
  lifecycleChain = adoption;
  await adoption;

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

  activeRomId = null;
  sessionStartTime = null;
  lifecycleChain = Promise.resolve();
  // Signal any in-flight adoption poll to abort instead of mutating torn-down state.
  sessionEpoch++;

  logInfo("Session manager destroyed");
}
