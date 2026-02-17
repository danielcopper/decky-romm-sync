import { afterPatch, replacePatch, callOriginal } from "@decky/ui";
import type { Patch } from "@decky/ui";
import type { RomMetadata } from "../types";

// Module-level state
let metadataCache: Record<string, RomMetadata> = {};
let appIdToRomId: Record<number, number> = {};
let registeredAppIds: Set<number> = new Set();
let patches: Patch[] = [];

// Genre string → Steam StoreCategory ID mapping
const GENRE_CATEGORY_MAP: Record<string, number> = {
  "Action": 21,
  "Adventure": 25,
  "RPG": 21,
  "Role-playing (RPG)": 21,
  "Role-playing": 21,
  "Strategy": 2,
  "Simulation": 28,
  "Sport": 18,
  "Sports": 18,
  "Racing": 9,
  "Puzzle": 4,
};

// Game mode string → Steam StoreCategory ID mapping
const MODE_CATEGORY_MAP: Record<string, number> = {
  "Single player": 2,
  "Multiplayer": 1,
  "Co-operative": 9,
  "Split screen": 24,
  "MMO": 20,
};

/**
 * Wrap MobX state mutations so Steam's observable stores allow changes.
 */
function stateTransaction<T>(block: () => T): T {
  const globals = (window as any).__mobxGlobals;
  if (!globals) return block();
  const prev = globals.allowStateChanges;
  globals.allowStateChanges = true;
  try {
    return block();
  } finally {
    globals.allowStateChanges = prev;
  }
}

/**
 * Look up cached metadata for a given Steam app ID.
 */
function getMetadataForAppId(appId: number): RomMetadata | null {
  const romId = appIdToRomId[appId];
  if (romId == null) return null;
  return metadataCache[String(romId)] || null;
}

/**
 * Build the set of Steam category IDs from a ROM's genres and game modes.
 * Always includes category 28 (full controller support) for our games.
 */
function buildCategorySet(metadata: RomMetadata): Set<number> {
  const categories = new Set<number>();
  // Full controller support for all RomM games
  categories.add(28);

  for (const genre of metadata.genres) {
    const cat = GENRE_CATEGORY_MAP[genre];
    if (cat != null) categories.add(cat);
  }
  for (const mode of metadata.game_modes) {
    const cat = MODE_CATEGORY_MAP[mode];
    if (cat != null) categories.add(cat);
  }
  return categories;
}

/**
 * Apply direct property mutations to a SteamAppOverview for a RomM app.
 */
function applyDirectMutations(appId: number, metadata: RomMetadata) {
  const overview = appStore.GetAppOverviewByAppID(appId);
  if (!overview) return;

  stateTransaction(() => {
    // Full controller support
    overview.controller_support = 2;

    // Metacritic score from average_rating (0-100 scale, rounded)
    if (metadata.average_rating != null) {
      overview.metacritic_score = Math.round(metadata.average_rating);
    }

    // Add store categories
    if (overview.m_setStoreCategories) {
      const cats = buildCategorySet(metadata);
      for (const cat of cats) {
        overview.m_setStoreCategories.add(cat);
      }
    }
  });
}

/**
 * Register all store patches for metadata display.
 * Call on plugin load after fetching the metadata cache and app ID map.
 */
