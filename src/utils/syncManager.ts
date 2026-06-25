import { addEventListener } from "@decky/api";
import type { SyncAddItem, SyncApplyUnitData } from "../types";
import {
  getArtworkBase64,
  reconcileShortcuts,
  reportUnitResults,
  syncHeartbeat,
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

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const HEARTBEAT_INTERVAL_MS = 10_000;
const ART_CONCURRENCY = 8;

interface ArtworkTarget {
  appId: number;
  romId: number;
  name: string;
}

let _cancelRequested = false;
let _isUnitRunning = false;

/**
 * Run id of the sync currently in flight, captured from the backend
 * ``sync_plan`` / ``sync_apply_unit`` events. Passed back on cancel so a
 * Cancel click is scoped to the active run — a click meant for run N can land
 * after run N finalized and run N+1 started, and an unscoped cancel would
 * wrongly abort run N+1 (#1198). ``null`` before any run has started; the
 * backend then cancels unconditionally (the safety case).
 */
let _activeRunId: string | null = null;

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
 * Set the captured run id and clear the cancel flag, marking a fresh run.
 *
 * Two callers, two roles:
 *
 * - The ``sync_plan`` listener passes the real ``run_id`` — sync_plan fires
 *   once per run before any unit, so this is the per-run cancel-flag reset that
 *   the per-unit handler can't be relied on for (a skip-only run never runs
 *   that handler and would otherwise carry a stale ``_cancelRequested`` from a
 *   prior cancelled run — the #1198 adjacent H4 defect).
 * - The sync trigger (``handleSync`` / ``handleApply``) passes ``""`` at the
 *   TOP, before the backend mints the new run's id. That clears the *previous*
 *   run's captured id (``"" → null``) so a Cancel in the "Fetching library…"
 *   window (reconcile + build_work_queue paginate for seconds, before
 *   sync_plan) sends ``""`` → the backend's unconditional-cancel path, not a
 *   stale id the backend would reject as a cross-run mismatch and drop (#1198).
 *   It can't reintroduce the cross-run race: an *already-fired* stale cancel
 *   carried its own run id as a bound argument, unaffected by this reset.
 */
export function beginSyncRun(runId: string): void {
  _activeRunId = runId || null;
  _cancelRequested = false;
}

/**
 * Run id of the sync currently in flight, or ``null`` before any run has
 * started. Passed to ``cancelSync`` so the backend scopes the cancel to the
 * active run (#1198).
 */
export function getActiveRunId(): string | null {
  return _activeRunId;
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
 * Resolve a shortcut item to an appId: update fields on the existing
 * shortcut when one is present, otherwise create a new shortcut. Returns
 * ``undefined`` if no appId could be resolved (creation failed).
 */
async function resolveShortcutAppId(item: SyncAddItem, existing: Map<number, number>): Promise<number | undefined> {
  const existingAppId = existing.get(item.rom_id);
  if (existingAppId) {
    SteamClient.Apps.SetShortcutName(existingAppId, item.name);
    SteamClient.Apps.SetShortcutExe(existingAppId, item.exe);
    SteamClient.Apps.SetShortcutStartDir(existingAppId, item.start_dir);
    // Launch options carry the full RetroDECK command (or "" for uninstalled).
    // Confirm the write landed rather than fire-and-forget — Set* returns void.
    await setLaunchOptionsConfirmed(existingAppId, item.launch_options);
    return existingAppId;
  }
  // Create path: a fresh shortcut. Record its appId as a real "added" delta —
  // the update path above is excluded (the shortcut already existed).
  const createdAppId = (await addShortcut(item)) ?? undefined;
  if (createdAppId) recordSyncCreated(createdAppId);
  return createdAppId;
}

/**
 * Process every shortcut for one unit at the CEF-safe 50ms cadence,
 * recording the rom_id→appId mapping and the artwork targets for the
 * follow-up artwork phase. Heartbeats are emitted every 10s. The loop
 * exits early on cancel.
 */
async function processUnitShortcuts(
  data: SyncApplyUnitData,
  existing: Map<number, number>,
  romIdToAppId: Record<string, number>,
  artworkTargets: ArtworkTarget[],
  total: number,
): Promise<void> {
  let lastHeartbeat = Date.now();
  for (const [i, item] of data.shortcuts.entries()) {
    try {
      updateSyncProgress({
        current: i + 1,
        message: `${data.unit_name}: ${i + 1}/${total}`,
      });
      const appId = await resolveShortcutAppId(item, existing);
      if (appId) {
        romIdToAppId[String(item.rom_id)] = appId;
        artworkTargets.push({ appId, romId: item.rom_id, name: item.name });
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
 * Fetch and apply cover artwork for a single target. Swallows per-target
 * errors so one failure doesn't take down the batch.
 */
async function applyArtworkForTarget({ appId, romId, name }: ArtworkTarget): Promise<void> {
  try {
    const artResult = await getArtworkBase64(romId);
    if (artResult.base64) {
      await SteamClient.Apps.SetCustomArtworkForApp(appId, artResult.base64, "png", 0);
    }
  } catch (artErr) {
    logError(`Per-unit: failed to fetch/set artwork for ${name}: ${artErr}`);
  }
}

/**
 * Fetch artwork for every target in batches of ``ART_CONCURRENCY``, with
 * heartbeats between batches. Exits early on cancel.
 */
async function processUnitArtwork(artworkTargets: ArtworkTarget[]): Promise<void> {
  if (artworkTargets.length === 0) return;
  let lastHeartbeat = Date.now();
  for (let i = 0; i < artworkTargets.length; i += ART_CONCURRENCY) {
    if (isCancelRequested()) break;
    const batch = artworkTargets.slice(i, i + ART_CONCURRENCY);
    await Promise.all(batch.map(applyArtworkForTarget));
    if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
      syncHeartbeat().catch(() => {});
      lastHeartbeat = Date.now();
    }
  }
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
 * reports back via ``reportUnitResults`` so the backend can advance the
 * work queue. Artwork still goes through the existing base64 round-trip —
 * the artwork-rename optimisation is deferred until hardware verification.
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
      // Capture the run id here too — ``sync_apply_unit`` always carries it, so
      // a Cancel click resolves the right run even if the ``sync_plan`` capture
      // was missed (#1198).
      _activeRunId = data.run_id || _activeRunId;
      const romIdToAppId: Record<string, number> = {};
      const artworkTargets: ArtworkTarget[] = [];

      const total = data.shortcuts.length;
      logInfo(
        `sync_apply_unit received: ${data.unit_type}=${data.unit_name} (${data.unit_index + 1}/${data.total_units}), ${total} shortcuts`,
      );

      updateSyncProgress({
        running: true,
        stage: "applying",
        current: 0,
        total,
        message: `${data.unit_name}: 0/${total}`,
        step: data.unit_index + 1,
        totalSteps: data.total_units,
      });

      const existing = await resolveExistingShortcuts(data.run_id);
      await processUnitShortcuts(data, existing, romIdToAppId, artworkTargets, total);

      // Artwork — keep existing base64 path; per-unit-sized batch keeps the
      // payload comfortably under the decky.emit WebSocket size ceiling.
      await processUnitArtwork(artworkTargets);

      // Do NOT ack a cancelled unit: the backend has already discarded this
      // run's in-flight state, so a post-cancel ack only risks being credited
      // to whatever run started next (the cross-run collision + rapid-restart
      // self-cancel in #1041). The backend also validates run_id/unit_id, but
      // not sending is the first line of defence.
      if (isCancelRequested()) {
        logInfo(`Per-unit cancel observed for ${data.unit_name}; skipping reportUnitResults`);
      } else {
        try {
          // Echo back the run + unit identity so the backend can reject a stale
          // ack (cancelled run) instead of crediting it to a fresh run (#1041).
          await reportUnitResults(romIdToAppId, data.run_id, data.unit_id);
        } catch (e) {
          logError(`Failed to report unit results for ${data.unit_name}: ${e}`);
        }
      }
      logInfo(`sync_apply_unit complete: ${data.unit_name} (${Object.keys(romIdToAppId).length}/${total})`);
    } finally {
      _isUnitRunning = false;
    }
  });
}
