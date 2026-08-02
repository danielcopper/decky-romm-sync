/**
 * Session manager — detects game start/stop for RomM shortcuts and triggers
 * save sync + playtime tracking via backend callables.
 *
 * Uses SteamClient.GameSessions.RegisterForAppLifetimeNotifications to detect
 * game lifecycle events — the notification's own `unAppID` identifies the app on
 * both edges. The guarded `runningApps` reader (`SteamUIStore.RunningApps`) is
 * used only for LIVENESS at reload-adoption, never to identify a launching app.
 */

import { toaster } from "@decky/api";
import { recordSessionStart, getAppIdRomIdMap, finalizeGameSession, logInfo, logError, debugLog } from "../api/backend";
import { saveSyncToastBody } from "./saveSyncToast";
import { setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import { detach } from "./detach";
import { readRunningApps, type RunningApp } from "./runningApps";
import { delay } from "./pacedOps";

// Active session tracking — ONE ENTRY PER RUNNING APP (#1624). Two RomM games at
// once each hold their own entry, so a second start no longer displaces the
// first (which dropped its playtime and its post-exit save sync entirely).
interface ActiveSession {
  /** The Steam app that opened the session — what a lifecycle stop is matched against. */
  appId: number;
  romId: number;
  /** Wall-clock start, mirrored into the durable breadcrumb. */
  startMs: number;
}

// Keyed by appId, because that is what every write path is handed: the lifetime
// notification's `unAppID` on both edges, and the running-app reading at
// adoption. The stop path in particular must match against the appId RECORDED
// WITH THE SESSION (#1621) — a romId key would force a reverse lookup through
// the `appId → romId` map, which can go stale mid-session and would then drop a
// live session instead of an unrelated one.
//
// `const` + `clear()` on teardown: the binding is never reassigned, so a handler
// already queued on the lifecycle chain cannot end up writing into a map that
// has been swapped out from under it.
const activeSessions = new Map<number, ActiveSession>();

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
 * Does this manager currently track a live session for `romId`? Read
 * synchronously by the launch interceptor, to skip its cancel-then-gate funnel
 * for a Play press on an already-running game (#1148 round 2) — re-syncing
 * mid-session would upload the save while the emulator holds the file open, and
 * Steam blocks the relaunch as "already running" anyway — and by the Play
 * button, to seed and self-heal its Resume overlay.
 */
export function isSessionActive(romId: number): boolean {
  for (const session of activeSessions.values()) {
    if (session.romId === romId) return true;
  }
  return false;
}

async function refreshAppIdMap(): Promise<void> {
  try {
    appIdToRomId = await getAppIdRomIdMap();
  } catch (e) {
    logError(`Failed to refresh app ID map: ${e}`);
  }
}

// Durable attestation of the open sessions — survives a plugin reload so the
// re-initialized manager can adopt the still-running games and finalize their
// stops. A single versioned localStorage row; every access is wrapped so a
// storage failure degrades to the no-attestation path instead of throwing.
const SESSION_BREADCRUMB_KEY = "decky-romm-sync:active-session";
const SESSION_BREADCRUMB_VERSION = 2;

/** Read one stored entry into the current shape, or `null` if it isn't one. */
function toSessionEntry(value: unknown): ActiveSession | null {
  if (typeof value !== "object" || value === null) return null;
  const { appId, romId, startMs } = value as Record<string, unknown>;
  if (typeof appId !== "number" || typeof romId !== "number" || typeof startMs !== "number") return null;
  return { appId, romId, startMs };
}

/**
 * The sessions the durable row attests, or an empty list when it attests none.
 *
 * The stored version is branched on FIRST, before any field is looked at, and
 * every version a released build could have written gets its own lift into the
 * current shape. Validating version and fields in one expression would make
 * every row written by the previous version fail — and a failed validation is
 * indistinguishable from "no attestation", so the live session's pre-upgrade
 * span would be silently discarded on the first reload after an upgrade.
 *
 * Entries are read individually: one malformed entry never voids its siblings.
 */
function readSessionBreadcrumbs(): ActiveSession[] {
  try {
    const raw = localStorage.getItem(SESSION_BREADCRUMB_KEY);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return [];
    const crumb = parsed as Record<string, unknown>;
    if (crumb.v === 2) {
      if (!Array.isArray(crumb.sessions)) return [];
      return crumb.sessions.map(toSessionEntry).filter((s) => s !== null);
    }
    if (crumb.v === 1) {
      // v1 carried a single session inline — lift it into a one-entry list. No
      // writer emits v1 any more; the next persist rewrites the row as v2.
      const lifted = toSessionEntry(crumb);
      return lifted === null ? [] : [lifted];
    }
    // Unknown version: written by a build this one knows nothing about, so its
    // fields cannot be trusted. Same standing as no attestation at all.
    return [];
  } catch (e) {
    logError(`Failed to read session breadcrumb: ${e}`);
    return [];
  }
}

/**
 * Rewrite the durable row from the session map.
 *
 * A projection, never a read-modify-write: the map is the source of truth and
 * the whole row is rewritten after every mutation, before any await. A failed
 * write leaves the previous row intact (`setItem` is atomic per key) and
 * self-heals at the next mutation.
 *
 * An empty map removes the row rather than storing an empty list, so "no live
 * session" stays the absent state every reader already understands.
 */
function persistSessions(): void {
  if (activeSessions.size === 0) {
    try {
      localStorage.removeItem(SESSION_BREADCRUMB_KEY);
    } catch (e) {
      logError(`Failed to clear session breadcrumb: ${e}`);
    }
    return;
  }
  try {
    const row = { v: SESSION_BREADCRUMB_VERSION, sessions: [...activeSessions.values()] };
    localStorage.setItem(SESSION_BREADCRUMB_KEY, JSON.stringify(row));
  } catch (e) {
    logError(`Failed to write session breadcrumb: ${e}`);
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
  activeSessions.set(appId, { appId, romId, startMs: Date.now() });
  dispatchSessionChanged(true, appId, romId);

  // Attest the open sessions so a reload mid-game can adopt and finalize them.
  persistSessions();

  // Record session start for playtime tracking
  try {
    await recordSessionStart(romId);
  } catch (e) {
    logError(`Failed to record session start: ${e}`);
  }
  // Pre-launch sync moved to CustomPlayButton.handlePlay
}

/**
 * Finalize the session the stopped app opened, if it opened one.
 *
 * `RegisterForAppLifetimeNotifications` fires for EVERY app Steam tracks, so an
 * unrelated app's exit reaches here too (#1621). The lookup is by the appId
 * RECORDED WITH THE SESSION, never a fresh `getRomIdForApp` lookup: the cached
 * map can be stale at stop time, and a stale miss would drop a real session —
 * recording no playtime and skipping the post-exit sync entirely.
 *
 * Any other app's exit — a foreign app, or a concurrent RomM game — leaves every
 * open session untouched. Finalizing one of those would record its playtime
 * early AND run the post-exit save sync while its emulator still holds the save
 * file open, capturing a half-written file.
 */
async function handleGameStop(stoppedAppId: number): Promise<void> {
  const session = activeSessions.get(stoppedAppId);
  if (!session) {
    detach(debugLog(`Session stop ignored: appId=${stoppedAppId} opened no session`));
    return;
  }

  const { appId, romId } = session;
  logInfo(`Session end: romId=${romId}`);

  // Flip any open Resume button back to Play (#1313). The appId comes from the
  // session itself, so this needs no reverse-map lookup.
  dispatchSessionChanged(false, appId, romId);

  // Drop this session immediately to avoid double-processing, and rewrite the
  // row before the finalize await — the stop is observed, so there is nothing
  // left to adopt for this app; any concurrent session stays attested.
  activeSessions.delete(appId);
  persistSessions();

  try {
    const result = await finalizeGameSession(romId);

    // Playtime display update — appStore mutation must stay frontend.
    if (result.total_seconds != null) {
      updatePlaytimeDisplay(appId, result.total_seconds);
    }

    // Post-exit save-sync toast. The directional success toast is rendered
    // frontend-side from the transfer counts via the shared helper (the single
    // source of that copy, #1481); the offline/failure body stays backend-owned
    // (failure_toast). The two are mutually exclusive — a successful run carries
    // failure_toast=null — so only one fires.
    const directionalBody = result.sync.success
      ? saveSyncToastBody(result.sync.uploaded, result.sync.downloaded)
      : null;
    if (directionalBody) {
      toaster.toast({ title: "RomM Save Sync", body: directionalBody });
    } else if (result.sync.failure_toast) {
      toaster.toast({ title: "RomM Save Sync", body: result.sync.failure_toast });
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
// full `plugin_loader` restart `SteamUIStore.RunningApps` reads empty for
// several seconds even though the game is still running (#1054 / #1148 round 2
// device evidence), so a single early read wrongly orphaned a live session. Poll
// the reader until a running app appears or the window elapses.
const ADOPTION_POLL_INTERVAL_MS = 500;
export const ADOPTION_POLL_MAX_MS = 15_000;

/**
 * Poll the running-app reader until it reports a running app or the window
 * elapses. Returns the foreground app (any app, RomM or not — the caller applies
 * the adoption matrix) or `null` on timeout, alongside the last round's
 * `diagnostics` so a timed-out adoption logs what the store actually reported
 * (absent / empty / threw). Each round's diagnostics are also emitted at debug
 * level.
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
 * (`SteamUIStore.RunningApps`) is the liveness authority; the localStorage
 * breadcrumb is the attestation of a start we actually observed. Every finalize
 * fold thus stays anchored to a marker stamped by an observed start.
 *
 * The liveness read is POLLED, not a single read: after a loader restart the
 * store reports an EMPTY running-app list for seconds while the game is still
 * up (#1054 / #1148 round 2), so a one-shot read raced the restart and wrongly
 * orphaned a still-running session.
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
  const crumbs = readSessionBreadcrumbs();
  const crumb = crumbs[0] ?? null;

  if (running && runningRomId !== null) {
    const appId = running.appid;
    if (crumb && crumb.appId === appId && crumb.romId === runningRomId) {
      // (a) The breadcrumb attests this exact running session. Restore the
      // in-memory state, keeping the attested startMs — re-stamping the durable
      // marker would discard the pre-reload playtime the backend already holds.
      activeSessions.set(appId, { appId, romId: runningRomId, startMs: crumb.startMs });
      persistSessions();
      dispatchSessionChanged(true, appId, runningRomId);
      logInfo(`Adopted running session from breadcrumb: romId=${runningRomId}, appId=${appId}`);
    } else {
      // (a′) A game is running but no usable breadcrumb (missing / mismatched /
      // corrupt). Adopt it and re-stamp the marker to a truthful lower bound
      // from now, then attest it so a later reload adopts via (a) rather than
      // re-stamping again.
      const adopted: ActiveSession = { appId, romId: runningRomId, startMs: Date.now() };
      activeSessions.set(appId, adopted);
      persistSessions();
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
    persistSessions();
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
          // The notification's own appid identifies the app that started. Do NOT
          // consult `SteamUIStore.RunningApps` here: its head is the most recently
          // FOREGROUNDED app and a fresh arrival is appended at the tail, so the
          // head names some other running game — attributing the start to it opens
          // a session on the wrong rom and never opens one for this app, whose
          // stop then finalizes nothing. Reading it also cost a 500ms delay that
          // stalled the whole serialized lifecycle chain (a stop queued behind a
          // start waited for it too); both are gone.
          const appId = update.unAppID;
          if (appId) {
            // Refresh map in case a sync happened since init
            await refreshAppIdMap();
            await handleGameStart(appId);
          }
        } else {
          // An app stopped — `handleGameStop` decides whether it is ours.
          await handleGameStop(update.unAppID);
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

  activeSessions.clear();
  lifecycleChain = Promise.resolve();
  // Signal any in-flight adoption poll to abort instead of mutating torn-down state.
  sessionEpoch++;

  logInfo("Session manager destroyed");
}
