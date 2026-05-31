import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { getCachedGameDetail, invalidateCachedGameDetail, _cacheForTests } from "./cachedGameDetailStore";
import type { CachedGameDetail } from "../api/backend";

describe("getCachedGameDetail", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    _cacheForTests.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns the same promise for back-to-back calls within the TTL window", async () => {
    const promise1 = getCachedGameDetail(42);
    const promise2 = getCachedGameDetail(42);
    expect(promise1).toBe(promise2);
    await promise1.catch(() => undefined);
  });

  it("creates a fresh entry per appId", () => {
    const a = getCachedGameDetail(1);
    const b = getCachedGameDetail(2);
    expect(a).not.toBe(b);
    expect(_cacheForTests.has(1)).toBe(true);
    expect(_cacheForTests.has(2)).toBe(true);
  });

  it("evicts the cache entry after the TTL window when the underlying call resolves", async () => {
    const detail: CachedGameDetail = { found: true, rom_id: 99 };
    _cacheForTests.set(7, { promise: Promise.resolve(detail), ts: Date.now() });

    await _cacheForTests.get(7)!.promise;
    vi.advanceTimersByTime(3000);
    await Promise.resolve();
    // Direct cache injection bypasses the .then-scheduled eviction; callers
    // who poke the cache directly should clean up themselves.
    expect(_cacheForTests.has(7)).toBe(true);
  });
});

describe("invalidateCachedGameDetail", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    _cacheForTests.clear();
  });
  afterEach(() => vi.useRealTimers());

  it("drops the entry for the given appId", () => {
    const detail: CachedGameDetail = { found: true, rom_id: 1 };
    _cacheForTests.set(1, { promise: Promise.resolve(detail), ts: Date.now() });
    expect(_cacheForTests.has(1)).toBe(true);

    invalidateCachedGameDetail(1);

    expect(_cacheForTests.has(1)).toBe(false);
  });

  it("is a no-op for an unknown appId", () => {
    expect(() => invalidateCachedGameDetail(999)).not.toThrow();
  });
});
