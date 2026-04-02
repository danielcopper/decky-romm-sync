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

import { logInfo, logWarn, logError, getCollectionRegistry, saveCollectionRegistry } from "../api/backend";

let _hostname = "";

// ── Configurable naming ───────────────────────────────────────
let _prefix = "";
let _includeHostname = false;

export function setCollectionNaming(prefix: string, includeHostname: boolean): void {
  _prefix = prefix;
  _includeHostname = includeHostname;
}

// ── Collection registry ───────────────────────────────────────
// Maps Steam collection ID → stable key ("platform:{slug}" or "named:{name}")
// Persisted to backend settings so deletions work regardless of naming changes.
let _registry: Record<string, string> = {};
let _registryLoaded = false;

async function loadRegistry(): Promise<void> {
  if (_registryLoaded) return;
  try {
    _registry = await getCollectionRegistry();
  } catch {
    _registry = {};
  }
  _registryLoaded = true;
}

async function persistRegistry(): Promise<void> {
  try {
    await saveCollectionRegistry(_registry);
  } catch (e) {
    logError(`Failed to persist collection registry: ${e}`);
  }
}

function registerCollection(steamId: string, stableKey: string): void {
  _registry[steamId] = stableKey;
}

function unregisterCollection(steamId: string): void {
  delete _registry[steamId];
}

