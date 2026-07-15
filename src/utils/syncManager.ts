import { addEventListener } from "@decky/api";
import type { SyncAddItem, SyncApplyUnitData } from "../types";
import {
  reconcileShortcuts,
  reportUnitResults,
  syncHeartbeat,
  getArtworkBase64,
  logInfo,
  logError,
} from "../api/backend";
import {
  getExistingRomMShortcuts,
  getLiveRomMShortcutAppIds,
  addShortcut,
  setLaunchOptionsConfirmed,
} from "./steamShortcuts";
import { updateSyncProgress } from "./syncProgress";
import { recordSyncCreated } from "./syncDeltaStore";
import { observeUnitTotal } from "./syncEta";
import { registerRomMAppId } from "../patches/gameDetailPatch";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const HEARTBEAT_INTERVAL_MS = 10_000;

let _cancelRequested = false;
let _isUnitRunning = false;

/**
 * Once-per-run cache of the existing-shortcut scan. The backend emits one
 * ``sync_apply_unit`` event per unit but the scan only needs to run once per
 * run: every pre-existing RomM shortcut is captured at the first unit, and the
 * backend deduplicates rom_ids so no rom_id is emitted by more than one unit in
 * a run. Keyed by ``run_id`` — a new run mints a new id, so the cache
 * self-resets on a fresh run (miss → fresh scan).
 *
 * ``liveAppIds`` is the raw exe-owned appId list from that same sweep (the input
 * the bound ``map`` was derived from). Caching it lets the orphan-adoption pool
 * reuse the one scan instead of running a second ``RegisterForAppDetails`` sweep
 * (#1366); ``null`` when the store was unreadable.
 */
let _scanCache: { runId: string; map: Map<number, number>; liveAppIds: number[] | null } | null = null;

/**
 * Once-per-run pool of adoptable ORPHAN shortcuts, built LAZILY on the first
 * create-path candidate of a run. An orphan is a live RomM-owned shortcut (exe
 * ends ``/bin/rom-launcher``) that carries NO DB binding — a crashed run's
 * uncommitted in-flight shortcut, or a zombie left after a DB reset. Without
 * adoption the create path mints a fresh ``AddShortcut`` for a ROM the orphan
 * already represents, leaving a visible duplicate (#1366). Keyed by Steam
 * display name → ascending appIds, so a same-name collision adopts the lowest
 * appId first and removes it (never twice). Keyed by ``run_id`` so a new run
 * mints a fresh pool; built only when a run actually reaches the create path
 * (most runs are pure updates and pay zero extra scan cost).
 */
let _orphanPoolCache: { runId: string; pool: Map<string, number[]> } | null = null;

/** Request cancellation of the frontend shortcut processing loop. */
export function requestSyncCancel(): void {
  _cancelRequested = true;
}

/**
 * Clear the per-run cancel flag at the start of a run.
 *
 * Two callers:
 *
 * - The ``sync_plan`` listener — sync_plan fires once per run before any unit,
 *   so this is the per-run reset the per-unit handler can't be relied on for (a
 *   skip-only run never runs that handler and would otherwise carry a stale
 *   ``_cancelRequested`` from a prior cancelled run — the #1198 H4 defect).
 * - The sync trigger (``handleSync`` / ``handleApply``) at the TOP, so a fresh
 *   sync never starts pre-cancelled by a flag left set from a prior run.
 *
 * Run identity is no longer mirrored frontend-side: a Cancel click reads the
 * backend-fed run id from the ``sync_progress`` store (#1202).
 */
export function resetSyncCancel(): void {
  _cancelRequested = false;
}

/**
 * Read the cancel flag. Accessed through a function so a read after the
 * per-unit ``_cancelRequested = false`` reset isn't narrowed to a constant
 * ``false`` by control-flow analysis — the flag is flipped externally by
 * {@link requestSyncCancel} during the awaited work, which TS can't see.
 * Exported so tests can observe the per-run reset on the skip-only path
 * (``sync_plan`` with no following ``sync_apply_unit``).
 */
export function isCancelRequested(): boolean {
  return _cancelRequested;
}

