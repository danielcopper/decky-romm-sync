import { describe, it, expect, beforeEach, vi } from "vitest";
import * as backend from "../api/backend";
import { fetchMetadataCachePages } from "./metadataCache";
import type { RomMetadata } from "../types";

function meta(summary: string): RomMetadata {
  return {
    summary,
    genres: [],
    companies: [],
    first_release_date: null,
    average_rating: null,
    game_modes: [],
    player_count: "",
    cached_at: 0,
  };
}

describe("metadataCache — fetchMetadataCachePages", () => {
  beforeEach(() => {
    vi.mocked(backend.getMetadataCachePage).mockReset();
  });

  it("assembles multiple pages into one cache, paged by pageSize at ascending offsets", async () => {
    vi.mocked(backend.getMetadataCachePage)
      .mockResolvedValueOnce({ items: { "1": meta("A"), "2": meta("B") }, total: 4 })
      .mockResolvedValueOnce({ items: { "3": meta("C"), "4": meta("D") }, total: 4 });

    const cache = await fetchMetadataCachePages(2, 1000);

    expect(Object.keys(cache).sort()).toEqual(["1", "2", "3", "4"]);
    // Distinct page content actually reaches the assembled cache.
    expect(cache["3"]!.summary).toBe("C");
    expect(vi.mocked(backend.getMetadataCachePage)).toHaveBeenNthCalledWith(1, 0, 2);
    expect(vi.mocked(backend.getMetadataCachePage)).toHaveBeenNthCalledWith(2, 2, 2);
    expect(vi.mocked(backend.getMetadataCachePage)).toHaveBeenCalledTimes(2);
  });

  it("stops on an empty page even when total overshoots the rows returned", async () => {
    vi.mocked(backend.getMetadataCachePage)
      .mockResolvedValueOnce({ items: { "1": meta("A") }, total: 100 })
      .mockResolvedValueOnce({ items: {}, total: 100 });

    const cache = await fetchMetadataCachePages(1, 1000);

    // The empty second page breaks the loop rather than spinning to total=100.
    expect(Object.keys(cache)).toEqual(["1"]);
    expect(vi.mocked(backend.getMetadataCachePage)).toHaveBeenCalledTimes(2);
  });

  it("returns an empty cache when the only page is empty (total 0)", async () => {
    vi.mocked(backend.getMetadataCachePage).mockResolvedValueOnce({ items: {}, total: 0 });

    const cache = await fetchMetadataCachePages(500, 1000);

    expect(cache).toEqual({});
    expect(vi.mocked(backend.getMetadataCachePage)).toHaveBeenCalledTimes(1);
  });
});
