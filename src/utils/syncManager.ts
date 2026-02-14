import { addEventListener } from "@decky/api";
import type { SyncApplyData } from "../types";
import { getArtworkBase64, reportSyncResults } from "../api/backend";
import { getExistingRomMShortcuts, addShortcut, removeShortcut } from "./steamShortcuts";
import { createOrUpdateCollections, clearPlatformCollection } from "./collections";
import { updateSyncProgress } from "./syncProgress";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

let _cancelRequested = false;

/** Request cancellation of the frontend shortcut processing loop. */
export function requestSyncCancel(): void {
  _cancelRequested = true;
}

/**
 * Initialize the sync manager that listens for sync_apply events from the backend.
 * Returns the event listener handle for cleanup.
 */
export function initSyncManager(): ReturnType<typeof addEventListener> {
  return addEventListener("sync_apply", async (data: SyncApplyData) => {
    console.log("[RomM] sync_apply received:", data.shortcuts.length, "add,", data.remove_rom_ids.length, "remove");

    _cancelRequested = false;
    let cancelled = false;

    const existing = await getExistingRomMShortcuts();
    const romIdToAppId: Record<string, number> = {};
    const removedRomIds: number[] = [];

    // Process additions/updates with small delays to avoid corrupting Steam state
    const total = data.shortcuts.length;
    updateSyncProgress({ running: true, phase: "applying", current: 0, total, message: "Applying shortcuts..." });
    for (let i = 0; i < data.shortcuts.length; i++) {
      const item = data.shortcuts[i];
      try {
        updateSyncProgress({ current: i + 1, message: `Applying ${i + 1}/${total}: ${item.name}` });
        let appId: number | undefined;
        const existingAppId = existing.get(item.rom_id);
        if (existingAppId) {
          // Already exists — update properties
          SteamClient.Apps.SetShortcutName(existingAppId, item.name);
          SteamClient.Apps.SetShortcutExe(existingAppId, item.exe);
          SteamClient.Apps.SetShortcutStartDir(existingAppId, item.start_dir);
          SteamClient.Apps.SetAppLaunchOptions(existingAppId, item.launch_options);
          appId = existingAppId;
          romIdToAppId[String(item.rom_id)] = existingAppId;
        } else {
          // New — create shortcut (addShortcut already has internal 500ms delay)
          const newAppId = await addShortcut(item);
          if (newAppId) {
            appId = newAppId;
            romIdToAppId[String(item.rom_id)] = newAppId;
          }
        }

        // Fetch and apply artwork per item via callable (avoids bulk base64 in WebSocket)
        if (appId) {
          try {
            const artResult = await getArtworkBase64(item.rom_id);
            if (artResult.base64) {
              await SteamClient.Apps.SetCustomArtworkForApp(appId, artResult.base64, "png", 0);
              console.log(`[RomM] Set cover artwork for ${item.name} (appId=${appId})`);
            }
          } catch (artErr) {
            console.error(`[RomM] Failed to fetch/set artwork for ${item.name}:`, artErr);
          }
        }
      } catch (e) {
        console.error(`[RomM] Failed to process shortcut for rom ${item.rom_id}:`, e);
      }
      // Small delay between operations to avoid overwhelming Steam
      await delay(50);

      if (_cancelRequested) {
        console.log(`[RomM] Cancel requested after processing ${i + 1}/${total} shortcuts`);
        cancelled = true;
        break;
      }
    }

    // Process removals (skip if cancelled)
    if (!cancelled) {
      for (const romId of data.remove_rom_ids) {
        const appId = existing.get(romId);
        if (appId) {
          removeShortcut(appId);
          removedRomIds.push(romId);
          await delay(50);
        }

        if (_cancelRequested) {
          console.log("[RomM] Cancel requested during removals");
          cancelled = true;
          break;
        }
      }
    }

    // Create/update Steam collections for whatever was processed
    const platformAppIds: Record<string, number[]> = {};
    for (const item of data.shortcuts) {
      const appId = romIdToAppId[String(item.rom_id)];
      if (appId) {
        if (!platformAppIds[item.platform_name]) {
          platformAppIds[item.platform_name] = [];
        }
        platformAppIds[item.platform_name].push(appId);
      }
    }
    if (!cancelled && Object.keys(platformAppIds).length > 0) {
      const numCollections = Object.keys(platformAppIds).length;
      updateSyncProgress({ phase: "collections", current: 0, total: numCollections, message: "Creating collections..." });
      await createOrUpdateCollections(platformAppIds, (cur, colTotal, name) => {
        updateSyncProgress({ current: cur, total: colTotal, message: `Collection ${cur}/${colTotal}: ${name}` });
      });
    }

    // Clean up collections for platforms that are no longer synced
    if (!cancelled && typeof collectionStore !== "undefined") {
      const activePlatforms = new Set(Object.keys(platformAppIds));
      const staleCollections = collectionStore.userCollections.filter(
        (c) => c.displayName.startsWith("RomM: ") && !activePlatforms.has(c.displayName.slice(6))
      );
      for (const c of staleCollections) {
        console.log(`[RomM] Removing stale collection "${c.displayName}"`);
        await clearPlatformCollection(c.displayName.slice(6));
      }
    }

    // Report results to backend — always call this so partial progress is saved
    try {
      await reportSyncResults(romIdToAppId, removedRomIds);
    } catch (e) {
      console.error("[RomM] Failed to report sync results:", e);
    }

    const doneMsg = cancelled
      ? `Sync cancelled (${Object.keys(romIdToAppId).length} processed)`
      : "Sync complete";
    updateSyncProgress({ running: false, phase: "done", current: total, total, message: doneMsg });
    console.log(`[RomM] sync_apply ${cancelled ? "cancelled" : "complete"}:`, Object.keys(romIdToAppId).length, "added/updated,", removedRomIds.length, "removed");
  });
}
