import { addEventListener } from "@decky/api";
import type { SyncApplyData, SyncAddItem, SyncChangedItem } from "../types";
import {
  downloadAndGetArtwork,
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
import { appendToCollections, appendToRomMCollections } from "./collections";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

let _cancelRequested = false;
let _isSyncRunning = false;

/** Request cancellation of the frontend shortcut processing loop. */
export function requestSyncCancel(): void {
  _cancelRequested = true;
}

// ── Configuration ──────────────────────────────────────────
const BATCH_SIZE = 20;            // Persist every 20 shortcuts
const BATCH_TIMEOUT_MS = 5_000;   // Or every 5 seconds, whichever comes first
const ART_CONCURRENCY = 8;        // Max parallel artwork fetches

/** Group items by platform_name, preserving insertion order. */
function groupByPlatform<T extends { platform_name: string }>(items: T[]): Map<string, T[]> {
  const map = new Map<string, T[]>();
  for (const item of items) {
    const list = map.get(item.platform_name);
    if (list) list.push(item);
    else map.set(item.platform_name, [item]);
  }
  return map;
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
        }
      }

      // In-flight artwork promises for parallel approach
      let artworkQueue: Promise<void>[] = [];

      /** Fire artwork fetch for a shortcut (non-blocking, limited concurrency). */
      function enqueueArtwork(appId: number, romId: number, name: string): void {
        const p: Promise<void> = downloadAndGetArtwork(romId)
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

      /** Wait for all in-flight artwork to finish, showing live countdown. */
      async function drainArtwork(prefix: string): Promise<void> {
        if (artworkQueue.length === 0) return;
        let remaining = artworkQueue.length;
        updateSyncProgress({ message: `${prefix} — Finishing artwork (${remaining} remaining)…` });
        while (artworkQueue.length > 0) {
          await Promise.race(artworkQueue);
          remaining = artworkQueue.length;
          if (remaining > 0) {
            updateSyncProgress({ message: `${prefix} — Finishing artwork (${remaining} remaining)…` });
          }
        }
      }

      // ── Build rom_id→collection_names lookup ───────────────
      const collectionMemberships = data.collection_memberships ?? {};
      const romIdToCollections = new Map<number, string[]>();
      for (const [collName, romIds] of Object.entries(collectionMemberships)) {
        for (const rid of romIds) {
          const arr = romIdToCollections.get(rid);
          if (arr) arr.push(collName);
          else romIdToCollections.set(rid, [collName]);
        }
      }

      // Step plan from backend
      const currentStep = data.next_step ?? 1;
      const totalSteps = data.total_steps ?? 3;

      // ── Group shortcuts by platform ────────────────────────
      const newByPlatform = groupByPlatform(data.shortcuts);
      const changedByPlatform = isDelta && data.changed_shortcuts
        ? groupByPlatform(data.changed_shortcuts)
        : new Map<string, SyncChangedItem[]>();

      // Build ordered list of unique platform names
      const platformSet = new Set<string>();
      for (const name of newByPlatform.keys()) platformSet.add(name);
      for (const name of changedByPlatform.keys()) platformSet.add(name);
      const platformNames = [...platformSet];
      const totalPlatforms = platformNames.length;

      // Overall totals (for global progress bar)
      const totalShortcuts = data.shortcuts.length + (data.changed_shortcuts?.length ?? 0);
      const totalRemovals = data.remove_rom_ids.length;
      const totalWork = totalShortcuts + totalRemovals;
      let globalProcessed = 0;

      logInfo(`Per-platform processing: ${totalPlatforms} platforms, ${totalShortcuts} shortcuts, ${totalRemovals} removals`);

      // ── Per-platform processing loop ───────────────────────
      for (let pIdx = 0; pIdx < totalPlatforms; pIdx++) {
        const platformName = platformNames[pIdx];
        const platformPrefix = `${platformName} (${pIdx + 1}/${totalPlatforms})`;
        const newItems: SyncAddItem[] = newByPlatform.get(platformName) ?? [];
        const changedItems: SyncChangedItem[] = changedByPlatform.get(platformName) ?? [];
        const platformTotal = newItems.length + changedItems.length;
        const platformAppIds: number[] = [];
        const platformRomMCollections: Record<string, number[]> = {};

        updateSyncProgress({
          running: true, phase: "applying",
          current: globalProcessed, total: totalWork,
          message: `${platformPrefix} — Starting (${platformTotal} games)`,
          step: currentStep, totalSteps,
        });

        // ── New shortcuts for this platform ──────────────────
        for (let i = 0; i < newItems.length; i++) {
          const item = newItems[i];
          globalProcessed++;
          updateSyncProgress({
            current: globalProcessed,
            message: `${platformPrefix} — Adding ${i + 1}/${platformTotal} — ${item.name}`,
          });

          try {
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

            if (appId) {
              platformAppIds.push(appId);
              enqueueArtwork(appId, item.rom_id, item.name);
              await throttleArtwork();
              // Track RomM collection memberships for this rom
              const colls = romIdToCollections.get(item.rom_id);
              if (colls) {
                for (const c of colls) {
                  if (!platformRomMCollections[c]) platformRomMCollections[c] = [];
                  platformRomMCollections[c].push(appId);
                }
              }
            }
          } catch (e) {
            logError(`Failed to process shortcut for rom ${item.rom_id}: ${e}`);
          }
          await delay(50);

          // Flush persistence batch when threshold reached
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
            cancelled = true;
            await flushBatch();
            break;
          }
        }

        // ── Changed shortcuts for this platform (delta mode) ─
        if (!cancelled && changedItems.length > 0) {
          for (let i = 0; i < changedItems.length; i++) {
            const item = changedItems[i];
            globalProcessed++;
            const itemIdx = newItems.length + i + 1;
            updateSyncProgress({
              current: globalProcessed,
              message: `${platformPrefix} — Updating ${itemIdx}/${platformTotal} — ${item.name}`,
            });

            try {
              const appId = item.existing_app_id;
              SteamClient.Apps.SetShortcutName(appId, item.name);
              SteamClient.Apps.SetShortcutExe(appId, item.exe);
              SteamClient.Apps.SetShortcutStartDir(appId, item.start_dir);
              SteamClient.Apps.SetAppLaunchOptions(appId, item.launch_options);
              romIdToAppId[String(item.rom_id)] = appId;
              batchRomIdToAppId[String(item.rom_id)] = appId;
              platformAppIds.push(appId);
              enqueueArtwork(appId, item.rom_id, item.name);
              await throttleArtwork();
              const colls = romIdToCollections.get(item.rom_id);
              if (colls) {
                for (const c of colls) {
                  if (!platformRomMCollections[c]) platformRomMCollections[c] = [];
                  platformRomMCollections[c].push(appId);
                }
              }
            } catch (e) {
              logError(`Failed to update shortcut for rom ${item.rom_id}: ${e}`);
            }
            await delay(50);

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
              cancelled = true;
              await flushBatch();
              break;
            }
          }
        }

        if (cancelled) {
          // Even on cancel, finalize what we have for this platform
          await drainArtwork(platformPrefix);
          if (platformAppIds.length > 0) {
            await appendToCollections({ [platformName]: platformAppIds });
            if (Object.keys(platformRomMCollections).length > 0) {
              await appendToRomMCollections(platformRomMCollections);
            }
          }
          await flushBatch();
          logInfo(`Platform cancelled mid-way: ${platformName} (${platformAppIds.length} completed)`);
          break;
        }

        // ── Platform complete: drain artwork, build collections, flush ──
        await drainArtwork(platformPrefix);

        if (platformAppIds.length > 0) {
          await appendToCollections({ [platformName]: platformAppIds });
          if (Object.keys(platformRomMCollections).length > 0) {
            await appendToRomMCollections(platformRomMCollections);
          }
        }

        await flushBatch();
        logInfo(`Platform ${pIdx + 1}/${totalPlatforms} complete: ${platformName} (${platformTotal} games, ${platformAppIds.length} shortcuts)`);
      }

      // ── Removals (cross-platform, at the end) ──────────────
      if (!cancelled && data.remove_rom_ids.length > 0) {
        for (let i = 0; i < data.remove_rom_ids.length; i++) {
          const romId = data.remove_rom_ids[i];
          const appId = existing.get(romId);
          if (appId) {
            removeShortcut(appId);
          }
          removedRomIds.push(romId);
          batchRemovedRomIds.push(romId);
          globalProcessed++;
          updateSyncProgress({
            current: globalProcessed,
            total: totalWork,
            message: `Removing shortcut ${i + 1}/${totalRemovals}`,
          });
          await delay(50);

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
        await flushBatch();
      }
  
      // ── Finalize: report to backend ──────────────────────────
      updateSyncProgress({ message: "Finalizing sync…" });
      try {
        if (useIncremental) {
          await reportSyncFinalized({}, [], cancelled);
        } else {
          await reportSyncResults(romIdToAppId, removedRomIds, cancelled);
        }
      } catch (e) {
        logError(`Failed to report sync results: ${e}`);
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