/**
 * Build the run's adoptable-orphan pool from the once-per-run live scan: every
 * live RomM-owned appId that is NOT already bound to a rom_id this run, indexed
 * by its Steam display name.
 *
 * *liveAppIds* is the cached result of the run's single
 * ``getLiveRomMShortcutAppIds()`` sweep (owned by {@link resolveExistingShortcuts}),
 * so this reuses that scan rather than running a second one. A ``null`` scan
 * (store unreadable) — or an unavailable ``appStore`` — yields an EMPTY pool:
 * adoption is disabled for the run rather than guessed against a store we
 * couldn't read. An orphan whose overview resolves to no display name is skipped
 * (it can't be matched by name).
 */
function buildAdoptableOrphanPool(liveAppIds: number[] | null, boundMap: Map<number, number>): Map<string, number[]> {
  const pool = new Map<string, number[]>();
  // null = store unreadable (do NOT adopt); [] = scan ran, no RomM shortcuts.
  if (liveAppIds === null) {
    logInfo("orphan adoption: live shortcut store unreadable; adoption disabled for this run");
    return pool;
  }
  if (typeof appStore === "undefined") {
    logInfo("orphan adoption: appStore unavailable; adoption disabled for this run");
    return pool;
  }

  const boundAppIds = new Set(boundMap.values());
  let orphanCount = 0;
  for (const appId of liveAppIds) {
    if (boundAppIds.has(appId)) continue; // already managed via a rom_id binding
    const overview = appStore.GetAppOverviewByAppID(appId);
    const name = overview?.strDisplayName || overview?.display_name;
    if (!name) continue; // no resolvable name → can't match by name, skip
    const bucket = pool.get(name);
    if (bucket) bucket.push(appId);
    else pool.set(name, [appId]);
    orphanCount++;
  }
  // Ascending order per name so a same-name collision adopts deterministically
  // (lowest appId first) and each orphan is taken at most once.
  for (const bucket of pool.values()) bucket.sort((a, b) => a - b);
  logInfo(`orphan adoption: pool holds ${orphanCount} adoptable orphan(s) across ${pool.size} name(s)`);
  return pool;
}

/**
 * Return the run's adoptable-orphan pool, building it at most once per run from
 * the already-scanned *liveAppIds*. Only ever called from the create path, so a
 * pure-update run never builds it (the appStore name-resolution pass is skipped
 * entirely). A new ``run_id`` is a cache miss → fresh pool.
 */
function resolveAdoptableOrphanPool(
  runId: string,
  liveAppIds: number[] | null,
  boundMap: Map<number, number>,
): Map<string, number[]> {
  if (_orphanPoolCache?.runId === runId) return _orphanPoolCache.pool;
  const pool = buildAdoptableOrphanPool(liveAppIds, boundMap);
  _orphanPoolCache = { runId, pool };
  return pool;
}

/**
 * Remove and return the lowest adoptable orphan appId for *name*, or
 * ``undefined`` when the pool holds none. Removing it ensures one orphan is
 * never adopted twice within a run.
 */
function takeAdoptableOrphan(pool: Map<string, number[]>, name: string): number | undefined {
  const bucket = pool.get(name);
  if (!bucket || bucket.length === 0) return undefined;
  return bucket.shift();
}

/**
 * Rewrite an existing shortcut's identity + launch bake in place: name, exe,
 * start dir, then the confirmed launch-options write. Shared by the update path
 * (rom already bound) and the adoption path (reusing an orphan's appId) so the
 * two can't drift. Launch options carry the full RetroDECK command (or ``""``
 * for uninstalled); the confirm-poll verifies the write landed rather than
 * fire-and-forget, since ``Set*`` returns void.
 */
async function rewriteShortcutIdentity(appId: number, item: SyncAddItem): Promise<void> {
  SteamClient.Apps.SetShortcutName(appId, item.name);
  SteamClient.Apps.SetShortcutExe(appId, item.exe);
  SteamClient.Apps.SetShortcutStartDir(appId, item.start_dir);
  await setLaunchOptionsConfirmed(appId, item.launch_options);
}

