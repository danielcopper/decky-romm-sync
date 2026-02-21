import { afterPatch, replacePatch, callOriginal } from "@decky/ui";
import type { Patch } from "@decky/ui";
import type { RomMetadata } from "../types";

// Module-level state
let metadataCache: Record<string, RomMetadata> = {};
let appIdToRomId: Record<number, number> = {};
let registeredAppIds: Set<number> = new Set();
let patches: Patch[] = [];

// BIsModOrShortcut bypass counters (MetaDeck pattern).
// Default state (both 0): BIsModOrShortcut returns false for our apps (metadata shows).
// bypassCounter > 0 or == -1: temporarily returns true during launch (preserves shortcut launch path).
// bypassBypass > 0: forces false during game detail page rendering (ensures metadata sections render).
let bypassCounter = 0;
let bypassBypass = 0;

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

  // --- BHasRecentlyLaunched (on appDetailsStore, not app prototype) ---
  // Fires during recent-games checks — temporarily restore shortcut identity.
  try {
    patches.push(
      afterPatch(detailsProto, "BHasRecentlyLaunched", function (this: any, _args: any[], ret: any) {
        bypassCounter = 4;
        return ret;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch BHasRecentlyLaunched:", e);
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

  // --- Patch 5: BIsModOrShortcut (bypass counter pattern from MetaDeck) ---
  // Returns false (metadata renders) by default. Temporarily returns true
  // during launch sequence so Steam uses the shortcut launch path.
  try {
    patches.push(
      afterPatch(appProto, "BIsModOrShortcut", function (this: SteamAppOverview, _args: any[], ret: any) {
        if (ret !== true || !registeredAppIds.has(this.appid)) return ret;

        // Game detail page render — force false for N calls
        if (bypassBypass > 0) {
          bypassBypass--;
          return false;
        }

        // Library home — always show as real game
        try {
          const pathname = (window as any).Router?.WindowStore
            ?.GamepadUIMainWindowInstance?.m_history?.location?.pathname;
          if (pathname === "/library/home") return false;
        } catch { /* ignore */ }

        // Launch sequence — temporarily return true (is a shortcut)
        if (bypassCounter > 0) {
          bypassCounter--;
        }
        return bypassCounter === -1 || bypassCounter > 0;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch BIsModOrShortcut:", e);
  }

  // --- Patch 6+7: GetGameID (set counter=-1 before, reset to 0 after) ---
  try {
    patches.push(
      replacePatch(appProto, "GetGameID", function (this: SteamAppOverview, _args: any[]) {
        if (registeredAppIds.has(this.appid)) bypassCounter = -1;
        return callOriginal;
      }),
    );
    patches.push(
      afterPatch(appProto, "GetGameID", function (this: SteamAppOverview, _args: any[], ret: any) {
        if (registeredAppIds.has(this.appid)) bypassCounter = 0;
        return ret;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch GetGameID:", e);
  }

  // --- Patch 8+9: GetPrimaryAppID (same counter pattern) ---
  try {
    patches.push(
      replacePatch(appProto, "GetPrimaryAppID", function (this: SteamAppOverview, _args: any[]) {
        if (registeredAppIds.has(this.appid)) bypassCounter = -1;
        return callOriginal;
      }),
    );
    patches.push(
      afterPatch(appProto, "GetPrimaryAppID", function (this: SteamAppOverview, _args: any[], ret: any) {
        if (registeredAppIds.has(this.appid)) bypassCounter = 0;
        return ret;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch GetPrimaryAppID:", e);
  }

  // --- Patch 10: GetPerClientData (protect shortcut identity) ---
  try {
    patches.push(
      afterPatch(appProto, "GetPerClientData", function (this: SteamAppOverview, _args: any[], ret: any) {
        if (registeredAppIds.has(this.appid)) {
          bypassCounter = 4;
        }
        return ret;
      }),
    );
  } catch (e) {
    console.error("[RomM] Failed to patch GetPerClientData:", e);
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
 * Write tracked playtime to Steam's native UI fields.
 * Sets minutes_playtime_forever and rt_last_time_played so Steam shows
 * actual play time instead of "Never Played" for RomM shortcuts.
 */
export function updatePlaytimeDisplay(appId: number, totalSeconds: number) {
  const overview = appStore.GetAppOverviewByAppID(appId);
  if (!overview) return;

  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes <= 0) return;

  stateTransaction(() => {
    overview.minutes_playtime_forever = totalMinutes;
    // Set rt_last_time_played to now (Unix epoch seconds) so Steam
    // shows "Last played: today" instead of "Never Played"
    if (!overview.rt_last_time_played) {
      overview.rt_last_time_played = Math.floor(Date.now() / 1000);
    }
  });
}

/**
 * Apply playtime data for all known apps from the bulk playtime map.
 * Called at plugin load and after sync_complete.
 */
export function applyAllPlaytime(
  playtimeMap: Record<string, { total_seconds: number }>,
  appIdMap: Record<string, number>,
) {
  // Build rom_id -> app_id reverse lookup
  const romIdToAppId: Record<string, number> = {};
  for (const [appIdStr, romId] of Object.entries(appIdMap)) {
    romIdToAppId[String(romId)] = Number(appIdStr);
  }

  for (const [romIdStr, entry] of Object.entries(playtimeMap)) {
    const appId = romIdToAppId[romIdStr];
    if (appId && entry.total_seconds > 0) {
      updatePlaytimeDisplay(appId, entry.total_seconds);
    }
  }
}

/**
 * Update metadata cache for a single app and apply direct property mutations.
 * Called after fetching fresh metadata for a ROM.
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

/**
 * Set the bypassBypass counter for game detail page rendering.
 * Call when navigating to /library/app/:appid so BIsModOrShortcut
 * returns false during the render pass (enabling metadata sections).
 */
export function setBypassBypass(count: number) {
  bypassBypass = count;
}

/**
 * Prepare the bypass counter for a shortcut launch.
 * Sets bypassCounter to -1 so BIsModOrShortcut returns true on every call
 * until GetGameID/GetPrimaryAppID naturally resets it to 0.
 * Call this immediately before SteamClient.Apps.RunGame().
 */
export function prepareForLaunch() {
  bypassCounter = -1;
}
