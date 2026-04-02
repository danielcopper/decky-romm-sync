/**
 * Steam collection management for RomM platforms.
 * Uses Steam's internal collectionStore API.
 *
 * Collection naming is configurable:
 *   - prefix (default: "") e.g. "RomM: "
 *   - hostname suffix (default: false) e.g. " (steamdeck)"
 *
 * Platform collection:  {prefix}{PlatformName}{hostnameSuffix}
 * Named collection:     {prefix}[{CollectionName}]{hostnameSuffix}
 */

import { logInfo, logWarn, logError } from "../api/backend";

let _hostname = "";

// ── Configurable naming ───────────────────────────────────────
let _prefix = "";
let _includeHostname = false;

export function setCollectionNaming(prefix: string, includeHostname: boolean): void {
  _prefix = prefix;
  _includeHostname = includeHostname;
}

async function hostnameSuffix(): Promise<string> {
  if (!_includeHostname) return "";
  const h = await getHostname();
  return ` (${h})`;
}

async function platformCollectionName(platformName: string): Promise<string> {
  return `${_prefix}${platformName}${await hostnameSuffix()}`;
}

async function namedCollectionName(collName: string): Promise<string> {
  return `${_prefix}[${collName}]${await hostnameSuffix()}`;
}

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
    logInfo(`Creating/updating collections for platforms: ${Object.keys(platformAppIds).join(", ")} (hostname: ${hostname})`);

    const entries = Object.entries(platformAppIds);
    let idx = 0;
    for (const [platformName, appIds] of entries) {
      idx++;
      onProgress?.(idx, entries.length, platformName);
      const collectionName = await platformCollectionName(platformName);
      const overviews = getOverviews(appIds);

      try {
        const existing = collectionStore.userCollections.find(
          (c) => c.displayName === collectionName
        );

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
      const collectionName = await namedCollectionName(collName);
      const overviews = getOverviews(appIds);

      try {
        const existing = collectionStore.userCollections.find(
          (c) => c.displayName === collectionName
        );

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

/**
 * Append apps to existing platform collections (or create if new).
 * Unlike createOrUpdateCollections, this does NOT remove existing apps first.
 * Used for incremental collection updates during sync.
 */
export async function appendToCollections(
  platformAppIds: Record<string, number[]>,
): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") return;
    for (const [platformName, appIds] of Object.entries(platformAppIds)) {
      if (appIds.length === 0) continue;
      const collectionName = await platformCollectionName(platformName);
      const overviews = getOverviews(appIds);
      try {
        const existing = collectionStore.userCollections.find(
          (c) => c.displayName === collectionName
        );
        if (existing) {
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
          logInfo(`Appended ${appIds.length} apps to platform collection "${collectionName}"`);
        } else {
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
          logInfo(`Created platform collection "${collectionName}" with ${appIds.length} apps`);
        }
      } catch (e) {
        logError(`Failed to append to platform collection "${collectionName}": ${e}`);
      }
    }
  } catch (e) {
    logError(`appendToCollections failed: ${e}`);
  }
}

/**
 * Append apps to existing RomM collections (or create if new).
 * Unlike createOrUpdateRomMCollections, this does NOT remove existing apps first.
 * Used for incremental collection updates during sync.
 */
export async function appendToRomMCollections(
  collectionAppIds: Record<string, number[]>,
): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") return;
    for (const [collName, appIds] of Object.entries(collectionAppIds)) {
      if (appIds.length === 0) continue;
      const collectionName = await namedCollectionName(collName);
      const overviews = getOverviews(appIds);
      try {
        const existing = collectionStore.userCollections.find(
          (c) => c.displayName === collectionName
        );
        if (existing) {
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
          logInfo(`Appended ${appIds.length} apps to RomM collection "${collectionName}"`);
        } else {
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
          logInfo(`Created RomM collection "${collectionName}" with ${appIds.length} apps`);
        }
      } catch (e) {
        logError(`Failed to append to RomM collection "${collectionName}": ${e}`);
      }
    }
  } catch (e) {
    logError(`appendToRomMCollections failed: ${e}`);
  }
}

export async function clearPlatformCollection(platformName: string): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") {
      logWarn("collectionStore not available, cannot clear platform collection");
      return;
    }
    const currentName = await platformCollectionName(platformName);
    const hostname = await getHostname();
    // Also match old naming conventions for cleanup
    const legacyScoped = `RomM: ${platformName} (${hostname})`;
    const legacyBare = `RomM: ${platformName}`;

    const namesToDelete = new Set([currentName, legacyScoped, legacyBare]);
    let deleted = false;
    for (const name of namesToDelete) {
      const coll = collectionStore.userCollections.find(
        (c) => c.displayName === name
      );
      if (coll) {
        logInfo(`Deleting collection "${name}" (id=${coll.id})`);
        await coll.Delete();
        deleted = true;
      }
    }
    if (!deleted) {
      logInfo(`Collection "${currentName}" not found, nothing to clear`);
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
    const hostSuffix = ` (${hostname})`;

    // Match collections created by this plugin under any naming convention:
    //   Current format:  "{prefix}PlatformName{hostnameSuffix}"  or  "{prefix}[CollName]{hostnameSuffix}"
    //   Legacy format:   "RomM: PlatformName (hostname)"  or  "RomM: PlatformName" (bare)
    // Strategy: match by current prefix OR legacy "RomM: " prefix, plus hostname scoping
    const prefixes = new Set([_prefix]);
    prefixes.add("RomM: "); // always clean up legacy
    if (_prefix) prefixes.add(""); // if prefix is set, also clean bare names from when prefix was empty

    const rommCollections = collectionStore.userCollections.filter((c) => {
      for (const pfx of prefixes) {
        if (!c.displayName.startsWith(pfx)) continue;
        // Matches current hostname suffix
        if (c.displayName.endsWith(hostSuffix)) return true;
        // Matches collections without any " (...)" suffix (bare/current-no-hostname)
        if (!/\s\([^)]+\)$/.test(c.displayName)) return true;
      }
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
