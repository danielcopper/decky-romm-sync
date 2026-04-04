/**
 * Steam collection management for RomM platforms.
 * Uses Steam's internal collectionStore API.
 *
 * Collection names are machine-scoped to prevent cross-device conflicts
 * when Steam Cloud syncs collections: "RomM: Platform (hostname)"
 */

import { logInfo, logWarn, logError, getCollectionRegistry, saveCollectionRegistry } from "../api/backend";

let _hostname = "";

// ── System collection protection ──────────────────────────────
// These are Steam's built-in collection IDs that must NEVER be deleted.
// Attempting to delete them corrupts Steam's internal collection state
// and causes persistent GetAppCountWithToolsFilter errors.
const SYSTEM_COLLECTION_IDS = new Set([
  "favorite", "hidden", "uncategorized",
  "type-games", "type-software", "type-dlc", "type-music", "type-tools",
  "desktop", "recent-activity",
]);

function isSystemCollection(coll: any): boolean {
  return SYSTEM_COLLECTION_IDS.has(coll.id);
}

/** Check if a collection is safe to delete (not a system collection). */
export function isCollectionSafeToDelete(coll: any): boolean {
  return !isSystemCollection(coll);
}

// ── Safety cap ────────────────────────────────────────────────
// Maximum number of NEW collections a single operation can create.
// If this limit is hit, the operation stops creating further collections
// and logs an error. This prevents runaway duplication from ever crashing
// Steam's Library again.
const MAX_NEW_COLLECTIONS_PER_OP = 50;
let _newCollectionsThisOp = 0;

function resetCreationCounter(): void {
  _newCollectionsThisOp = 0;
}