/**
 * Resolve a shortcut item to an appId and report whether it is a newly-managed
 * shortcut (``created``).
 *
 * - An **existing** binding (rom_id already in *existing*) → update in place,
 *   ``created: false``.
 * - No binding → the create path. First try to **adopt** a live RomM-owned
 *   orphan of the same name — reuse its appId + rewrite its identity/launch
 *   bake, instead of ``addShortcut`` minting a duplicate the user then sees
 *   twice (#1366). Failing that, mint a fresh shortcut.
 *
 * *liveAppIds* is the run's cached live-shortcut scan, threaded in so the orphan
 * pool reuses it (no second scan). ``appId`` is ``undefined`` when a create
 * failed. ``created`` is ``true`` for both a fresh create and an adoption (each
 * brings a game under management for the first time), and drives two caller
 * behaviours: applying cover artwork (creates + adoptions get it, updates keep
 * their grid file) and — via ``recordSyncCreated`` here — the user-facing
 * "added" delta. It is deliberately NOT a proxy for an ``AddShortcut`` call: an
 * adoption sets ``created`` without one, because the renderer-cost of minting a
 * Steam shortcut never happens.
 */
async function resolveShortcutAppId(
  item: SyncAddItem,
  existing: Map<number, number>,
  runId: string,
  liveAppIds: number[] | null,
): Promise<{ appId: number | undefined; created: boolean }> {
  const existingAppId = existing.get(item.rom_id);
  if (existingAppId) {
    await rewriteShortcutIdentity(existingAppId, item);
    return { appId: existingAppId, created: false };
  }
  // Create path. Before minting a fresh shortcut, try to ADOPT a live RomM-owned
  // orphan of the same name (a shortcut whose exe is ours but which carries no
  // DB binding). Adopting reuses its appId — the shortcut, its artwork, and its
  // collections survive — instead of leaving a visible duplicate (#1366).
  const orphanPool = resolveAdoptableOrphanPool(runId, liveAppIds, existing);
  const adoptedAppId = takeAdoptableOrphan(orphanPool, item.name);
  if (adoptedAppId !== undefined) {
    // Same identity + launch writes as the update path — the orphan already exists.
    await rewriteShortcutIdentity(adoptedAppId, item);
    // A game came under management → count it in the user-facing "added" delta,
    // exactly like a fresh create (no AddShortcut happened, but getSyncDelta
    // feeds only the terminal toast, never renderer-cost accounting).
    recordSyncCreated(adoptedAppId);
    logInfo(`adopted orphan shortcut ${adoptedAppId} for rom ${item.rom_id} (${item.name})`);
    return { appId: adoptedAppId, created: true };
  }
  // No orphan to adopt → mint a fresh shortcut. Record its appId as a real
  // "added" delta — the update path above is excluded (the shortcut existed).
  const createdAppId = (await addShortcut(item)) ?? undefined;
  if (createdAppId) recordSyncCreated(createdAppId);
  return { appId: createdAppId, created: createdAppId !== undefined };
}

/**
 * Fetch a shortcut's cover and push it through Steam's native artwork API so
 * the tile shows the real cover in-session, with no client restart.
 * ``SetCustomArtworkForApp`` writes ``{app_id}p.png`` — the same grid file the
 * backend also writes, which stays as the durability net if this call fails.
 * ``rom_id`` is the emitted entry's own id, which the backend always resolves
 * to the representative's cover through the in-flight pending-sync path (or the
 * persisted cache for a #1386 refresh entry), so no separate bind key is
 * needed. Fail-soft: a cover that can't be fetched (``base64: null``) or
 * applied never fails the shortcut — the backend grid copy still lands. One
 * cover per item, awaited inside the 50ms-paced loop: batching covers resident
 * is exactly the #797 CEF-heap overflow the session-budget gate protects
 * against. Two callers: newly created shortcuts, and the backend's #1386
 * cover-refresh list for existing shortcuts whose server cover changed.
 */