/** Invalidate in-memory cache so next operation reloads from backend. */
export function invalidateCollectionRegistry(): void {
  _registryLoaded = false;
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
    await loadRegistry();

    const hostname = await getHostname();
    logInfo(`Creating/updating collections for platforms: ${Object.keys(platformAppIds).join(", ")} (hostname: ${hostname})`);

    let dirty = false;
    const entries = Object.entries(platformAppIds);
    let idx = 0;
    for (const [platformName, appIds] of entries) {
      idx++;
      onProgress?.(idx, entries.length, platformName);
      const collectionName = await platformCollectionName(platformName);
      const stableKey = `platform:${platformName}`;
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
          registerCollection(existing.id, stableKey);
          dirty = true;
        } else {
          logInfo(`Creating collection "${collectionName}" with ${appIds.length} apps`);
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
          registerCollection(collection.id, stableKey);
          dirty = true;
        }
        logInfo(`Successfully saved collection "${collectionName}"`);
      } catch (colErr) {
        logError(`Failed to save collection "${collectionName}": ${colErr}`);
      }
    }
    if (dirty) await persistRegistry();
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
    await loadRegistry();

    const hostname = await getHostname();
    logInfo(`Creating/updating RomM collections: ${Object.keys(collectionAppIds).join(", ")} (hostname: ${hostname})`);

    let dirty = false;
    const entries = Object.entries(collectionAppIds);
    let idx = 0;
    for (const [collName, appIds] of entries) {
      idx++;
      onProgress?.(idx, entries.length, collName);
      const collectionName = await namedCollectionName(collName);
      const stableKey = `named:${collName}`;
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
          registerCollection(existing.id, stableKey);
          dirty = true;
        } else {
          logInfo(`Creating RomM collection "${collectionName}" with ${appIds.length} apps`);
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
          registerCollection(collection.id, stableKey);
          dirty = true;
        }
        logInfo(`Successfully saved RomM collection "${collectionName}"`);
      } catch (colErr) {
        logError(`Failed to save RomM collection "${collectionName}": ${colErr}`);
      }
    }
    if (dirty) await persistRegistry();
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
    await loadRegistry();
    let dirty = false;
    for (const [platformName, appIds] of Object.entries(platformAppIds)) {
      if (appIds.length === 0) continue;
      const collectionName = await platformCollectionName(platformName);
      const stableKey = `platform:${platformName}`;
      const overviews = getOverviews(appIds);
      try {
        const existing = collectionStore.userCollections.find(
          (c) => c.displayName === collectionName
        );
        if (existing) {
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
          registerCollection(existing.id, stableKey);
          dirty = true;
          logInfo(`Appended ${appIds.length} apps to platform collection "${collectionName}"`);
        } else {
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
          registerCollection(collection.id, stableKey);
          dirty = true;
          logInfo(`Created platform collection "${collectionName}" with ${appIds.length} apps`);
        }
      } catch (e) {
        logError(`Failed to append to platform collection "${collectionName}": ${e}`);
      }
    }
    if (dirty) await persistRegistry();
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
    await loadRegistry();
    let dirty = false;
    for (const [collName, appIds] of Object.entries(collectionAppIds)) {
      if (appIds.length === 0) continue;
      const collectionName = await namedCollectionName(collName);
      const stableKey = `named:${collName}`;
      const overviews = getOverviews(appIds);
      try {
        const existing = collectionStore.userCollections.find(
          (c) => c.displayName === collectionName
        );
        if (existing) {
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
          registerCollection(existing.id, stableKey);
          dirty = true;
          logInfo(`Appended ${appIds.length} apps to RomM collection "${collectionName}"`);
        } else {
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
          registerCollection(collection.id, stableKey);
          dirty = true;
          logInfo(`Created RomM collection "${collectionName}" with ${appIds.length} apps`);
        }
      } catch (e) {
        logError(`Failed to append to RomM collection "${collectionName}": ${e}`);
      }
    }
    if (dirty) await persistRegistry();
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
    await loadRegistry();

    const stableKey = `platform:${platformName}`;
    let deleted = false;
    let dirty = false;

    // Primary: delete by registry ID
    for (const [steamId, key] of Object.entries(_registry)) {
      if (key === stableKey) {
        const coll = collectionStore.GetCollection(steamId);
        if (coll) {
          logInfo(`Deleting collection by registry: "${coll.displayName}" (id=${steamId})`);
          await coll.Delete();
          deleted = true;
        }
        unregisterCollection(steamId);
        dirty = true;
      }
    }

    // Fallback: name-matching for legacy collections created before registry
    const currentName = await platformCollectionName(platformName);
    const hostname = await getHostname();
    const legacyScoped = `RomM: ${platformName} (${hostname})`;
    const legacyBare = `RomM: ${platformName}`;

    for (const name of [currentName, legacyScoped, legacyBare]) {
      const coll = collectionStore.userCollections.find(
        (c) => c.displayName === name
      );
      if (coll) {
        logInfo(`Deleting legacy collection "${name}" (id=${coll.id})`);
        await coll.Delete();
        unregisterCollection(coll.id);
        dirty = true;
        deleted = true;
      }
    }

    if (dirty) await persistRegistry();
    if (!deleted) {
      logInfo(`Collection for "${platformName}" not found, nothing to clear`);
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
    await loadRegistry();

    const deletedIds = new Set<string>();

    // Primary: delete every collection tracked in the registry
    for (const [steamId, key] of Object.entries(_registry)) {
      const coll = collectionStore.GetCollection(steamId);
      if (coll) {
        logInfo(`Deleting registered collection "${coll.displayName}" (key=${key}, id=${steamId})`);
        await coll.Delete();
        deletedIds.add(steamId);
      }
      unregisterCollection(steamId);
    }

    // Fallback: sweep by name patterns for legacy collections
    const hostname = await getHostname();
    const hostSuffix = ` (${hostname})`;
    const prefixes = new Set([_prefix]);
    prefixes.add("RomM: ");
    if (_prefix) prefixes.add("");

    const legacyCollections = collectionStore.userCollections.filter((c) => {
      if (deletedIds.has(c.id)) return false; // already handled
      for (const pfx of prefixes) {
        if (!c.displayName.startsWith(pfx)) continue;
        if (c.displayName.endsWith(hostSuffix)) return true;
        if (!/\s\([^)]+\)$/.test(c.displayName)) return true;
      }
      return false;
    });

    if (legacyCollections.length > 0) {
      logInfo(`Deleting ${legacyCollections.length} legacy collections by name matching`);
      for (const c of legacyCollections) {
        logInfo(`Deleting legacy collection "${c.displayName}" (id=${c.id})`);
        await c.Delete();
      }
    }

    await persistRegistry();
    logInfo(`Cleared all RomM collections (${deletedIds.size} by registry, ${legacyCollections.length} by name)`);
  } catch (e) {
    logError(`Failed to clear collections: ${e}`);
  }
}