function canCreateNewCollection(name: string): boolean {
  if (_newCollectionsThisOp >= MAX_NEW_COLLECTIONS_PER_OP) {
    logError(`SAFETY CAP: Refusing to create collection "${name}" — already created ${_newCollectionsThisOp} new collections this operation (limit ${MAX_NEW_COLLECTIONS_PER_OP}). This likely indicates a find/match bug.`);
    return false;
  }
  _newCollectionsThisOp++;
  return true;
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

async function platformCollectionName(platformName: string): Promise<string> {
  const hostname = await getHostname();
  return `RomM: ${platformName} (${hostname})`;
}

async function namedCollectionName(collName: string): Promise<string> {
  const hostname = await getHostname();
  return `RomM: [${collName}] (${hostname})`;
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

/**
 * Find an existing Steam collection by registry ID first, then by name variants.
 * This prevents creating duplicates when naming settings change.
 * If found under an old name, renames it to the current name.
 */
async function findExistingCollection(
  stableKey: string,
  currentName: string,
  legacyNames: string[],
): Promise<any | null> {
  // 1. Registry lookup — most reliable
  for (const [steamId, key] of Object.entries(_registry)) {
    if (key === stableKey) {
      const coll = collectionStore.GetCollection(steamId);
      if (coll) {
        // Rename if name drifted
        if (coll.displayName !== currentName) {
          logInfo(`Renaming collection "${coll.displayName}" → "${currentName}" (matched by registry)`);
          coll.displayName = currentName;
        }
        return coll;
      }
      // Registry pointed to a deleted collection — clean up
      unregisterCollection(steamId);
    }
  }

  // 2. Exact current name match
  const byCurrentName = collectionStore.userCollections.find(
    (c: any) => c.displayName === currentName
  );
  if (byCurrentName) return byCurrentName;

  // 3. Legacy name patterns (old "RomM: X (hostname)" format, bare name, etc.)
  for (const name of legacyNames) {
    if (name === currentName) continue; // already checked
    const coll = collectionStore.userCollections.find(
      (c: any) => c.displayName === name
    );
    if (coll) {
      logInfo(`Found collection under legacy name "${name}", renaming to "${currentName}"`);
      coll.displayName = currentName;
      return coll;
    }
  }

  return null;
}

/** Build all possible legacy name variants for a platform collection */
async function platformLegacyNames(platformName: string): Promise<string[]> {
  const hostname = await getHostname();
  return [
    `RomM: ${platformName} (${hostname})`,
    `RomM: ${platformName}`,
    platformName,
  ];
}

/** Build all possible legacy name variants for a named (RomM) collection */
async function namedLegacyNames(collName: string): Promise<string[]> {
  const hostname = await getHostname();
  return [
    `RomM: [${collName}] (${hostname})`,
    `RomM: [${collName}]`,
    `[${collName}]`,
  ];
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
    resetCreationCounter();

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
      const legacyNames = await platformLegacyNames(platformName);

      try {
        const existing = await findExistingCollection(stableKey, collectionName, legacyNames);

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
          if (!canCreateNewCollection(collectionName)) continue;
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
    resetCreationCounter();

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
      const legacyNames = await namedLegacyNames(collName);

      try {
        const existing = await findExistingCollection(stableKey, collectionName, legacyNames);

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
          if (!canCreateNewCollection(collectionName)) continue;
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
    resetCreationCounter();
    let dirty = false;
    for (const [platformName, appIds] of Object.entries(platformAppIds)) {
      if (appIds.length === 0) continue;
      const collectionName = await platformCollectionName(platformName);
      const stableKey = `platform:${platformName}`;
      const overviews = getOverviews(appIds);
      const legacyNames = await platformLegacyNames(platformName);
      try {
        const existing = await findExistingCollection(stableKey, collectionName, legacyNames);
        if (existing) {
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
          registerCollection(existing.id, stableKey);
          dirty = true;
          logInfo(`Appended ${appIds.length} apps to platform collection "${collectionName}"`);
        } else {
          if (!canCreateNewCollection(collectionName)) continue;
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
    resetCreationCounter();
    let dirty = false;
    for (const [collName, appIds] of Object.entries(collectionAppIds)) {
      if (appIds.length === 0) continue;
      const collectionName = await namedCollectionName(collName);
      const stableKey = `named:${collName}`;
      const overviews = getOverviews(appIds);
      const legacyNames = await namedLegacyNames(collName);
      try {
        const existing = await findExistingCollection(stableKey, collectionName, legacyNames);
        if (existing) {
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
          registerCollection(existing.id, stableKey);
          dirty = true;
          logInfo(`Appended ${appIds.length} apps to RomM collection "${collectionName}"`);
        } else {
          if (!canCreateNewCollection(collectionName)) continue;
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

/**
 * Remove specific app IDs from all user collections (Favorites, Hidden, etc.).
 * Must be called BEFORE the shortcuts themselves are removed, so Steam's
 * collection renderer never sees references to deleted apps.
 */
export function removeAppsFromAllCollections(appIds: number[]): void {
  try {
    if (typeof collectionStore === "undefined" || !appIds.length) return;
    const overviews = getOverviews(appIds);
    if (!overviews.length) return;
    // Iterate all user collections + built-in ones (Favorites, Hidden)
    const allCollections = collectionStore.userCollections;
    let cleaned = 0;
    for (const coll of allCollections) {
      try {
        const matching = overviews.filter((o) => coll.apps?.has(o.appid));
        if (matching.length > 0) {
          const dd = coll.AsDragDropCollection?.();
          if (dd) {
            dd.RemoveApps(matching);
            logInfo(`Removed ${matching.length} apps from collection "${coll.displayName}"`);
            cleaned += matching.length;
          } else {
            logWarn(`Cannot clean "${coll.displayName}": AsDragDropCollection returned null`);
          }
        }
      } catch (e) {
        logWarn(`Failed to clean collection "${coll.displayName}": ${e}`);
      }
    }
    if (cleaned > 0) {
      logInfo(`Cleaned ${cleaned} app references from collections before removal`);
    }
  } catch (e) {
    logError(`removeAppsFromAllCollections failed: ${e}`);
  }
}

/**
 * Safely drain a collection's apps and then delete it.
 * Draining first prevents Steam from crashing if it tries to render
 * a collection whose referenced apps have already been removed.
 */
async function drainAndDelete(coll: any, label: string): Promise<void> {
  if (isSystemCollection(coll)) {
    logWarn(`REFUSED to delete system collection "${label}" (id=${coll.id}) — this would corrupt Steam's Library`);
    return;
  }
  try {
    const apps = coll.allApps;
    if (apps && apps.length > 0) {
      const dd = coll.AsDragDropCollection?.();
      if (dd) {
        logInfo(`Draining ${apps.length} apps from "${label}" before deletion`);
        dd.RemoveApps(apps);
      } else {
        logWarn(`Cannot drain "${label}": AsDragDropCollection returned null (built-in collection?)`);
      }
    }
  } catch (e) {
    logWarn(`Failed to drain apps from "${label}": ${e}`);
  }
  await coll.Delete();
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
          await drainAndDelete(coll, coll.displayName);
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
        (c: any) => c.displayName === name
      );
      if (coll) {
        logInfo(`Deleting legacy collection "${name}" (id=${coll.id})`);
        await drainAndDelete(coll, name);
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
      if (SYSTEM_COLLECTION_IDS.has(steamId)) {
        logWarn(`Registry contains system collection id="${steamId}" (key=${key}) — removing from registry without deleting`);
        unregisterCollection(steamId);
        continue;
      }
      const coll = collectionStore.GetCollection(steamId);
      if (coll) {
        logInfo(`Deleting registered collection "${coll.displayName}" (key=${key}, id=${steamId})`);
        await drainAndDelete(coll, coll.displayName);
        deletedIds.add(steamId);
      }
      unregisterCollection(steamId);
    }

    // Fallback: sweep by name patterns for legacy collections.
    // ONLY match the canonical "RomM: " prefix — never use broad patterns
    // that could match system or unrelated user collections.
    const isRomMCollection = (name: string): boolean => {
      return name.startsWith("RomM: ");
    };

    // Second pass over userCollections to catch legacy collections not in the registry
    const allRomM = collectionStore.userCollections.filter((c: any) => {
      if (deletedIds.has(c.id)) return false;
      if (isSystemCollection(c)) return false;
      return isRomMCollection(c.displayName);
    });

    if (allRomM.length > 0) {
      logInfo(`Deleting ${allRomM.length} collections by name matching`);
      for (const c of allRomM) {
        logInfo(`Deleting collection "${c.displayName}" (id=${c.id})`);
        await drainAndDelete(c, c.displayName);
      }
    }

    await persistRegistry();
    logInfo(`Cleared all RomM collections (${deletedIds.size} by registry, ${allRomM.length} by name)`);
  } catch (e) {
    logError(`Failed to clear collections: ${e}`);
  }
}

/**
 * Emergency startup cleanup: remove orphaned user collections whose apps
 * ALL reference non-existent shortcuts. These orphans cause Steam's Library
 * to crash with GetAppCountWithToolsFilter errors.
 *
 * This runs once at startup via collectionStore API (in-memory), which is
 * the only reliable cleanup path — file-level edits get overwritten by
 * Steam Cloud sync.
 */
export async function cleanupOrphanedCollections(): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") return;
    if (typeof appStore === "undefined") return;

    // Phase 1: Clean orphaned app IDs from the hidden collection.
    // Non-existent app IDs in hidden cause GetAppCountWithToolsFilter crashes
    // when any Decky plugin triggers a Steam route re-render.
    const hiddenColl = collectionStore.userCollections.find(
      (c: any) => c.id === "hidden"
    );
    if (hiddenColl) {
      const hiddenApps = hiddenColl.allApps ?? [];
      const orphanedHiddenApps = hiddenApps.filter((app: any) => {
        const overview = appStore.GetAppOverviewByAppID(app.appid);
        return !overview || !overview.display_name;
      });
      if (orphanedHiddenApps.length > 0) {
        logInfo(`Orphan cleanup: removing ${orphanedHiddenApps.length} orphaned apps from hidden collection`);
        const dd = hiddenColl.AsDragDropCollection?.();
        if (dd) {
          dd.RemoveApps(orphanedHiddenApps);
          logInfo(`Orphan cleanup: cleaned hidden collection`);
        }
      }
    }

    // Phase 2: Delete fully-orphaned user collections (all apps missing)
    const orphaned: any[] = [];
    for (const coll of collectionStore.userCollections) {
      if (isSystemCollection(coll)) continue;

      // Only clean uc-* collections (user-created, not RomM-prefixed)
      // RomM collections are handled by clearAllRomMCollections
      if (!coll.id.startsWith("uc-") && coll.displayName.startsWith("RomM: ")) continue;

      // Check if ALL apps in this collection are non-existent
      const apps = coll.allApps;
      if (!apps || apps.length === 0) {
        orphaned.push(coll);
        continue;
      }

      let allMissing = true;
      for (const app of apps) {
        const overview = appStore.GetAppOverviewByAppID(app.appid);
        if (overview && overview.display_name) {
          allMissing = false;
          break;
        }
      }
      if (allMissing) {
        orphaned.push(coll);
      }
    }

    if (orphaned.length === 0) {
      logInfo("Orphan cleanup: no orphaned collections found");
      return;
    }

    logInfo(`Orphan cleanup: removing ${orphaned.length} orphaned collections`);
    for (const coll of orphaned) {
      logInfo(`Orphan cleanup: deleting "${coll.displayName}" (id=${coll.id}, apps=${coll.allApps?.length ?? 0})`);
      await drainAndDelete(coll, coll.displayName);
    }
    logInfo(`Orphan cleanup: done, removed ${orphaned.length} collections`);
  } catch (e) {
    logError(`Orphan cleanup failed: ${e}`);
  }
}