async function applyCoverArtwork(appId: number, romId: number): Promise<void> {
  try {
    const { base64 } = await getArtworkBase64(romId);
    if (base64) {
      await SteamClient.Apps.SetCustomArtworkForApp(appId, base64, "png", 0);
    }
  } catch (e) {
    logError(`Per-unit: failed to apply cover for rom ${romId} (appId ${appId}): ${e}`);
  }
}

/**
 * Re-apply the covers of EXISTING shortcuts whose server-side cover changed
 * (#1386). The backend's cover-cache invalidation pass already re-downloaded
 * the per-ROM cache file and republished the grid copy — this loop pushes each
 * fresh cover through Steam's artwork API so the tile refreshes in-session
 * (``rt_custom_image_mtime`` only bumps through ``SetCustomArtworkForApp`` or a
 * client restart). The list rides the unit's first chunk and is already
 * budget-clipped backend-side (a cover ≈ 1 MB of transient renderer heap);
 * processed at the same CEF-safe 50ms cadence as created-shortcut covers, with
 * heartbeats so a long refresh tail never trips the backend's per-unit
 * timeout. Runs BEFORE the chunk ack so the backend's wait covers this work.
 * Fail-soft per item; exits early on cancel.
 */
async function processCoverRefreshes(refreshes: { rom_id: number; app_id: number }[]): Promise<void> {
  let lastHeartbeat = Date.now();
  for (const entry of refreshes) {
    if (isCancelRequested()) {
      logInfo("Per-unit cancel observed during cover refresh");
      break;
    }
    await applyCoverArtwork(entry.app_id, entry.rom_id);
    await delay(50);
    if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
      syncHeartbeat().catch(() => {});
      lastHeartbeat = Date.now();
    }
  }
}

/**
 * Process every shortcut for one apply chunk at the CEF-safe 50ms cadence,
 * recording the rom_id→appId mapping. Progress is unit-wide — ``chunk_offset``
 * carries the count from prior chunks so the QAM bar advances continuously across
 * a unit's chunks against ``unit_total``. Heartbeats are emitted every 10s. The
 * loop exits early on cancel.
 *
 * A newly-managed shortcut — a fresh create OR an adopted orphan (#1366) — has
 * its cover applied through Steam's artwork API in the same iteration (see
 * {@link applyCoverArtwork}) so covers appear as the shortcuts land — one per
 * item under the 50ms pacing, safe under the session-budget gate that brakes a
 * large run before the CEF heap overflows. Updates/rebinds keep their existing
 * grid file here; an update whose SERVER cover changed instead arrives on the
 * chunk's ``cover_refreshes`` list and is re-applied by
 * {@link processCoverRefreshes} (#1386). A cover failure is fail-soft and never
 * fails the item/chunk.
 */
async function processUnitShortcuts(
  data: SyncApplyUnitData,
  existing: Map<number, number>,
  liveAppIds: number[] | null,
  romIdToAppId: Record<string, number>,
): Promise<void> {
  let lastHeartbeat = Date.now();
  for (const [i, item] of data.shortcuts.entries()) {
    const unitCurrent = data.chunk_offset + i + 1;
    try {
      // Carry the chunk-constant fields on every per-item update, not just the
      // fine current/message. The chunk-init emit (initUnitSyncManager) seeds
      // these once, but a QAM remount mid-chunk can replace the module store
      // with the backend's coarse snapshot; re-asserting the full field set here
      // lets the store self-heal on the next item (≤0.55s), so the fine line +
      // step counter reappear without waiting for the next chunk boundary.
      updateSyncProgress({
        running: true,
        stage: "applying",
        current: unitCurrent,
        total: data.unit_total,
        message: `${data.unit_name}: ${unitCurrent}/${data.unit_total}`,
        step: data.unit_index + 1,
        totalSteps: data.total_units,
      });
      const { appId, created } = await resolveShortcutAppId(item, existing, data.run_id, liveAppIds);
      if (appId) {
        romIdToAppId[String(item.rom_id)] = appId;
        // Register the appId as RomM-owned the moment the mapping exists — the
        // earliest point the game-detail patch and launch interceptor can gate
        // on it. Registering only at sync_complete leaves a newly created
        // shortcut's detail page rendering native Steam UI for the whole run's
        // artwork/collection tail, and a collection-only sync's appIds may never
        // reach platform_app_ids at all (#1205). Idempotent (Set.add); covers
        // created, updated, and rebind entries alike.
        registerRomMAppId(appId);
        // Apply cover artwork to newly-managed shortcuts — a fresh create or an
        // adopted orphan (#1366; ``created`` covers both). An updated/rebound
        // shortcut keeps its existing grid file (cover refresh on change is
        // #1386). Awaited so covers stay one-per-item under the 50ms pacing;
        // fail-soft.
        if (created) await applyCoverArtwork(appId, item.rom_id);
      }
    } catch (e) {
      logError(`Per-unit: failed to process shortcut for rom ${item.rom_id}: ${e}`);
    }
    await delay(50);

    if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
      syncHeartbeat().catch(() => {});
      lastHeartbeat = Date.now();
    }
    if (isCancelRequested()) {
      logInfo(`Per-unit cancel observed during ${data.unit_name}`);
      break;
    }
  }
}

