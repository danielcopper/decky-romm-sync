import { addEventListener } from "@decky/api";
import type { SyncAddItem, SyncApplyData, SyncApplyUnitData, SyncChangedItem } from "../types";
import { getArtworkBase64, reportSyncResults, reportUnitResults, syncHeartbeat, logInfo, logError } from "../api/backend";
import { getExistingRomMShortcuts, addShortcut, removeShortcut } from "./steamShortcuts";
import { updateSyncProgress } from "./syncProgress";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const HEARTBEAT_INTERVAL_MS = 10_000;
const ART_CONCURRENCY = 8;

interface ArtworkTarget {
  appId: number;
  romId: number;
  name: string;
}

let _cancelRequested = false;
let _isSyncRunning = false;
let _isUnitRunning = false;

/** Request cancellation of the frontend shortcut processing loop. */
export function requestSyncCancel(): void {
  _cancelRequested = true;
}

/**
 * Resolve a shortcut item to an appId: update fields on the existing
 * shortcut when one is present, otherwise create a new shortcut. Returns
 * ``undefined`` if no appId could be resolved (creation failed).
 */
async function resolveShortcutAppId(
  item: SyncAddItem,
  existing: Map<number, number>,
): Promise<number | undefined> {
  const existingAppId = existing.get(item.rom_id);
  if (existingAppId) {
    SteamClient.Apps.SetShortcutName(existingAppId, item.name);
    SteamClient.Apps.SetShortcutExe(existingAppId, item.exe);
    SteamClient.Apps.SetShortcutStartDir(existingAppId, item.start_dir);
    SteamClient.Apps.SetAppLaunchOptions(existingAppId, item.launch_options);
    return existingAppId;
  }
  return (await addShortcut(item)) ?? undefined;
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
  for (let i = 0; i < data.shortcuts.length; i++) {
    const item = data.shortcuts[i];
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
      logInfo(`sync_apply_unit received: ${data.unit_type}=${data.unit_name} (${data.unit_index + 1}/${data.total_units}), ${total} shortcuts`);

      updateSyncProgress({
        running: true,
        phase: "applying",
        current: 0,
        total,
        message: `${data.unit_name}: 0/${total}`,
        step: data.unit_index + 1,
        totalSteps: data.total_units,
      });

      const existing = await getExistingRomMShortcuts();
      await processUnitShortcuts(data, existing, romIdToAppId, artworkTargets, total);

      // Artwork — keep existing base64 path; this is a per-unit-sized batch
      // so the giant single sync_apply payload concern doesn't apply.
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

/**
 * Initialize the sync manager that listens for sync_apply events from the backend.
 * Returns the event listener handle for cleanup.
 */
export function initSyncManager(): ReturnType<typeof addEventListener> {
  return addEventListener("sync_apply", async (data: SyncApplyData) => {
    if (_isSyncRunning) {
      logInfo("sync_apply: already running, ignoring duplicate event");
      return;
    }
    _isSyncRunning = true;
    try {
      // Defensive checks against malformed event data
      if (!Array.isArray(data.shortcuts)) {
        logError("sync_apply: data.shortcuts is not an array, aborting");
        return;
      }
      if (!Array.isArray(data.remove_rom_ids)) {
        logError("sync_apply: data.remove_rom_ids is not an array, aborting");
        return;
      }
      const isDelta = Array.isArray(data.changed_shortcuts);
      logInfo(`sync_apply received: ${data.shortcuts.length} new, ${isDelta ? data.changed_shortcuts!.length + " changed, " : ""}${data.remove_rom_ids.length} remove${isDelta ? " (delta)" : ""}`);
  
      _cancelRequested = false;
      let cancelled = false;
      let lastHeartbeat = Date.now();

      const existing = await getExistingRomMShortcuts();
      const romIdToAppId: Record<string, number> = {};
      const removedRomIds: number[] = [];
      const artworkTargets: ArtworkTarget[] = [];
  
      // Step plan from backend
      let currentStep = data.next_step ?? 1;
      const totalSteps = data.total_steps ?? 3;
  
      // --- Step: Apply shortcuts (new + changed) ---
      const totalNew = data.shortcuts.length;
      const totalChanged = data.changed_shortcuts?.length ?? 0;
      const totalShortcuts = totalNew + totalChanged;
  
      if (totalShortcuts > 0) {
        updateSyncProgress({
          running: true, phase: "applying",
          current: 0, total: totalShortcuts,
          message: `Applying shortcuts 0/${totalShortcuts}`,
          step: currentStep, totalSteps,
        });
  
        for (let i = 0; i < data.shortcuts.length; i++) {
          const item = data.shortcuts[i];
          try {
            updateSyncProgress({
              current: i + 1,
              message: `Applying shortcuts ${i + 1}/${totalShortcuts}`,
            });
            let appId: number | undefined;
  
            if (isDelta) {
              const newAppId = await addShortcut(item);
              if (newAppId) {
                appId = newAppId;
                romIdToAppId[String(item.rom_id)] = newAppId;
              }
            } else {
              const existingAppId = existing.get(item.rom_id);
              if (existingAppId) {
                SteamClient.Apps.SetShortcutName(existingAppId, item.name);
                SteamClient.Apps.SetShortcutExe(existingAppId, item.exe);
                SteamClient.Apps.SetShortcutStartDir(existingAppId, item.start_dir);
                SteamClient.Apps.SetAppLaunchOptions(existingAppId, item.launch_options);
                appId = existingAppId;
                romIdToAppId[String(item.rom_id)] = existingAppId;
              } else {
                const newAppId = await addShortcut(item);
                if (newAppId) {
                  appId = newAppId;
                  romIdToAppId[String(item.rom_id)] = newAppId;
                }
              }
            }
  
            if (appId) {
              artworkTargets.push({ appId, romId: item.rom_id, name: item.name });
            }
          } catch (e) {
            logError(`Failed to process shortcut for rom ${item.rom_id}: ${e}`);
          }
          await delay(50);
  
          if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
            syncHeartbeat().catch(() => {});
            lastHeartbeat = Date.now();
          }
  
          if (_cancelRequested) {
            logInfo(`Cancel requested after processing ${i + 1}/${totalShortcuts} shortcuts`);
            cancelled = true;
            break;
          }
        }
  
        // Process changed shortcuts (delta mode only)
        if (!cancelled && isDelta && data.changed_shortcuts) {
          for (let i = 0; i < data.changed_shortcuts.length; i++) {
            const item: SyncChangedItem = data.changed_shortcuts[i];
            const idx = totalNew + i;
            try {
              updateSyncProgress({
                current: idx + 1,
                message: `Updating shortcuts ${idx + 1}/${totalShortcuts}`,
              });
              const appId = item.existing_app_id;
  
              SteamClient.Apps.SetShortcutName(appId, item.name);
              SteamClient.Apps.SetShortcutExe(appId, item.exe);
              SteamClient.Apps.SetShortcutStartDir(appId, item.start_dir);
              SteamClient.Apps.SetAppLaunchOptions(appId, item.launch_options);
              romIdToAppId[String(item.rom_id)] = appId;
  
              artworkTargets.push({ appId, romId: item.rom_id, name: item.name });
            } catch (e) {
              logError(`Failed to update shortcut for rom ${item.rom_id}: ${e}`);
            }
            await delay(50);
  
            if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
              syncHeartbeat().catch(() => {});
              lastHeartbeat = Date.now();
            }
  
            if (_cancelRequested) {
              logInfo(`Cancel requested during changed shortcuts processing`);
              cancelled = true;
              break;
            }
          }
        }
  
        currentStep++;
      }

      // --- Batch artwork fetch (parallel, up to 8 at a time) ---
      if (!cancelled && artworkTargets.length > 0) {
        for (let i = 0; i < artworkTargets.length; i += ART_CONCURRENCY) {
          if (_cancelRequested) {
            logInfo("Cancel requested during artwork fetching");
            cancelled = true;
            break;
          }
          const batch = artworkTargets.slice(i, i + ART_CONCURRENCY);
          await Promise.all(batch.map(async ({ appId, romId, name }) => {
            try {
              const artResult = await getArtworkBase64(romId);
              if (artResult.base64) {
                await SteamClient.Apps.SetCustomArtworkForApp(appId, artResult.base64, "png", 0);
                logInfo(`Set cover artwork for ${name} (appId=${appId})`);
              }
            } catch (artErr) {
              logError(`Failed to fetch/set artwork for ${name}: ${artErr}`);
            }
          }));
        }
      }

      // --- Step: Remove shortcuts ---
      if (!cancelled && data.remove_rom_ids.length > 0) {
        const totalRemovals = data.remove_rom_ids.length;
        updateSyncProgress({
          phase: "applying", current: 0, total: totalRemovals,
          message: `Removing shortcuts 0/${totalRemovals}`,
          step: currentStep, totalSteps,
        });
  
        for (let i = 0; i < data.remove_rom_ids.length; i++) {
          const romId = data.remove_rom_ids[i];
          const appId = existing.get(romId);
          if (appId) {
            removeShortcut(appId);
          }
          removedRomIds.push(romId);
          updateSyncProgress({
            current: i + 1,
            message: `Removing shortcuts ${i + 1}/${totalRemovals}`,
          });
          await delay(50);
  
          if (_cancelRequested) {
            logInfo("Cancel requested during removals");
            cancelled = true;
            break;
          }
        }
  
        currentStep++;
      }
  
      // Report results to backend
      try {
        await reportSyncResults(romIdToAppId, removedRomIds, cancelled);
      } catch (e) {
        logError(`Failed to report sync results: ${e}`);
      }
  
      const doneMsg = cancelled
        ? `Sync cancelled (${Object.keys(romIdToAppId).length} processed)`
        : "Sync complete";
      updateSyncProgress({ running: false, phase: "done", message: doneMsg });
      logInfo(`sync_apply ${cancelled ? "cancelled" : "complete"}: ${Object.keys(romIdToAppId).length} added/updated, ${removedRomIds.length} removed`);
    } finally {
      _isSyncRunning = false;
    }
  });
}
