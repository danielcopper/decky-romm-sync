/**
 * In-memory cache for the get_cached_game_detail callable. Centralizes the
 * cache map + TTL eviction so component call sites never mutate it directly.
 *
 * Follows the same module-scope pattern as utils/connectionState.ts: state
 * lives here, callers go through exported functions. No subscribe API yet —
 * add one when there's a consumer that needs invalidation notifications.
 */

import { callable } from "@decky/api";
import type { CachedGameDetail } from "../api/backend";

const CACHE_TTL_MS = 3000;

interface CacheEntry {
  promise: Promise<CachedGameDetail>;
  ts: number;
}

const _cache = new Map<number, CacheEntry>();
const _raw = callable<[number], CachedGameDetail>("get_cached_game_detail");

export function getCachedGameDetail(appId: number): Promise<CachedGameDetail> {
  const now = Date.now();
  const entry = _cache.get(appId);
  if (entry && now - entry.ts < CACHE_TTL_MS) return entry.promise;
  const promise = _raw(appId);
  _cache.set(appId, { promise, ts: now });
  promise.then(
    () => {
      setTimeout(() => {
        _cache.delete(appId);
      }, CACHE_TTL_MS);
    },
    () => {
      _cache.delete(appId);
    },
  );
  return promise;
}

/** Drop a single appId's cached entry so the next get_cached_game_detail call
 *  hits the backend. Called after operations that invalidate the cached state
 *  (e.g. core changes). */
export function invalidateCachedGameDetail(appId: number): void {
  _cache.delete(appId);
}

/** Test-only: wipe the entire cache + assert entry presence. */
export const _cacheForTests = _cache;