/**
 * Return the existing RomM-shortcut map for this run plus the raw live-scan list
 * it was derived from, scanning Steam at most once per run. One
 * ``RegisterForAppDetails`` sweep (`getLiveRomMShortcutAppIds`) produces both the
 * bound ``map`` and the ``liveAppIds`` the orphan pool reuses (#1366), so the
 * expensive scan never runs twice in a create-bearing run. On a cache hit
 * (``run_id`` matches) the stored pair is reused; on a miss the scan runs, the
 * result is cached, and one ``logInfo`` records how long it took.
 */
async function resolveExistingShortcuts(
  runId: string,
): Promise<{ map: Map<number, number>; liveAppIds: number[] | null }> {
  if (_scanCache?.runId === runId) return { map: _scanCache.map, liveAppIds: _scanCache.liveAppIds };
  const start = Date.now();
  const liveAppIds = (await getLiveRomMShortcutAppIds()) ?? null;
  const map = await getExistingRomMShortcuts(liveAppIds);
  _scanCache = { runId, map, liveAppIds };
  logInfo(`getExistingRomMShortcuts: scanned ${map.size} RomM shortcuts in ${Date.now() - start}ms (run ${runId})`);
  return { map, liveAppIds };
}

/**
 * Sync-start reconcile of stale shortcut bindings (#1046).
 *
 * Reads Steam's live RomM-shortcut appIds and asks the backend to unbind any
 * binding absent from that set — a shortcut the user deleted via Steam's own UI
 * leaves a dead ``roms.shortcut_app_id``, which the incremental skip otherwise
 * counts as "unchanged" forever, so the shortcut never comes back. Unbinding
 * before the work queue is built lets the next sync's incremental skip re-fetch
 * the platform and recreate the missing shortcut.
 *
 * Best-effort: only reconciles when the live scan actually ran (a `null` scan —
 * Steam's store unreadable — is skipped, never reconciled, so a transient store
 * failure can't unbind every binding). Any error is logged and swallowed so a
 * reconcile failure never blocks the sync itself.
 */
export async function reconcileStaleShortcuts(): Promise<void> {
  let liveAppIds: number[] | null;
  try {
    liveAppIds = await getLiveRomMShortcutAppIds();
  } catch (e) {
    logError(`reconcileStaleShortcuts: failed to scan live shortcuts: ${e}`);
    return;
  }
  // null = Steam's shortcut store was unreadable; do NOT reconcile (would unbind
  // every binding). [] = scan ran, found none — a real signal the backend acts on.
  if (liveAppIds === null) return;
  try {
    const result = await reconcileShortcuts(liveAppIds);
    if (result.unbound_count) {
      logInfo(`reconcileStaleShortcuts: backend unbound ${result.unbound_count} stale shortcut(s)`);
    }
  } catch (e) {
    logError(`reconcileStaleShortcuts: backend reconcile failed: ${e}`);
  }
}

