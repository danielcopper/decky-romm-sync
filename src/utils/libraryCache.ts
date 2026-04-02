/**
 * Module-level prefetch cache for Library page data.
 *
 * Fires parallel fetches for platforms, collections, and BIOS on plugin init
 * (after backend is reachable) so data is already available when the user
 * opens the Library page — eliminating the loading spinner on first visit.
 *
 * Cache is invalidated after each sync completes so the next visit gets
 * fresh data.
 */

import { getPlatforms, getCollections, getSettings, getFirmwareStatus, logInfo, logError } from "../api/backend";
import type { PlatformSyncSetting, CollectionSyncSetting, FirmwarePlatformExt, RommErrorCode } from "../types";

// ── Cache entries ────────────────────────────────────────────

interface PlatformsCache {
  success: boolean;
  platforms: PlatformSyncSetting[];
}

interface CollectionsCache {
  success: boolean;
  collections: CollectionSyncSetting[];
  message?: string;
  error_code?: RommErrorCode;
  platformGroups: boolean;
}

interface BiosCache {
  success: boolean;
  message?: string;
  serverOffline: boolean;
  platforms: FirmwarePlatformExt[];
}

let _platformsCache: PlatformsCache | null = null;
let _collectionsCache: CollectionsCache | null = null;
let _biosCache: BiosCache | null = null;

let _platformsPromise: Promise<PlatformsCache> | null = null;
let _collectionsPromise: Promise<CollectionsCache> | null = null;
let _biosPromise: Promise<BiosCache> | null = null;

// ── Public API ───────────────────────────────────────────────

/**
 * Kick off parallel fetches for all three data sets.
 * Safe to call multiple times — won't re-fetch if already in progress or cached.
 */
export function prefetchLibraryData(): void {
  if (!_platformsCache && !_platformsPromise) {
    _platformsPromise = fetchPlatforms();
  }
  if (!_collectionsCache && !_collectionsPromise) {
    _collectionsPromise = fetchCollections();
  }
  if (!_biosCache && !_biosPromise) {
    _biosPromise = fetchBios();
  }
}

/**
 * Get cached platforms data. Returns null if not yet available.
 * If a fetch is in progress, returns the promise (caller can await).
 */
export function getCachedPlatforms(): PlatformsCache | null {
  return _platformsCache;
}

export function getPlatformsPromise(): Promise<PlatformsCache> | null {
  return _platformsPromise;
}

export function getCachedCollections(): CollectionsCache | null {
  return _collectionsCache;
}

export function getCollectionsPromise(): Promise<CollectionsCache> | null {
  return _collectionsPromise;
}

export function getCachedBios(): BiosCache | null {
  return _biosCache;
}

export function getBiosPromise(): Promise<BiosCache> | null {
  return _biosPromise;
}

/**
 * Invalidate all caches. Call after sync completes or when data may be stale.
 */
export function invalidateLibraryCache(): void {
  _platformsCache = null;
  _collectionsCache = null;
  _biosCache = null;
  _platformsPromise = null;
  _collectionsPromise = null;
  _biosPromise = null;
}

// ── Internal fetch helpers ───────────────────────────────────

async function fetchPlatforms(): Promise<PlatformsCache> {
  try {
    const result = await getPlatforms();
    _platformsCache = { success: result.success, platforms: result.platforms };
    logInfo(`Library prefetch: ${result.platforms.length} platforms cached`);
  } catch (e) {
    _platformsCache = { success: false, platforms: [] };
    logError(`Library prefetch platforms failed: ${e}`);
  }
  _platformsPromise = null;
  return _platformsCache;
}

async function fetchCollections(): Promise<CollectionsCache> {
  try {
    const [collResult, settingsResult] = await Promise.all([
      getCollections(),
      getSettings(),
    ]);
    const collections = collResult.collections ?? [];
    _collectionsCache = {
      success: collResult.success,
      collections,
      message: collResult.message,
      error_code: collResult.error_code,
      platformGroups: !!settingsResult.collection_create_platform_groups,
    };
    logInfo(`Library prefetch: ${collections.length} collections cached`);
  } catch (e) {
    _collectionsCache = { success: false, collections: [], platformGroups: false, serverOffline: false } as CollectionsCache;
    logError(`Library prefetch collections failed: ${e}`);
  }
  _collectionsPromise = null;
  return _collectionsCache;
}

async function fetchBios(): Promise<BiosCache> {
  try {
    const result = await getFirmwareStatus();
    _biosCache = {
      success: result.success,
      message: result.message,
      serverOffline: result.server_offline ?? false,
      platforms: result.platforms,
    };
    logInfo(`Library prefetch: ${result.platforms.length} BIOS platforms cached`);
  } catch (e) {
    _biosCache = { success: false, serverOffline: false, platforms: [], message: `${e}` };
    logError(`Library prefetch BIOS failed: ${e}`);
  }
  _biosPromise = null;
  return _biosCache;
}
