import { addEventListener, removeEventListener } from "@decky/api";
import type { SyncApplyPlatformData, SyncApplyRemovalsData, SyncApplyDoneData, SyncAddItem, SyncChangedItem } from "../types";
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

// ── Queue mechanism for per-platform events ─────────────────
type QueueItem =
  | { type: "platform"; data: SyncApplyPlatformData }
  | { type: "removals"; data: SyncApplyRemovalsData }
  | { type: "done";     data: SyncApplyDoneData };

const _queue: QueueItem[] = [];
let _resolveWaiter: (() => void) | null = null;

function enqueue(item: QueueItem): void {
  _queue.push(item);
  if (_resolveWaiter) {
    _resolveWaiter();
    _resolveWaiter = null;
  }
}

function waitForItem(): Promise<void> {
  if (_queue.length > 0) return Promise.resolve();
  return new Promise<void>((resolve) => { _resolveWaiter = resolve; });
}

/**
 * Initialize the sync manager that listens for per-platform sync events
 * from the backend.  Returns an object with an unregister() method for
 * cleanup on plugin dismount.
 *
 * Event flow:
 *   1. Backend emits one `sync_apply_platform` per platform (queued here)
 *   2. Backend emits `sync_apply_removals` for stale shortcuts
 *   3. Backend emits `sync_apply_done` — no more events coming
 *
 * The processing loop dequeues one platform at a time and fully completes
 * it (create shortcuts → drain artwork → build collections → flush
 * persistence) before moving to the next platform.
 */
export function initSyncManager(): { unregister: () => void } {
  const h1 = addEventListener("sync_apply_platform", (data: SyncApplyPlatformData) => {
    enqueue({ type: "platform", data });
    if (!_isSyncRunning) startProcessingLoop();
  });

  const h2 = addEventListener("sync_apply_removals", (data: SyncApplyRemovalsData) => {
    enqueue({ type: "removals", data });
  });

  const h3 = addEventListener("sync_apply_done", (data: SyncApplyDoneData) => {
    enqueue({ type: "done", data });
  });

  return {
    unregister: () => {
      removeEventListener("sync_apply_platform", h1);
      removeEventListener("sync_apply_removals", h2);
      removeEventListener("sync_apply_done", h3);
    },
  };
}

// ── Main processing loop ─────────────────────────────────────

