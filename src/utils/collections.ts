/**
 * Steam collection management for RomM platforms.
 * Uses Steam's internal collectionStore API.
 *
 * Collection names are machine-scoped to prevent cross-device conflicts
 * when Steam Cloud syncs collections: "RomM: Platform (hostname)"
 */

import { logInfo, logWarn, logError } from "../api/backend";

let _hostname = "";

export async function getHostname(): Promise<string> {
  if (_hostname) return _hostname;
  try {
    const info = await SteamClient.System.GetSystemInfo();
    _hostname = info.sHostname || "unknown";
  } catch {
    _hostname = "unknown";
  }
  return _hostname;
}

function getOverviews(appIds: number[]): AppStoreOverview[] {
  const overviews: AppStoreOverview[] = [];
  for (const appId of appIds) {
    if (typeof appStore !== "undefined") {
      const overview = appStore.GetAppOverviewByAppID(appId);
      if (overview) {
        overviews.push(overview);
        continue;
      }
    }
    // Fallback: construct a minimal overview
    overviews.push({ appid: appId, display_name: "", strDisplayName: "" });
  }
  return overviews;
}

export async function createOrUpdateCollections(
  platformAppIds: Record<string, number[]>,
  onProgress?: (current: number, total: number, name: string) => void,
): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") {
      logWarn("collectionStore not available, skipping collections");
      return;
    }

    const hostname = await getHostname();
    logInfo(
      `Creating/updating collections for platforms: ${Object.keys(platformAppIds).join(", ")} (hostname: ${hostname})`,
    );

    const entries = Object.entries(platformAppIds);
    let idx = 0;
    for (const [platformName, appIds] of entries) {
      idx++;
      onProgress?.(idx, entries.length, platformName);
      const collectionName = `RomM: ${platformName} (${hostname})`;
      const overviews = getOverviews(appIds);

      try {
        // Case-insensitive match: Steam collapses collection names by a
        // case-insensitive identity, so a case-variant collection ("RomM: [7 Up]
        // (host)" vs "RomM: [7 up] (host)") must be UPDATED, not shadowed by a
        // colliding new create that then overwrites it and loses its games (#1569).
        const target = collectionName.toLowerCase();
        const existing = collectionStore.userCollections.find((c) => c.displayName.toLowerCase() === target);

        if (existing) {
          logInfo(`Updating collection "${collectionName}" with ${appIds.length} apps`);
          const existingApps = existing.allApps;
          if (existingApps.length > 0) {
            existing.AsDragDropCollection().RemoveApps(existingApps);
          }
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
        } else {
          logInfo(`Creating collection "${collectionName}" with ${appIds.length} apps`);
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
        }
        logInfo(`Successfully saved collection "${collectionName}"`);
      } catch (colErr) {
        logError(`Failed to save collection "${collectionName}": ${colErr}`);
      }
    }
  } catch (e) {
    logError(`Failed to update collections: ${e}`);
  }
}

export async function createOrUpdateRomMCollections(
  collectionAppIds: Record<string, number[]>,
  onProgress?: (current: number, total: number, name: string) => void,
): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") {
      logWarn("collectionStore not available, skipping RomM collections");
      return;
    }

    const hostname = await getHostname();
    logInfo(`Creating/updating RomM collections: ${Object.keys(collectionAppIds).join(", ")} (hostname: ${hostname})`);

    const entries = Object.entries(collectionAppIds);
    let idx = 0;
    for (const [collName, appIds] of entries) {
      idx++;
      onProgress?.(idx, entries.length, collName);
      const collectionName = `RomM: [${collName}] (${hostname})`;
      const overviews = getOverviews(appIds);

      try {
        // Case-insensitive match: Steam collapses collection names by a
        // case-insensitive identity, so a case-variant collection ("RomM: [7 Up]
        // (host)" vs "RomM: [7 up] (host)") must be UPDATED, not shadowed by a
        // colliding new create that then overwrites it and loses its games (#1569).
        const target = collectionName.toLowerCase();
        const existing = collectionStore.userCollections.find((c) => c.displayName.toLowerCase() === target);

        if (existing) {
          logInfo(`Updating RomM collection "${collectionName}" with ${appIds.length} apps`);
          const existingApps = existing.allApps;
          if (existingApps.length > 0) {
            existing.AsDragDropCollection().RemoveApps(existingApps);
          }
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
        } else {
          logInfo(`Creating RomM collection "${collectionName}" with ${appIds.length} apps`);
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
        }
        logInfo(`Successfully saved RomM collection "${collectionName}"`);
      } catch (colErr) {
        logError(`Failed to save RomM collection "${collectionName}": ${colErr}`);
      }
    }
  } catch (e) {
    logError(`Failed to update RomM collections: ${e}`);
  }
}

