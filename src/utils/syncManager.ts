import { addEventListener } from "@decky/api";
import type { SyncAddItem, SyncApplyUnitData } from "../types";
import { reconcileShortcuts, reportUnitResults, syncHeartbeat, logInfo, logError } from "../api/backend";
import {
  getExistingRomMShortcuts,
  getLiveRomMShortcutAppIds,
  addShortcut,
  setLaunchOptionsConfirmed,
} from "./steamShortcuts";
import { updateSyncProgress } from "./syncProgress";
import { recordSyncCreated, recordSyncAcked } from "./syncDeltaStore";
import { stampCoverMtimes } from "./coverMtime";
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
 */
let _scanCache: { runId: string; map: Map<number, number> } | null = null;

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
 * Resolve a shortcut item to an appId and report whether it was newly created:
 * update fields on the existing shortcut when one is present (``created: false``),
 * otherwise create a new shortcut (``created: true`` iff the create produced an
 * appId). ``appId`` is ``undefined`` when creation failed. The ``created`` flag
 * lets the caller stamp only fresh shortcuts' cover mtime, not updates/rebinds.
 */
async function resolveShortcutAppId(
  item: SyncAddItem,
  existing: Map<number, number>,
): Promise<{ appId: number | undefined; created: boolean }> {
  const existingAppId = existing.get(item.rom_id);
  if (existingAppId) {
    SteamClient.Apps.SetShortcutName(existingAppId, item.name);
    SteamClient.Apps.SetShortcutExe(existingAppId, item.exe);
    SteamClient.Apps.SetShortcutStartDir(existingAppId, item.start_dir);
    // Launch options carry the full RetroDECK command (or "" for uninstalled).
    // Confirm the write landed rather than fire-and-forget — Set* returns void.
    await setLaunchOptionsConfirmed(existingAppId, item.launch_options);
    return { appId: existingAppId, created: false };
  }
  // Create path: a fresh shortcut. Record its appId as a real "added" delta —
  // the update path above is excluded (the shortcut already existed).
  const createdAppId = (await addShortcut(item)) ?? undefined;
  if (createdAppId) recordSyncCreated(createdAppId);
  return { appId: createdAppId, created: createdAppId !== undefined };
}

/**
 * Process every shortcut for one apply chunk at the CEF-safe 50ms cadence,
 * recording the rom_id→appId mapping and returning the appIds this chunk NEWLY
 * created (not updates/rebinds) so the handler can stamp their cover mtime once
 * the ack commits. Progress is unit-wide — ``chunk_offset`` carries the count
 * from prior chunks so the QAM bar advances continuously across a unit's chunks
 * against ``unit_total``. Heartbeats are emitted every 10s. The loop exits early
 * on cancel.
 *
 * Cover artwork is NOT pushed here: the backend already writes each cover to the
 * Steam grid dir as ``{app_id}p.png`` at commit (reporter._finalize_cover_path),
 * and Steam loads that file lazily for visible entries only. Pushing every cover
 * through ``SetCustomArtworkForApp`` during sync forced Steam to decode and cache
 * all covers resident at once, overflowing the CEF heap on large libraries (the
 * #797 steamwebhelper SIGTRAP). The lightweight mtime stamp
 * ({@link stampCoverMtimes}) only cache-busts the tile URL — no decode, no heap.
 */
async function processUnitShortcuts(
  data: SyncApplyUnitData,
  existing: Map<number, number>,
  romIdToAppId: Record<string, number>,
): Promise<number[]> {
  const createdAppIds: number[] = [];
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
      const { appId, created } = await resolveShortcutAppId(item, existing);
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
        // Collect only NEWLY CREATED appIds for the post-ack cover stamp — an
        // updated/rebound shortcut already resolved its cover before this run.
        if (created) createdAppIds.push(appId);
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
  return createdAppIds;
}

/**
 * Return the existing RomM-shortcut map for this run, scanning Steam at most
 * once per run. On a cache hit (``run_id`` matches the cached run) the stored
 * map is reused; on a miss the scan runs, the result is cached, and one
 * ``logInfo`` records how long the scan took so operators can confirm it ran
 * exactly once per run.
 */
async function resolveExistingShortcuts(runId: string): Promise<Map<number, number>> {
  if (_scanCache?.runId === runId) return _scanCache.map;
  const start = Date.now();
  const map = await getExistingRomMShortcuts();
  _scanCache = { runId, map };
  logInfo(`getExistingRomMShortcuts: scanned ${map.size} RomM shortcuts in ${Date.now() - start}ms (run ${runId})`);
  return map;
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
 * events, processes each unit's shortcuts at the CEF-safe 50ms cadence, and
 * reports back via ``reportUnitResults`` so the backend can advance the work
 * queue. After each chunk's ack commits, it stamps the cover mtime of the
 * shortcuts this chunk created (see {@link stampCoverMtimes}) so covers appear
 * progressively during the run; the onSyncComplete sweep is the belt-and-braces
 * net for rebinds and any chunk the per-chunk stamp missed.
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

      const existing = await resolveExistingShortcuts(data.run_id);
      const createdAppIds = await processUnitShortcuts(data, existing, romIdToAppId);

      // Do NOT ack a cancelled unit: the backend has already discarded this
      // run's in-flight state, so a post-cancel ack only risks being credited
      // to whatever run started next (the cross-run collision + rapid-restart
      // self-cancel in #1041). The backend also validates run_id/unit_id, but
      // not sending is the first line of defence. (No cover stamp on this path
      // either — nothing was committed.)
      if (isCancelRequested()) {
        logInfo(`Per-unit cancel observed for ${data.unit_name}; skipping reportUnitResults`);
      } else {
        try {
          // Echo back the run + unit + chunk identity so the backend can reject
          // a stale ack (cancelled run or superseded chunk) instead of crediting
          // it to a fresh run/chunk (#1041).
          await reportUnitResults(romIdToAppId, data.run_id, data.unit_id, data.chunk_index);
          // Ack resolved → the backend committed the chunk AND wrote its grid
          // covers, so the tile URLs now resolve. Stamp the created shortcuts'
          // cover mtime so their covers appear progressively during the run (a
          // mid-run pause/interrupt then leaves everything committed-so-far
          // visible), not only at sync_complete. Skipped if the ack rejects (the
          // catch below) — no commit, so stamping would risk the 404 negative-cache.
          // Record this acked chunk's creates as safe-to-stamp (committed + grid
          // files written) so onSyncComplete's sweep/heal skip a cancelled run's
          // uncommitted in-flight chunk (#M1). Fire-and-forget stamp (micro-batched
          // inside): must NOT delay the next chunk.
          recordSyncAcked(createdAppIds);
          void stampCoverMtimes(createdAppIds, " (chunk)");
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
