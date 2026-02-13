import { addEventListener } from "@decky/api";
import type { SyncApplyData } from "../types";
import { reportSyncResults } from "../api/backend";
import { getExistingRomMShortcuts, addShortcut, removeShortcut } from "./steamShortcuts";
import { createOrUpdateCollections } from "./collections";

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Initialize the sync manager that listens for sync_apply events from the backend.
 * Returns the event listener handle for cleanup.
 */
export function initSyncManager(): ReturnType<typeof addEventListener> {
  return addEventListener("sync_apply", async (data: SyncApplyData) => {
    console.log("[RomM] sync_apply received:", data.shortcuts.length, "add,", data.remove_rom_ids.length, "remove");

    const existing = await getExistingRomMShortcuts();
    const romIdToAppId: Record<string, number> = {};
    const removedRomIds: number[] = [];

    // Process additions/updates with small delays to avoid corrupting Steam state
    for (const item of data.shortcuts) {
      try {
        const existingAppId = existing.get(item.rom_id);
        if (existingAppId) {
          // Already exists — update properties
          SteamClient.Apps.SetShortcutName(existingAppId, item.name);
          SteamClient.Apps.SetShortcutExe(existingAppId, item.exe);
          SteamClient.Apps.SetShortcutStartDir(existingAppId, item.start_dir);
          SteamClient.Apps.SetAppLaunchOptions(existingAppId, item.launch_options);
          if (item.cover_base64) {
            try {
              await SteamClient.Apps.SetCustomArtworkForApp(existingAppId, item.cover_base64, "png", 0);
              console.log(`[RomM] Updated cover artwork for ${item.name} (appId=${existingAppId})`);
            } catch (artErr) {
              console.error(`[RomM] Failed to set artwork for ${item.name}:`, artErr);
            }
          }
          romIdToAppId[String(item.rom_id)] = existingAppId;
        } else {
          // New — create shortcut (addShortcut already has internal 300ms delay)
          const appId = await addShortcut(item);
          if (appId) {
            romIdToAppId[String(item.rom_id)] = appId;
          }
        }
      } catch (e) {
        console.error(`[RomM] Failed to process shortcut for rom ${item.rom_id}:`, e);
      }
      // Small delay between operations to avoid overwhelming Steam
      await delay(50);
    }

    // Process removals
    for (const romId of data.remove_rom_ids) {
      const appId = existing.get(romId);
      if (appId) {
        removeShortcut(appId);
        removedRomIds.push(romId);
        await delay(50);
      }
    }

    // Report results to backend
    try {
      await reportSyncResults(romIdToAppId, removedRomIds);
    } catch (e) {
      console.error("[RomM] Failed to report sync results:", e);
    }

    // Create/update Steam collections per platform
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
    createOrUpdateCollections(platformAppIds);

    console.log("[RomM] sync_apply complete:", Object.keys(romIdToAppId).length, "added/updated,", removedRomIds.length, "removed");
  });
}