export async function clearPlatformCollection(platformName: string): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") {
      logWarn("collectionStore not available, cannot clear platform collection");
      return;
    }
    const hostname = await getHostname();
    const scopedName = `RomM: ${platformName} (${hostname})`;
    const legacyName = `RomM: ${platformName}`;

    // Case-insensitive match on the full name: Steam's collection identity is
    // case-insensitive, so a case-variant of this platform's collection is the
    // SAME Steam collection and must still be found for deletion (#1569).
    const scopedTarget = scopedName.toLowerCase();
    const legacyTarget = legacyName.toLowerCase();

    // Delete the machine-scoped collection
    const scoped = collectionStore.userCollections.find((c) => c.displayName.toLowerCase() === scopedTarget);
    if (scoped) {
      logInfo(`Deleting collection "${scopedName}" (id=${scoped.id})`);
      await scoped.Delete();
    }

    // Also clean up legacy collection (without hostname suffix) if it exists
    const legacy = collectionStore.userCollections.find((c) => c.displayName.toLowerCase() === legacyTarget);
    if (legacy) {
      logInfo(`Deleting legacy collection "${legacyName}" (id=${legacy.id})`);
      await legacy.Delete();
    }

    if (!scoped && !legacy) {
      logInfo(`Collection "${scopedName}" not found, nothing to clear`);
    }
  } catch (e) {
    logError(`Failed to clear platform collection: ${e}`);
  }
}

export async function clearAllRomMCollections(): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") {
      logWarn("collectionStore not available, cannot clear collections");
      return;
    }
    const hostname = await getHostname();
    const suffix = ` (${hostname})`;
    const lowerSuffix = suffix.toLowerCase();

    // Match collections belonging to this machine OR legacy ones without any hostname suffix.
    // Covers both platform collections ("RomM: PlatformName (hostname)") and
    // RomM collection-based collections ("RomM: [CollectionName] (hostname)").
    // Legacy collections match "RomM: ..." but do NOT have a parenthesized suffix.
    // This avoids deleting collections from other devices like "RomM: N64 (othermachine)".
    // Prefix + host-suffix are compared case-insensitively (Steam's collection
    // identity is case-insensitive, so a case-variant of one of our names is the
    // same collection and must still be swept, #1569).
    const rommCollections = collectionStore.userCollections.filter((c) => {
      const lower = c.displayName.toLowerCase();
      if (!lower.startsWith("romm: ")) return false;
      // This machine's scoped collections (both platform and RomM collection style)
      if (lower.endsWith(lowerSuffix)) return true;
      // Legacy collections: start with "RomM: " but have no " (...)" suffix at all
      if (!/\s\([^)]+\)$/.test(c.displayName)) return true;
      return false;
    });

    logInfo(`Deleting ${rommCollections.length} RomM collections (hostname: ${hostname})`);
    for (const c of rommCollections) {
      logInfo(`Deleting collection "${c.displayName}" (id=${c.id})`);
      await c.Delete();
    }
  } catch (e) {
    logError(`Failed to clear collections: ${e}`);
  }
}
