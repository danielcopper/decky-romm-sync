import type { RomMetadata } from "../types";
import { debugLog, logInfo } from "../api/backend";

// Module-level state
let metadataCache: Record<string, RomMetadata> = {};
let appIdToRomId: Record<number, number> = {};
let registeredAppIds: Set<number> = new Set();

/**
 * Wrap MobX state mutations so Steam's observable stores allow changes.
 */
function stateTransaction<T>(block: () => T): T {
  const globals = __mobxGlobals;
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
 * Apply direct property mutations to a SteamAppOverview for a RomM app.
 */
function applyDirectMutations(appId: number, metadata: RomMetadata) {
  const overview = appStore.GetAppOverviewByAppID(appId);
  if (!overview) return;

  stateTransaction(() => {
    overview.controller_support = 2;

    if (metadata.average_rating != null) {
      overview.metacritic_score = Math.round(metadata.average_rating);
    }

    if (overview.m_setStoreCategories && metadata.steam_categories) {
      for (const cat of metadata.steam_categories) {
        overview.m_setStoreCategories.add(cat);
      }
    }
  });
}

/**
 * Initialize metadata state and apply direct property mutations.
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

  // Apply direct mutations for all known apps (controller support, categories, metacritic)
  for (const appId of registeredAppIds) {
    const meta = getMetadataForAppId(appId);
    if (meta) applyDirectMutations(appId, meta);
  }

  logInfo(`Applied metadata mutations for ${registeredAppIds.size} apps`);
}

/**
 * Clean up metadata state. Call on plugin dismount.
 */
export function unregisterMetadataPatches() {
  metadataCache = {};
  appIdToRomId = {};
  registeredAppIds = new Set();
  logInfo("Cleared metadata state");
}

/**
 * Write tracked playtime to Steam's native UI fields.
 * Sets minutes_playtime_forever and rt_last_time_played so Steam shows
 * actual play time instead of "Never Played" for RomM shortcuts.
 * Returns true if the write succeeded, false if the overview wasn't available.
 */
export function updatePlaytimeDisplay(appId: number, totalSeconds: number, updateLastPlayed = true): boolean {
  const overview = appStore.GetAppOverviewByAppID(appId);
  if (!overview) {
    debugLog(`updatePlaytimeDisplay: appId=${appId} overview=null, skipping`);
    return false;
  }

  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes <= 0) return true; // Nothing to write, but not a failure

  const prevMinutes = overview.minutes_playtime_forever;
  const prevLastPlayed = overview.rt_last_time_played;
  stateTransaction(() => {
    overview.minutes_playtime_forever = totalMinutes;
    if (updateLastPlayed) {
      overview.rt_last_time_played = Math.floor(Date.now() / 1000);
    }
  });
  debugLog(`updatePlaytimeDisplay: appId=${appId} wrote ${totalMinutes}min (was ${prevMinutes}), rt_last_time_played was ${prevLastPlayed}`);
  return true;
}

type PlaytimeItem = { appId: number; totalSeconds: number };

/**
 * Attempt one pass of playtime writes. Returns items whose appStore overview
 * wasn't available yet, so the caller can retry them after a delay.
 */
function tryWritePlaytime(items: PlaytimeItem[]): PlaytimeItem[] {
  const notWritten: PlaytimeItem[] = [];
  for (const item of items) {
    if (!updatePlaytimeDisplay(item.appId, item.totalSeconds, false)) {
      notWritten.push(item);
    }
  }
  return notWritten;
}

/**
 * Apply playtime data for all known apps from the bulk playtime map.
 * Retries apps whose appStore overview isn't available yet (Steam may
 * still be loading shortcuts into its MobX store at plugin init).
 * Called at plugin load and after sync_complete.
 */
export async function applyAllPlaytime(
  playtimeMap: Record<string, { total_seconds: number }>,
  appIdMap: Record<string, number>,
) {
  // Build rom_id -> app_id reverse lookup
  const romIdToAppId: Record<string, number> = {};
  for (const [appIdStr, romId] of Object.entries(appIdMap)) {
    romIdToAppId[String(romId)] = Number(appIdStr);
  }

  // Build list of {appId, totalSeconds} to apply
  let pending: PlaytimeItem[] = [];
  for (const [romIdStr, entry] of Object.entries(playtimeMap)) {
    const appId = romIdToAppId[romIdStr];
    if (appId && entry.total_seconds > 0) {
      pending.push({ appId, totalSeconds: entry.total_seconds });
    }
  }

  debugLog(`applyAllPlaytime: ${Object.keys(playtimeMap).length} entries in playtimeMap, ${pending.length} with appId and >0 seconds`);

  if (pending.length === 0) return;

  // Try up to 4 times with increasing delays (0ms, 1s, 3s, 5s)
  const delays = [0, 1000, 3000, 5000];
  for (let attempt = 0; attempt < delays.length && pending.length > 0; attempt++) {
    if (delays[attempt] > 0) {
      await new Promise((r) => setTimeout(r, delays[attempt]));
    }

    pending = tryWritePlaytime(pending);

    if (pending.length > 0 && attempt < delays.length - 1) {
      debugLog(`applyAllPlaytime: attempt ${attempt + 1}, ${pending.length} apps not in appStore yet, retrying in ${delays[attempt + 1]}ms...`);
    }
  }

  if (pending.length > 0) {
    debugLog(`applyAllPlaytime: ${pending.length} apps still unavailable in appStore after all retries`);
  }
}

