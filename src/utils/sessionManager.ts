/**
 * Session manager — detects game start/stop for RomM shortcuts and triggers
 * save sync + playtime tracking via backend callables.
 *
 * Uses SteamClient.GameSessions.RegisterForAppLifetimeNotifications to detect
 * game lifecycle events — the notification's own `unAppID` identifies the app on
 * both edges. The guarded `runningApps` reader (`SteamUIStore.RunningApps`) is
 * used only for LIVENESS at reload-adoption, never to identify a launching app.
 */

import { showToast, SAVE_SYNC_TOAST_TITLE } from "./toast";
import { recordSessionStart, getAppIdRomIdMap, finalizeGameSession, logInfo, logError, debugLog } from "../api/backend";
import { saveSyncToastBody } from "./saveSyncToast";
import { setMigrationStatus } from "./migrationStore";
import { setSaveSortMigrationStatus } from "./saveSortMigrationStore";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import { detach } from "./detach";
import { readRunningApps, type RunningAppsReading } from "./runningApps";
import { delay } from "./pacedOps";

// Active session tracking — ONE ENTRY PER RUNNING APP (#1624). Two RomM games at
// once each hold their own entry, so a second start no longer displaces the
// first (which dropped its playtime and its post-exit save sync entirely).
export interface ActiveSession {
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

/**
 * Open a session for the app that just started — unless it already has one.
 *
 * The idempotency is load-bearing (#1589). `record_session_start` RE-OPENS the
 * durable marker rather than extending it, so a second call for a live session
 * silently discards the span already played. Steam can report an app as started
 * twice (notably when a launch lands inside the plugin's own startup window and
 * reload-adoption has already opened the session), and the re-open is deliberate
 * backend behaviour that adoption relies on — so the guard belongs here.
 *
 * It is keyed on the appId and checked BEFORE the romId lookup: a map that
 * emptied mid-session must not be able to drop a live entry.
 */
async function handleGameStart(appId: number): Promise<void> {
  const open = activeSessions.get(appId);
  if (open) {
    detach(debugLog(`Session start ignored: appId=${appId} already has an open session (romId=${open.romId})`));
    // Re-announce it — a surface that missed the first event self-heals, and the
    // earlier startMs is kept.
    dispatchSessionChanged(true, open.appId, open.romId);
    return;
  }

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
      showToast(directionalBody, { title: SAVE_SYNC_TOAST_TITLE });
    } else if (result.sync.failure_toast) {
      showToast(result.sync.failure_toast, { title: SAVE_SYNC_TOAST_TITLE });
    }

