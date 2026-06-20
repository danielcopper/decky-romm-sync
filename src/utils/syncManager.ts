import { addEventListener } from "@decky/api";
import type { SyncAddItem, SyncApplyUnitData } from "../types";
import { getArtworkBase64, reportUnitResults, syncHeartbeat, logInfo, logError } from "../api/backend";
import { getExistingRomMShortcuts, addShortcut, setLaunchOptionsConfirmed } from "./steamShortcuts";
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
    if (_cancelRequested) {
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
    if (_cancelRequested) break;
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

      try {
        await reportUnitResults(romIdToAppId);
      } catch (e) {
        logError(`Failed to report unit results for ${data.unit_name}: ${e}`);
      }
      logInfo(`sync_apply_unit complete: ${data.unit_name} (${Object.keys(romIdToAppId).length}/${total})`);
    } finally {
      _isUnitRunning = false;
    }
  });
}
