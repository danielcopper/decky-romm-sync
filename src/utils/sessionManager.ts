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
  logWarn,
  logError,
  debugLog,
} from "../api/backend";
import { setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import { detach } from "./detach";

declare let Router: {
  MainRunningApp: { appid: number; display_name: string } | null;
};

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// Active session tracking
let activeRomId: number | null = null;
let sessionStartTime: number | null = null;
let suspendedAt: number | null = null;
// Accumulated device-suspend wall-clock for the current session (ms). Folded
// into `suspendedSeconds` and subtracted from playtime at session stop.
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
  pausedMs: number;
}

function isSessionBreadcrumb(value: unknown): value is SessionBreadcrumb {
  if (typeof value !== "object" || value === null) return false;
  const c = value as Record<string, unknown>;
  return (
    c.v === SESSION_BREADCRUMB_VERSION &&
    typeof c.appId === "number" &&
    typeof c.romId === "number" &&
    typeof c.startMs === "number" &&
    typeof c.pausedMs === "number"
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

/** Persist the running suspend accumulator onto the open breadcrumb (no-op if none). */
function updateSessionBreadcrumbPaused(pausedMs: number): void {
  const crumb = readSessionBreadcrumb();
  if (crumb) writeSessionBreadcrumb({ ...crumb, pausedMs });
}

async function handleGameStart(appId: number): Promise<void> {
  const romId = getRomIdForApp(appId);
  if (!romId) return; // Not a RomM shortcut

  logInfo(`Session start: romId=${romId}, appId=${appId}`);
  activeRomId = romId;
  sessionStartTime = Date.now();
  suspendedAt = null;
  totalPausedMs = 0;

  // Attest the open session so a reload mid-game can adopt and finalize it.
  writeSessionBreadcrumb({ v: SESSION_BREADCRUMB_VERSION, appId, romId, startMs: sessionStartTime, pausedMs: 0 });

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

  // Fold any in-flight suspend (device stopped the game while suspended)
  // into the accumulator before computing the total.
  if (suspendedAt !== null) {
    totalPausedMs += Date.now() - suspendedAt;
  }
  const suspendedSeconds = Math.round(totalPausedMs / 1000);

  // Clear active session immediately to avoid double-processing. The breadcrumb
  // goes with it — the stop is observed, so there is nothing left to adopt.
  activeRomId = null;
  sessionStartTime = null;
  suspendedAt = null;
  totalPausedMs = 0;
  clearSessionBreadcrumb();

  try {
    const result = await finalizeGameSession(romId, suspendedSeconds);

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
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
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
    // Persist the completed suspend cycle so an adopted session keeps it. An
    // in-flight suspend (still open at reload) is intentionally not persisted.
    updateSessionBreadcrumbPaused(totalPausedMs);
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
 */
async function adoptOrphanedSession(): Promise<void> {
  const running = typeof Router === "undefined" ? null : Router.MainRunningApp; // NOSONAR(typescript:S7741) — Router is an undeclared Steam SP global; direct === undefined would throw ReferenceError.
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
      suspendedAt = null;
      totalPausedMs = crumb.pausedMs;
      logInfo(`Adopted running session from breadcrumb: romId=${runningRomId}, appId=${appId}`);
    } else {
      // (a′) A game is running but no usable breadcrumb (missing / mismatched /
      // corrupt). Adopt it and re-stamp the marker to a truthful lower bound
      // from now, then attest with a fresh breadcrumb so a later reload adopts
      // via (a) rather than re-stamping again.
      activeRomId = runningRomId;
      sessionStartTime = Date.now();
      suspendedAt = null;
      totalPausedMs = 0;
      writeSessionBreadcrumb({
        v: SESSION_BREADCRUMB_VERSION,
        appId,
        romId: runningRomId,
        startMs: sessionStartTime,
        pausedMs: 0,
      });
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
 * Read `SteamClient.System` as a plain record, tolerating a runtime where it is
 * absent, not an object, or a throwing getter — any of which yields an empty
 * record. Keeps the #1148 presence check from throwing out of
 * `initSessionManager` (before adoption) when SteamClient is malformed.
 */
function readSystemApi(): Record<string, unknown> {
  try {
    const system = (SteamClient as unknown as Record<string, unknown>).System;
    if (system !== null && typeof system === "object") return system as Record<string, unknown>;
  } catch {
    // Odd SteamClient shape (e.g. a throwing getter) — fall through to {}.
  }
  return {};
}

/**
 * Report an unregister handle's runtime shape without trusting its declared
 * type — the whole point of the #1148 probe is that the runtime may not match
 * the `.d.ts`, so the handle is typed `unknown` and every shape is handled:
 * `null`, a non-object (e.g. a build that returns `undefined`), or an object
 * that may or may not carry an `unregister` function.
 */
function describeHandle(handle: unknown): string {
  if (handle === null) return "null";
  if (typeof handle !== "object") return `type=${typeof handle}`;
  const rec = handle as Record<string, unknown>;
  return `{unregister:${typeof rec.unregister === "function" ? "fn" : "missing"}}`;
}

/**
 * Enumerate the suspend/resume/sleep/wake members `SteamClient` actually exposes
 * at runtime (#1148 investigation point 3 — is there a newer/alternative API on
 * this SteamOS build?). Never throws: an odd or absent SteamClient shape degrades
 * to a marker string that still gets logged.
 */
function probeSuspendSurface(): string {
  try {
    const client = SteamClient as unknown as Record<string, unknown>;
    const pattern = /suspend|resume|sleep|wake/i;
    const matches: string[] = [];
    for (const namespace of ["System", "User"]) {
      const ns = client[namespace];
      if (ns === null || typeof ns !== "object") continue;
      for (const key of Object.keys(ns)) {
        if (pattern.test(key)) matches.push(`${namespace}.${key}`);
      }
    }
    return matches.length > 0 ? matches.join(", ") : "(none)";
  } catch (e) {
    return `probe failed: ${e}`;
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
          // Game started — wait for Router.MainRunningApp to populate
          await delay(500);
          const running = typeof Router === "undefined" ? null : Router.MainRunningApp; // NOSONAR(typescript:S7741) — Router is an undeclared Steam SP global; direct === undefined would throw ReferenceError.
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

  // Suspend/resume for accurate playtime. #1148 diagnostics: on-device these
  // hooks never fired, so decision C's suspend-subtraction shipped dormant.
  // Probe presence, registration outcome, and the returned handle shape once per
  // init so a single Game-Mode run tells us whether the API is missing, throwing,
  // or registering but never firing. The typed SteamClient claims the members
  // always exist, so presence is read through an `unknown` view — the runtime is
  // the authority here, not the `.d.ts`. `SteamClient.System` itself is read
  // defensively (absent, non-object, or a throwing getter → empty record) so a
  // broken SteamClient still emits the "hooks missing" headline + surface probe
  // and lets init reach the #1054 adoption, rather than throwing before either.
  const systemApi = readSystemApi();
  const hasSuspendReg = typeof systemApi.RegisterForOnSuspendRequest === "function";
  const hasResumeReg = typeof systemApi.RegisterForOnResumeFromSuspend === "function";
  if (!hasSuspendReg || !hasResumeReg) {
    // Actionable headline — the members decision C depends on are absent on this
    // build. Warn so it lands even at the default log level.
    logWarn(
      `Suspend/resume hooks missing on this build: RegisterForOnSuspendRequest=${hasSuspendReg} RegisterForOnResumeFromSuspend=${hasResumeReg}`,
    );
  }
  try {
    if (hasSuspendReg) suspendHook = SteamClient.System.RegisterForOnSuspendRequest(handleSuspend);
    if (hasResumeReg) resumeHook = SteamClient.System.RegisterForOnResumeFromSuspend(handleResume);
    detach(
      debugLog(
        `Suspend/resume registration: suspend=${describeHandle(suspendHook)} resume=${describeHandle(resumeHook)}`,
      ),
    );
  } catch (e) {
    logWarn(`Suspend/resume registration threw: ${e}`);
  }
  // Investigation point 3 — enumerate the suspend/resume members SteamClient
  // actually exposes at runtime. Debug-gated; never throws.
  detach(debugLog(`SteamClient suspend/resume surface: ${probeSuspendSurface()}`));

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
