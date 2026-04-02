import { addEventListener } from "@decky/api";
import type { SyncApplyData, SyncChangedItem } from "../types";
import {
  getArtworkBase64,
  reportSyncResults,
  reportIncrementalResults,
  reportSyncFinalized,
  syncHeartbeat,
  logInfo,
  logWarn,
  logError,
} from "../api/backend";
import { getExistingRomMShortcuts, addShortcut, removeShortcut } from "./steamShortcuts";
import { updateSyncProgress } from "./syncProgress";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

let _cancelRequested = false;
let _isSyncRunning = false;

/** Request cancellation of the frontend shortcut processing loop. */
export function requestSyncCancel(): void {
  _cancelRequested = true;
}

// ── Incremental batch configuration ────────────────────────
const BATCH_SIZE = 20;            // Persist every 20 shortcuts
const BATCH_TIMEOUT_MS = 5_000;   // Or every 5 seconds, whichever comes first
const ART_CONCURRENCY = 8;        // Max parallel artwork fetches

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
      const HEARTBEAT_INTERVAL_MS = 10_000;
  
      const existing = await getExistingRomMShortcuts();
      const romIdToAppId: Record<string, number> = {};
      const removedRomIds: number[] = [];

      // ── Incremental batch state ────────────────────────────
      let useIncremental = true;
      let batchRomIdToAppId: Record<string, number> = {};
      let batchRemovedRomIds: number[] = [];
      let lastFlushTime = Date.now();
      let totalPersisted = 0;

      /** Flush the current batch to the backend for incremental persistence. */
      async function flushBatch(): Promise<void> {
        if (!useIncremental) return;
        const batchCount = Object.keys(batchRomIdToAppId).length + batchRemovedRomIds.length;
        if (batchCount === 0) return;

        const toSend = { ...batchRomIdToAppId };
        const toRemove = [...batchRemovedRomIds];
        batchRomIdToAppId = {};
        batchRemovedRomIds = [];
        lastFlushTime = Date.now();

        try {
          const result = await reportIncrementalResults(toSend, toRemove);
          if (result.success) {
            totalPersisted += result.persisted;
            logInfo(`Incremental batch persisted: ${result.persisted} items (total: ${totalPersisted})`);
          }
        } catch (e) {
          logWarn(`Incremental reporting not available, falling back to batch mode: ${e}`);
          useIncremental = false;
          // Re-add to full accumulator — they'll be caught by the final report
          // (romIdToAppId already has them, so no data loss)
        }
      }

      // In-flight artwork promises for parallel approach
      let artworkQueue: Promise<void>[] = [];

      /** Fire artwork fetch for a shortcut (non-blocking, limited concurrency). */
      function enqueueArtwork(appId: number, romId: number, name: string): void {
        const p: Promise<void> = getArtworkBase64(romId)
          .then(async (artResult) => {
            if (artResult.base64) {
              await SteamClient.Apps.SetCustomArtworkForApp(appId, artResult.base64, "png", 0);
            }
          })
          .catch((artErr) => { logError(`Artwork failed for ${name}: ${artErr}`); })
          .finally(() => { artworkQueue = artworkQueue.filter((q) => q !== p); });
        artworkQueue.push(p);
      }

      /** Wait for artwork queue to drain below concurrency limit. */
      async function throttleArtwork(): Promise<void> {
        while (artworkQueue.length >= ART_CONCURRENCY) {
          await Promise.race(artworkQueue);
        }
      }
  
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
                batchRomIdToAppId[String(item.rom_id)] = newAppId;
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
                batchRomIdToAppId[String(item.rom_id)] = existingAppId;
              } else {
                const newAppId = await addShortcut(item);
                if (newAppId) {
                  appId = newAppId;
                  romIdToAppId[String(item.rom_id)] = newAppId;
                  batchRomIdToAppId[String(item.rom_id)] = newAppId;
                }
              }
            }
  
            // Enqueue artwork fetch immediately (parallel, non-blocking)
            if (appId) {
              enqueueArtwork(appId, item.rom_id, item.name);
              await throttleArtwork();
            }
          } catch (e) {
            logError(`Failed to process shortcut for rom ${item.rom_id}: ${e}`);
          }
          await delay(50);

          // Flush batch when threshold reached
          const batchCount = Object.keys(batchRomIdToAppId).length;
          const timeSinceFlush = Date.now() - lastFlushTime;
          if (batchCount >= BATCH_SIZE || (batchCount > 0 && timeSinceFlush >= BATCH_TIMEOUT_MS)) {
            await flushBatch();
          }
  
          if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
            syncHeartbeat().catch(() => {});
            lastHeartbeat = Date.now();
          }
  
          if (_cancelRequested) {
            logInfo(`Cancel requested after processing ${i + 1}/${totalShortcuts} shortcuts`);
            cancelled = true;
            await flushBatch();
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
              batchRomIdToAppId[String(item.rom_id)] = appId;
  
              // Enqueue artwork for changed shortcuts too
              enqueueArtwork(appId, item.rom_id, item.name);
              await throttleArtwork();
            } catch (e) {
              logError(`Failed to update shortcut for rom ${item.rom_id}: ${e}`);
            }
            await delay(50);

            // Flush batch when threshold reached
            const batchCount = Object.keys(batchRomIdToAppId).length;
            const timeSinceFlush = Date.now() - lastFlushTime;
            if (batchCount >= BATCH_SIZE || (batchCount > 0 && timeSinceFlush >= BATCH_TIMEOUT_MS)) {
              await flushBatch();
            }
  
            if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {
              syncHeartbeat().catch(() => {});
              lastHeartbeat = Date.now();
            }
  
            if (_cancelRequested) {
              logInfo(`Cancel requested during changed shortcuts processing`);
              cancelled = true;
              await flushBatch();
              break;
            }
          }
        }
  
        // Flush any remaining shortcuts before moving to next phase
        await flushBatch();
        currentStep++;
      }

      // --- Wait for any remaining in-flight artwork ---
      if (artworkQueue.length > 0) {
        logInfo(`Waiting for ${artworkQueue.length} remaining artwork fetches...`);
        await Promise.allSettled(artworkQueue);
        artworkQueue = [];
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
          batchRemovedRomIds.push(romId);
          updateSyncProgress({
            current: i + 1,
            message: `Removing shortcuts ${i + 1}/${totalRemovals}`,
          });
          await delay(50);

          // Flush removal batch when threshold reached
          if (batchRemovedRomIds.length >= BATCH_SIZE) {
            await flushBatch();
          }
  
          if (_cancelRequested) {
            logInfo("Cancel requested during removals");
            cancelled = true;
            await flushBatch();
            break;
          }
        }
  
        // Flush any remaining removals
        await flushBatch();
        currentStep++;
      }
  
      // ── Finalize: report to backend ──────────────────────────
      // If incremental mode was used, call reportSyncFinalized (which
      // only needs to handle stragglers + collection building + last_sync).
      // If incremental mode failed, fall back to the legacy reportSyncResults.
      try {
        if (useIncremental) {
          // All items already persisted incrementally — pass empty maps
          // for the "remaining" parameter since flushBatch already sent them.
          await reportSyncFinalized({}, [], cancelled);
        } else {
          // Fallback: legacy all-at-once report
          await reportSyncResults(romIdToAppId, removedRomIds, cancelled);
        }
      } catch (e) {
        logError(`Failed to report sync results: ${e}`);
        // Last-resort fallback: try the old API if finalized failed
        if (useIncremental) {
          try {
            logWarn("reportSyncFinalized failed, attempting legacy reportSyncResults...");
            await reportSyncResults(romIdToAppId, removedRomIds, cancelled);
          } catch (error_) {
            logError(`Legacy fallback also failed: ${error_}`);
          }
        }
      }
  
      const doneMsg = cancelled
        ? `Sync cancelled (${Object.keys(romIdToAppId).length} processed, ${totalPersisted} persisted)`
        : "Sync complete";
      updateSyncProgress({ running: false, phase: "done", message: doneMsg });
      logInfo(`sync_apply ${cancelled ? "cancelled" : "complete"}: ${Object.keys(romIdToAppId).length} added/updated, ${removedRomIds.length} removed, ${totalPersisted} persisted incrementally`);
    } finally {
      _isSyncRunning = false;
    }
  });
}