    // Save-sync event dispatch — fires unconditionally so open surfaces refresh
    // to the honest post-sync state. A failed post-exit sync must refresh too
    // (#1334): the panel would otherwise keep showing a stale green "synced" for
    // a file that is now pending upload.
    globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));

    // Additive conflicts toast — backend renders the count string.
    if (result.sync.conflicts_toast) {
      showToast(result.sync.conflicts_toast, { title: SAVE_SYNC_TOAST_TITLE });
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
 * Poll the running-app reader until its reading has SETTLED or the window
 * elapses, and return that last reading (its `diagnostics` say what the store
 * reported — absent / empty / threw / the appids found; each round's are also
 * emitted at debug level).
 *
 * Settled means: something is running AND every app the breadcrumbs attest has
 * surfaced. Stopping at the first non-empty reading is not enough — the store
 * omits apps whose overview has not loaded yet, so a reading that already lists
 * one concurrent game can still be missing its sibling, which would then be
 * orphaned. The wait for stragglers shares the one existing budget.
 */
async function pollForRunningApps(wanted: Set<number>): Promise<RunningAppsReading> {
  const started = Date.now();
  for (;;) {
    const reading = readRunningApps();
    detach(debugLog(`adoption poll round: ${reading.diagnostics}`));
    const surfaced = new Set(reading.apps.map((app) => app.appid));
    if (surfaced.size > 0 && [...wanted].every((appId) => surfaced.has(appId))) return reading;
    if (Date.now() - started >= ADOPTION_POLL_MAX_MS) return reading;
    await delay(ADOPTION_POLL_INTERVAL_MS);
  }
}

/** What reconciling the attested sessions against the running apps decided. */
export interface AdoptionPlan {
  /** Attested and still running — restored exactly as attested. */
  adopted: ActiveSession[];
  /** Running and ours but unattested — restored with the marker re-stamped. */
  restamped: ActiveSession[];
  /** Attested but no longer running — dropped, never finalized. */
  orphans: ActiveSession[];
}

/**
 * Decide the fate of every attested session and every running app (#1624).
 *
 * Pure: reads no module state, performs no I/O, mutates nothing it is handed.
 * The caller commits the plan — this only says what should happen:
 *
 * - attested AND running → adopted as attested. The crumb's own romId is
 *   trusted, which is why `resolveRomId` is not consulted for it: the durable
 *   marker belongs to THAT rom, and re-stamping the map's current romId instead
 *   would open a marker on a different row and leave the original dangling (the
 *   rule #1621 established for the stop path — an `appId → romId` binding is 1:1
 *   at any instant but not stable over time).
 * - running, ours, but unattested → re-stamped: adopted with the marker moved to
 *   `nowMs`, a truthful lower bound, and attested afterwards so a later reload
 *   adopts it as attested rather than re-stamping again.
 * - attested but NOT running → orphaned. Its stop was never observed, and a
 *   truthful finalize is impossible without an observed end, so the attestation
 *   is dropped rather than an end time fabricated.
 * - running but not ours (`resolveRomId` returns `null`) → ignored entirely. A
 *   foreign app is never adopted and never causes anything else to be orphaned.
 *
 * `tracked` names the apps that already hold an open session; both passes skip
 * them, so adoption can never re-open one a start notification opened first
 * (#1589).
 *
 * That skip is UNREACHABLE as the manager is currently wired, and deliberately
 * kept: adoption and every notification handler run on the one serialized
 * `lifecycleChain`, adoption is enqueued in the same synchronous block that
 * registers the hook, and a teardown clears the session map — so a fresh init
 * always reconciles against an empty `tracked`. Two wiring changes would make it
 * live: taking adoption off that chain (giving a notification a window to
 * complete during the up-to-15s poll), or a handler that acts directly instead
 * of enqueueing. Do not delete it as dead code without making one of those
 * orderings impossible instead.
 */
export function planAdoption(
  crumbs: readonly ActiveSession[],
  runningAppIds: readonly number[],
  tracked: ReadonlySet<number>,
  resolveRomId: (appId: number) => number | null,
  nowMs: number,
): AdoptionPlan {
  const running = new Set(runningAppIds);
  const adopted: ActiveSession[] = [];
  const orphans: ActiveSession[] = [];
  for (const crumb of crumbs) {
    if (tracked.has(crumb.appId)) continue;
    (running.has(crumb.appId) ? adopted : orphans).push(crumb);
  }

  const attested = new Set(adopted.map((session) => session.appId));
  const restamped: ActiveSession[] = [];
  for (const appId of runningAppIds) {
    if (tracked.has(appId) || attested.has(appId)) continue;
    const romId = resolveRomId(appId);
    if (romId !== null) restamped.push({ appId, romId, startMs: nowMs });
  }

  return { adopted, restamped, orphans };
}

/**
 * Adopt the play sessions orphaned by a plugin reload mid-game.
 *
 * `destroySessionManager` wipes the in-memory sessions on unload, so the
 * game-stops after a reload would otherwise never finalize — the pre-reload
 * playtime is lost and the post-exit sync never runs. Steam's running-state
 * (`SteamUIStore.RunningApps`) is the liveness authority; the localStorage
 * breadcrumbs are the attestations of starts we actually observed. Every
 * finalize fold thus stays anchored to a marker stamped by an observed start.
 *
 * The liveness read is POLLED, not a single read: after a loader restart the
 * store reports an EMPTY running-app list for seconds while the game is still
 * up (#1054 / #1148 round 2), so a one-shot read raced the restart and wrongly
 * orphaned a still-running session.
 *
 * This is the orchestration around that: capture the epoch, poll, re-check the
 * epoch, ask {@link planAdoption} what to do, then commit / dispatch / log /
 * re-stamp. The reconcile matrix itself lives in that pure function.
 */
async function adoptOrphanedSessions(): Promise<void> {
  const epoch = sessionEpoch;
  const pollStart = Date.now();
  const crumbs = readSessionBreadcrumbs();
  const reading = await pollForRunningApps(new Set(crumbs.map((c) => c.appId)));
  if (epoch !== sessionEpoch) {
    // destroySessionManager ran while the poll was in flight — abort before
    // touching module state, the breadcrumbs, or the backend.
    detach(debugLog("adoption: cancelled by destroy"));
    return;
  }
  const waitedMs = Date.now() - pollStart;
  logInfo(
    reading.apps.length > 0
      ? `adoption: running app appeared after ${waitedMs}ms [${reading.diagnostics}]`
      : `adoption: no running app after ${waitedMs}ms [${reading.diagnostics}]`,
  );

  const { adopted, restamped, orphans } = planAdoption(
    crumbs,
    reading.apps.map((app) => app.appid),
    new Set(activeSessions.keys()),
    getRomIdForApp,
    Date.now(),
  );

  const recovered = [...adopted, ...restamped];
  for (const session of recovered) activeSessions.set(session.appId, session);
  // One rewrite for the whole reconcile: it drops the orphans and lifts a row
  // written by an older schema version into the current one.
  persistSessions();

  for (const session of recovered) dispatchSessionChanged(true, session.appId, session.romId);
  for (const s of adopted) logInfo(`Adopted running session from breadcrumb: romId=${s.romId}, appId=${s.appId}`);
  for (const s of orphans) logInfo(`Session orphaned — playtime not recorded (romId=${s.romId})`);

  for (const session of restamped) {
    if (epoch !== sessionEpoch) {
      // Each re-stamp is an await, so the teardown check is per iteration.
      detach(debugLog("adoption: cancelled by destroy"));
      return;
    }
    try {
      await recordSessionStart(session.romId);
    } catch (e) {
      logError(`Failed to record session start on adoption: ${e}`);
    }
    logInfo(
      `Adopted running session without breadcrumb, re-stamped marker: romId=${session.romId}, appId=${session.appId}`,
    );
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
    .then(() => adoptOrphanedSessions())
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