async function startProcessingLoop(): Promise<void> {
  if (_isSyncRunning) return;
  _isSyncRunning = true;
  _cancelRequested = false;

  try {
    logInfo("Per-platform processing loop started");

    // ── Shared state across all platforms ─────────────────────
    const existing = await getExistingRomMShortcuts();
    const romIdToAppId: Record<string, number> = {};
    const removedRomIds: number[] = [];
    let lastHeartbeat = Date.now();
    const HEARTBEAT_INTERVAL_MS = 10_000;

    // ── Incremental batch state ──────────────────────────────
    let useIncremental = true;
    let batchRomIdToAppId: Record<string, number> = {};
    let batchRemovedRomIds: number[] = [];
    let lastFlushTime = Date.now();
    let totalPersisted = 0;

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

    // ── Artwork queue ────────────────────────────────────────
    let artworkQueue: Promise<void>[] = [];

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

    async function throttleArtwork(): Promise<void> {
      while (artworkQueue.length >= ART_CONCURRENCY) {
        await Promise.race(artworkQueue);
      }
    }

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

    // ── Global progress counters ─────────────────────────────
    let globalProcessed = 0;
    let globalTotal = 0;
    let cancelled = false;
    let doneReceived = false;

    // ── Process queue items ──────────────────────────────────
    while (!doneReceived && !cancelled) {
      await waitForItem();

      while (_queue.length > 0 && !cancelled) {
        const item = _queue.shift()!;

        // ── sync_apply_done: no more events coming ───────────
        if (item.type === "done") {
          doneReceived = true;
          // Use the done event's totals for global progress if not already set
          if (globalTotal === 0) {
            globalTotal = item.data.total_shortcuts + item.data.total_removals;
          }
          break;
        }

        // ── sync_apply_removals: stale shortcut cleanup ──────
        if (item.type === "removals") {
          const removeIds = item.data.remove_rom_ids;
          logInfo(`Processing ${removeIds.length} removals`);
          for (let i = 0; i < removeIds.length; i++) {
            const romId = removeIds[i];
            const appId = existing.get(romId);
            if (appId) removeShortcut(appId);
            removedRomIds.push(romId);
            batchRemovedRomIds.push(romId);
            globalProcessed++;
            updateSyncProgress({
              current: globalProcessed,
              total: globalTotal || globalProcessed,
              message: `Removing shortcut ${i + 1}/${removeIds.length}`,
            });
            await delay(50);
            if (batchRemovedRomIds.length >= BATCH_SIZE) await flushBatch();
            if (_cancelRequested) { cancelled = true; break; }
          }
          await flushBatch();
          continue;
        }

        // ── sync_apply_platform: process one platform ────────
        const pData = item.data;
        const {
          platform_name,
          platform_index,
          total_platforms,
          total_shortcuts_all,
          shortcuts_before,
        } = pData;
        const platformPrefix = `${platform_name} (${platform_index}/${total_platforms})`;
        const newItems: SyncAddItem[] = pData.shortcuts ?? [];
        const changedItems: SyncChangedItem[] = pData.changed_shortcuts ?? [];
        const isDelta = changedItems.length > 0 || (pData.changed_shortcuts !== undefined);
        const platformTotal = newItems.length + changedItems.length;
        const platformAppIds: number[] = [];
        const platformRomMCollections: Record<string, number[]> = {};

        // Set global total from the first platform event
        if (globalTotal === 0) globalTotal = total_shortcuts_all;

        // Build rom_id → collection_names lookup for this platform
        const collectionMemberships = pData.collection_memberships ?? {};
        const romIdToCollections = new Map<number, string[]>();
        for (const [collName, romIds] of Object.entries(collectionMemberships)) {
          for (const rid of romIds) {
            const arr = romIdToCollections.get(rid);
            if (arr) arr.push(collName);
            else romIdToCollections.set(rid, [collName]);
          }
        }

        updateSyncProgress({
          running: true, phase: "applying",
          current: shortcuts_before, total: globalTotal,
          message: `${platformPrefix} — Starting (${platformTotal} games)`,
          step: pData.step, totalSteps: pData.total_steps,
        });
        globalProcessed = shortcuts_before;

        logInfo(`Platform ${platform_index}/${total_platforms}: ${platform_name} — ${newItems.length} new, ${changedItems.length} changed`);

        // ── New shortcuts ────────────────────────────────────
        for (let i = 0; i < newItems.length; i++) {
          const shortcut = newItems[i];
          globalProcessed = shortcuts_before + i + 1;
          updateSyncProgress({
            current: globalProcessed,
            message: `${platformPrefix} — Adding ${i + 1}/${platformTotal} — ${shortcut.name}`,
          });

          try {
            let appId: number | undefined;
            if (isDelta) {
              const newAppId = await addShortcut(shortcut);
              if (newAppId) {
                appId = newAppId;
                romIdToAppId[String(shortcut.rom_id)] = newAppId;
                batchRomIdToAppId[String(shortcut.rom_id)] = newAppId;
              }
            } else {
              const existingAppId = existing.get(shortcut.rom_id);
              if (existingAppId) {
                SteamClient.Apps.SetShortcutName(existingAppId, shortcut.name);
                SteamClient.Apps.SetShortcutExe(existingAppId, shortcut.exe);
                SteamClient.Apps.SetShortcutStartDir(existingAppId, shortcut.start_dir);
                SteamClient.Apps.SetAppLaunchOptions(existingAppId, shortcut.launch_options);
                appId = existingAppId;
                romIdToAppId[String(shortcut.rom_id)] = existingAppId;
                batchRomIdToAppId[String(shortcut.rom_id)] = existingAppId;
              } else {
                const newAppId = await addShortcut(shortcut);
                if (newAppId) {
                  appId = newAppId;
                  romIdToAppId[String(shortcut.rom_id)] = newAppId;
                  batchRomIdToAppId[String(shortcut.rom_id)] = newAppId;
                }
              }
            }

            if (appId) {
              platformAppIds.push(appId);
              enqueueArtwork(appId, shortcut.rom_id, shortcut.name);
              await throttleArtwork();
              const colls = romIdToCollections.get(shortcut.rom_id);
              if (colls) {
                for (const c of colls) {
                  if (!platformRomMCollections[c]) platformRomMCollections[c] = [];
                  platformRomMCollections[c].push(appId);
                }
              }
            }
          } catch (e) {
            logError(`Failed to process shortcut for rom ${shortcut.rom_id}: ${e}`);
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
          if (_cancelRequested) { cancelled = true; break; }
        }

        // ── Changed shortcuts (delta mode) ───────────────────
        if (!cancelled && changedItems.length > 0) {
          for (let i = 0; i < changedItems.length; i++) {
            const shortcut = changedItems[i];
            globalProcessed = shortcuts_before + newItems.length + i + 1;
            const itemIdx = newItems.length + i + 1;
            updateSyncProgress({
              current: globalProcessed,
              message: `${platformPrefix} — Updating ${itemIdx}/${platformTotal} — ${shortcut.name}`,
            });

            try {
              const appId = shortcut.existing_app_id;
              SteamClient.Apps.SetShortcutName(appId, shortcut.name);
              SteamClient.Apps.SetShortcutExe(appId, shortcut.exe);
              SteamClient.Apps.SetShortcutStartDir(appId, shortcut.start_dir);
              SteamClient.Apps.SetAppLaunchOptions(appId, shortcut.launch_options);
              romIdToAppId[String(shortcut.rom_id)] = appId;
              batchRomIdToAppId[String(shortcut.rom_id)] = appId;
              platformAppIds.push(appId);
              enqueueArtwork(appId, shortcut.rom_id, shortcut.name);
              await throttleArtwork();
              const colls = romIdToCollections.get(shortcut.rom_id);
              if (colls) {
                for (const c of colls) {
                  if (!platformRomMCollections[c]) platformRomMCollections[c] = [];
                  platformRomMCollections[c].push(appId);
                }
              }
            } catch (e) {
              logError(`Failed to update shortcut for rom ${shortcut.rom_id}: ${e}`);
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
            if (_cancelRequested) { cancelled = true; break; }
          }
        }

        // ── Platform finalization: drain artwork → collections → flush ──
        await drainArtwork(platformPrefix);

        if (platformAppIds.length > 0) {
          updateSyncProgress({ message: `${platformPrefix} — Building collections…` });
          await appendToCollections({ [platform_name]: platformAppIds });
          if (Object.keys(platformRomMCollections).length > 0) {
            await appendToRomMCollections(platformRomMCollections);
          }
        }

        await flushBatch();
        logInfo(`Platform ${platform_index}/${total_platforms} complete: ${platform_name} (${platformTotal} games, ${platformAppIds.length} shortcuts)`);

        if (cancelled) {
          logInfo(`Sync cancelled after completing platform: ${platform_name}`);
        }
      } // inner while (_queue.length > 0)
    } // outer while (!doneReceived && !cancelled)

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
    logInfo(`Sync ${cancelled ? "cancelled" : "complete"}: ${Object.keys(romIdToAppId).length} added/updated, ${removedRomIds.length} removed, ${totalPersisted} persisted incrementally`);
  } finally {
    _isSyncRunning = false;
    // Clear any remaining queue items on exit
    _queue.length = 0;
  }
}