/**
 * Initialize the per-unit pipeline handler. Listens for ``sync_apply_unit``
 * events, processes each unit's shortcuts at the CEF-safe 50ms cadence — applying
 * each newly created shortcut's cover through Steam's artwork API as it goes (see
 * {@link processUnitShortcuts}), then re-applying the chunk's ``cover_refreshes``
 * for existing shortcuts whose server cover changed (#1386, see
 * {@link processCoverRefreshes}) — and reports back via ``reportUnitResults`` so
 * the backend can advance the work queue and durably commit the chunk.
 */
export function initUnitSyncManager(): ReturnType<typeof addEventListener> {
  return addEventListener("sync_apply_unit", async (data: SyncApplyUnitData) => {
    if (_isUnitRunning) {
      logInfo(`sync_apply_unit: already processing a unit, dropping duplicate for ${data.unit_name}`);
      return;
    }
    _isUnitRunning = true;
    try {
      if (!Array.isArray(data.shortcuts)) {
        logError("sync_apply_unit: data.shortcuts is not an array, aborting");
        return;
      }

      _cancelRequested = false;
      const romIdToAppId: Record<string, number> = {};

      // Correct this unit's live-countdown weight to its real delta size
      // (unit_total is now the new+changed count, not the raw rom_count the plan
      // seeded), so a mostly-unchanged trailing unit stops over-weighting the
      // "~N min left" readout (#1383 / #1382-M3). Idempotent across the unit's
      // chunks — every chunk carries the same unit_total.
      observeUnitTotal(data.unit_index, data.unit_total);

      const total = data.shortcuts.length;
      logInfo(
        `sync_apply_unit received: ${data.unit_type}=${data.unit_name} (${data.unit_index + 1}/${data.total_units}), ` +
          `chunk ${data.chunk_index + 1}/${data.chunk_count}, ${total} shortcuts`,
      );

      // Progress is unit-wide: seed from ``chunk_offset`` against ``unit_total``
      // so the bar reads e.g. "PSX: 1200/3084" continuously across a unit's
      // chunks rather than restarting at 0 each chunk.
      updateSyncProgress({
        running: true,
        stage: "applying",
        current: data.chunk_offset,
        total: data.unit_total,
        message: `${data.unit_name}: ${data.chunk_offset}/${data.unit_total}`,
        step: data.unit_index + 1,
        totalSteps: data.total_units,
      });

      const { map: existing, liveAppIds } = await resolveExistingShortcuts(data.run_id);
      await processUnitShortcuts(data, existing, liveAppIds, romIdToAppId);

      // Cover refreshes for EXISTING shortcuts whose server cover changed
      // (#1386) — budget-clipped backend-side, non-empty only on chunk 0.
      // Processed before the ack so the backend's per-unit wait (heartbeat-fed)
      // covers this work; a cancel observed above skips it entirely.
      if (!isCancelRequested() && data.cover_refreshes?.length) {
        await processCoverRefreshes(data.cover_refreshes);
      }

      // Do NOT ack a cancelled unit: the backend has already discarded this
      // run's in-flight state, so a post-cancel ack only risks being credited
      // to whatever run started next (the cross-run collision + rapid-restart
      // self-cancel in #1041). The backend also validates run_id/unit_id, but
      // not sending is the first line of defence.
      if (isCancelRequested()) {
        logInfo(`Per-unit cancel observed for ${data.unit_name}; skipping reportUnitResults`);
      } else {
        try {
          // Echo back the run + unit + chunk identity so the backend can reject
          // a stale ack (cancelled run or superseded chunk) instead of crediting
          // it to a fresh run/chunk (#1041).
          await reportUnitResults(romIdToAppId, data.run_id, data.unit_id, data.chunk_index);
        } catch (e) {
          logError(`Failed to report unit results for ${data.unit_name}: ${e}`);
        }
      }
      logInfo(
        `sync_apply_unit complete: ${data.unit_name} chunk ${data.chunk_index + 1}/${data.chunk_count} ` +
          `(${Object.keys(romIdToAppId).length}/${total})`,
      );
    } finally {
      _isUnitRunning = false;
    }
  });
}
