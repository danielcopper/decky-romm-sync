/**
 * Steam collection management for RomM platforms.
 * Uses Steam's internal collectionStore API.
 *
 * Collection names are machine-scoped to prevent cross-device conflicts
 * when Steam Cloud syncs collections: "RomM: Platform (hostname)"
 */

let _hostname = "";

async function getHostname(): Promise<string> {
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
      console.warn("[RomM] collectionStore not available, skipping collections");
      return;
    }

    const hostname = await getHostname();
    console.log("[RomM] Creating/updating collections for platforms:", Object.keys(platformAppIds), `(hostname: ${hostname})`);

    const entries = Object.entries(platformAppIds);
    let idx = 0;
    for (const [platformName, appIds] of entries) {
      idx++;
      onProgress?.(idx, entries.length, platformName);
      const collectionName = `RomM: ${platformName} (${hostname})`;
      const overviews = getOverviews(appIds);

      try {
        const existing = collectionStore.userCollections.find(
          (c) => c.displayName === collectionName
        );

        if (existing) {
          console.log(`[RomM] Updating collection "${collectionName}" with ${appIds.length} apps`);
          const existingApps = existing.allApps;
          if (existingApps.length > 0) {
            existing.AsDragDropCollection().RemoveApps(existingApps);
          }
          existing.AsDragDropCollection().AddApps(overviews);
          await existing.Save();
        } else {
          console.log(`[RomM] Creating collection "${collectionName}" with ${appIds.length} apps`);
          const collection = collectionStore.NewUnsavedCollection(collectionName, undefined, []);
          collection.AsDragDropCollection().AddApps(overviews);
          await collection.Save();
        }
        console.log(`[RomM] Successfully saved collection "${collectionName}"`);
      } catch (colErr) {
        console.error(`[RomM] Failed to save collection "${collectionName}":`, colErr);
      }
    }
  } catch (e) {
    console.error("[RomM] Failed to update collections:", e);
  }
}

export async function clearPlatformCollection(platformName: string): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") {
      console.warn("[RomM] collectionStore not available, cannot clear platform collection");
      return;
    }
    const hostname = await getHostname();
    const scopedName = `RomM: ${platformName} (${hostname})`;
    const legacyName = `RomM: ${platformName}`;

    // Delete the machine-scoped collection
    const scoped = collectionStore.userCollections.find(
      (c) => c.displayName === scopedName
    );
    if (scoped) {
      console.log(`[RomM] Deleting collection "${scopedName}" (id=${scoped.id})`);
      await scoped.Delete();
    }

    // Also clean up legacy collection (without hostname suffix) if it exists
    const legacy = collectionStore.userCollections.find(
      (c) => c.displayName === legacyName
    );
    if (legacy) {
      console.log(`[RomM] Deleting legacy collection "${legacyName}" (id=${legacy.id})`);
      await legacy.Delete();
    }

    if (!scoped && !legacy) {
      console.log(`[RomM] Collection "${scopedName}" not found, nothing to clear`);
    }
  } catch (e) {
    console.error("[RomM] Failed to clear platform collection:", e);
  }
}

export async function clearAllRomMCollections(): Promise<void> {
  try {
    if (typeof collectionStore === "undefined") {
      console.warn("[RomM] collectionStore not available, cannot clear collections");
      return;
    }
    const hostname = await getHostname();
    const suffix = ` (${hostname})`;

    // Match collections belonging to this machine OR legacy ones without any hostname suffix.
    // Legacy collections match "RomM: ..." but do NOT have a parenthesized suffix.
    // This avoids deleting collections from other devices like "RomM: N64 (othermachine)".
    const rommCollections = collectionStore.userCollections.filter((c) => {
      if (!c.displayName.startsWith("RomM: ")) return false;
      // This machine's scoped collections
      if (c.displayName.endsWith(suffix)) return true;
      // Legacy collections: start with "RomM: " but have no " (...)" suffix at all
      if (!/\s\([^)]+\)$/.test(c.displayName)) return true;
      return false;
    });

    console.log(`[RomM] Deleting ${rommCollections.length} RomM collections (hostname: ${hostname})`);
    for (const c of rommCollections) {
      console.log(`[RomM] Deleting collection "${c.displayName}" (id=${c.id})`);
      await c.Delete();
    }
  } catch (e) {
    console.error("[RomM] Failed to clear collections:", e);
  }
}
