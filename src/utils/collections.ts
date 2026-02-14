/**
 * Steam collection management for RomM platforms.
 * Uses Steam's internal collectionStore API.
 */

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

    console.log("[RomM] Creating/updating collections for platforms:", Object.keys(platformAppIds));

    const entries = Object.entries(platformAppIds);
    let idx = 0;
    for (const [platformName, appIds] of entries) {
      idx++;
      onProgress?.(idx, entries.length, platformName);
      const collectionName = `RomM: ${platformName}`;
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
    const collectionName = `RomM: ${platformName}`;
    const existing = collectionStore.userCollections.find(
      (c) => c.displayName === collectionName
    );
    if (existing) {
      console.log(`[RomM] Deleting collection "${collectionName}" (id=${existing.id})`);
      await existing.Delete();
    } else {
      console.log(`[RomM] Collection "${collectionName}" not found, nothing to clear`);
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
    const rommCollections = collectionStore.userCollections.filter(
      (c) => c.displayName.startsWith("RomM: ")
    );
    console.log(`[RomM] Deleting ${rommCollections.length} RomM collections`);
    for (const c of rommCollections) {
      console.log(`[RomM] Deleting collection "${c.displayName}" (id=${c.id})`);
      await c.Delete();
    }
  } catch (e) {
    console.error("[RomM] Failed to clear collections:", e);
  }
}
