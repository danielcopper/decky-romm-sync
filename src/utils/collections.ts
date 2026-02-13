/**
 * Steam collection management for RomM platforms.
 * Uses Steam's internal collectionStore API if available.
 */

interface SteamCollection {
  AsDragDropCollection: () => unknown;
  bIsDynamic: boolean;
  displayName: string;
  id: string;
  visibleApps: Set<number>;
}

declare const collectionStore: {
  userCollections: SteamCollection[];
  CreateCollection: (name: string, apps?: number[]) => void;
  SetAppsInCollection: (id: string, apps: number[]) => void;
  GetCollection: (id: string) => SteamCollection | undefined;
} | undefined;

export function createOrUpdateCollections(
  platformAppIds: Record<string, number[]>
): void {
  try {
    if (typeof collectionStore === "undefined") {
      console.warn("[RomM] collectionStore not available, skipping collections");
      return;
    }

    console.log("[RomM] collectionStore available, userCollections count:", collectionStore.userCollections.length);
    console.log("[RomM] Creating/updating collections for platforms:", Object.keys(platformAppIds));

    for (const [platformName, appIds] of Object.entries(platformAppIds)) {
      const collectionName = `RomM: ${platformName}`;

      const existing = collectionStore.userCollections.find(
        (c) => c.displayName === collectionName
      );

      if (existing) {
        console.log(`[RomM] Updating existing collection "${collectionName}" (id=${existing.id}) with ${appIds.length} apps`);
        try {
          collectionStore.SetAppsInCollection(existing.id, appIds);
          console.log(`[RomM] Successfully updated collection "${collectionName}"`);
        } catch (setErr) {
          console.error(`[RomM] SetAppsInCollection failed for "${collectionName}":`, setErr);
        }
      } else {
        console.log(`[RomM] Creating new collection "${collectionName}" with ${appIds.length} apps:`, appIds);
        try {
          collectionStore.CreateCollection(collectionName, appIds);
          console.log(`[RomM] Successfully created collection "${collectionName}"`);
        } catch (createErr) {
          console.error(`[RomM] CreateCollection failed for "${collectionName}":`, createErr);
        }
      }
    }
  } catch (e) {
    console.error("[RomM] Failed to update collections:", e);
  }
}

export function clearPlatformCollection(platformName: string): void {
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
      console.log(`[RomM] Clearing collection "${collectionName}" (id=${existing.id})`);
      collectionStore.SetAppsInCollection(existing.id, []);
    } else {
      console.log(`[RomM] Collection "${collectionName}" not found, nothing to clear`);
    }
  } catch (e) {
    console.error("[RomM] Failed to clear platform collection:", e);
  }
}

export function clearAllRomMCollections(): void {
  try {
    if (typeof collectionStore === "undefined") {
      console.warn("[RomM] collectionStore not available, cannot clear collections");
      return;
    }
    const rommCollections = collectionStore.userCollections.filter(
      (c) => c.displayName.startsWith("RomM: ")
    );
    console.log(`[RomM] Clearing ${rommCollections.length} RomM collections`);
    for (const c of rommCollections) {
      console.log(`[RomM] Clearing collection "${c.displayName}" (id=${c.id})`);
      collectionStore.SetAppsInCollection(c.id, []);
    }
  } catch (e) {
    console.error("[RomM] Failed to clear collections:", e);
  }
}