export function registerMetadataPatches(
  cache: Record<string, RomMetadata>,
  appIdMap: Record<string, number>,
) {
  metadataCache = cache;

  // Build reverse lookup: app_id → rom_id
  appIdToRomId = {};
  for (const [appIdStr, romId] of Object.entries(appIdMap)) {
    appIdToRomId[Number(appIdStr)] = romId;
  }

  // Build set of registered app IDs
  registeredAppIds = new Set(Object.keys(appIdToRomId).map(Number));

  // Apply direct mutations for all known apps
  for (const appId of registeredAppIds) {
    const meta = getMetadataForAppId(appId);
    if (meta) applyDirectMutations(appId, meta);
  }

  // --- Patch 1: GetDescriptions ---
  const detailsProto = Object.getPrototypeOf(appDetailsStore);
  try {
    patches.push(
      replacePatch(detailsProto, "GetDescriptions", function (this: any, args: any[]) {
        const appId = args[0];
        if (!registeredAppIds.has(appId)) return callOriginal;

        const metadata = getMetadataForAppId(appId);
        if (!metadata?.summary) return callOriginal;

        const appData = appDetailsStore.GetAppData(appId);
        if (!appData) return callOriginal;

        return stateTransaction(() => {
          appData.descriptionsData = {
            strFullDescription: metadata.summary,
            strSnippet: metadata.summary,
          };
          appDetailsCache.SetCachedDataForApp(appId, "descriptions", 1, appData.descriptionsData);
          return appData.descriptionsData;
        });
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch GetDescriptions:", e);
  }

  // --- Patch 2: GetAssociations ---
  try {
    patches.push(
      replacePatch(detailsProto, "GetAssociations", function (this: any, args: any[]) {
        const appId = args[0];
        if (!registeredAppIds.has(appId)) return callOriginal;

        const metadata = getMetadataForAppId(appId);
        if (!metadata?.companies?.length) return callOriginal;

        const appData = appDetailsStore.GetAppData(appId);
        if (!appData) return callOriginal;

        if (!appData.associationData) {
          stateTransaction(() => {
            appData.associationData = {
              rgDevelopers: metadata.companies.map((c) => ({ strName: c, strURL: "" })),
              rgPublishers: [],
              rgFranchises: [],
            };
            appDetailsCache.SetCachedDataForApp(appId, "associations", 1, appData.associationData);
          });
        }

        return callOriginal;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch GetAssociations:", e);
  }

  // Patches on appStore.allApps[0].__proto__ (SteamAppOverview prototype)
  // Retry up to 10 times (5 seconds) if allApps is empty at startup
  const tryRegisterAppProtoPatches = (retriesLeft: number) => {
    if (!appStore.allApps?.length) {
      if (retriesLeft > 0) {
        setTimeout(() => tryRegisterAppProtoPatches(retriesLeft - 1), 500);
      } else {
        console.warn("[RomM] appStore.allApps still empty after retries, skipping prototype patches");
      }
      return;
    }
    registerAppProtoPatches(Object.getPrototypeOf(appStore.allApps[0]));
  };
  tryRegisterAppProtoPatches(10);

  console.log(`[RomM] Registered store patches for ${registeredAppIds.size} apps, awaiting app prototype...`);
}

function registerAppProtoPatches(appProto: any) {

  // --- Patch 3: GetCanonicalReleaseDate ---
  try {
    patches.push(
      afterPatch(appProto, "GetCanonicalReleaseDate", function (this: SteamAppOverview, _args: any[], ret: any) {
        if (!registeredAppIds.has(this.appid)) return ret;

        const metadata = getMetadataForAppId(this.appid);
        if (metadata?.first_release_date) {
          return metadata.first_release_date;
        }
        return ret;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch GetCanonicalReleaseDate:", e);
  }

  // --- Patch 4: BHasStoreCategory ---
  try {
    patches.push(
      replacePatch(appProto, "BHasStoreCategory", function (this: SteamAppOverview, args: any[]) {
        if (!registeredAppIds.has(this.appid)) return callOriginal;

        const metadata = getMetadataForAppId(this.appid);
        if (!metadata) return callOriginal;

        const categories = buildCategorySet(metadata);
        if (categories.has(args[0])) return true;

        return callOriginal;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch BHasStoreCategory:", e);
  }

  // --- Patch 5: BIsModOrShortcut ---
  try {
    patches.push(
      afterPatch(appProto, "BIsModOrShortcut", function (this: SteamAppOverview, _args: any[], ret: any) {
        if (ret === true && registeredAppIds.has(this.appid)) {
          return false;
        }
        return ret;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch BIsModOrShortcut:", e);
  }

  console.log(`[RomM] Registered metadata patches for ${registeredAppIds.size} apps`);
}

/**
 * Unregister all store patches. Call on plugin dismount.
 */
export function unregisterMetadataPatches() {
  for (const patch of patches) {
    try {
      patch.unpatch();
    } catch (e) {
      console.error("[RomM] Failed to unpatch:", e);
    }
  }
  patches = [];
  metadataCache = {};
  appIdToRomId = {};
  registeredAppIds = new Set();
  console.log("[RomM] Unregistered metadata patches");
}

/**
 * Update metadata cache for a single app and apply direct property mutations.
 * Called from GameDetailPanel after fetching fresh metadata.
 */
export function updateMetadataForApp(appId: number, romId: number, metadata: RomMetadata) {
  metadataCache[String(romId)] = metadata;
  appIdToRomId[appId] = romId;
  registeredAppIds.add(appId);
  applyDirectMutations(appId, metadata);
}

/**
 * Update the set of known RomM app IDs (e.g. after sync).
 */
export function setRegisteredAppIds(appIds: number[]) {
  registeredAppIds = new Set(appIds);
}
