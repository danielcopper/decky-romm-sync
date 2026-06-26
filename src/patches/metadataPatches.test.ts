import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { registerMetadataPatches, applyAllMetadata } from "./metadataPatches";
import type { RomMetadata } from "../types";

// RomMetadata has several required fields; build a full object and override the
// few that applyDirectMutations actually reads (average_rating, steam_categories).
function makeMeta(overrides: Partial<RomMetadata> = {}): RomMetadata {
  return {
    summary: "",
    genres: [],
    companies: [],
    first_release_date: null,
    average_rating: null,
    game_modes: [],
    player_count: "",
    cached_at: 0,
    ...overrides,
  };
}

interface FakeOverview {
  appid: number;
  controller_support: number;
  metacritic_score: number;
  m_setStoreCategories: Set<number>;
}

function makeOverview(appid: number): FakeOverview {
  return { appid, controller_support: 0, metacritic_score: 0, m_setStoreCategories: new Set<number>() };
}

// appId 100 → rom_id 10
const APP_ID_MAP = { "100": 10 };

describe("applyAllMetadata (#1203 readiness retry)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // test-setup's afterEach calls unstubAllGlobals, so re-stub the Steam globals
    // each test. __mobxGlobals is undefined here → stateTransaction applies the
    // mutation block directly (its `if (!globals) return block()` path).
    vi.stubGlobal("__mobxGlobals", undefined);
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: vi.fn(), allApps: [] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("applies controller support, metacritic and categories when the overview is present", async () => {
    const ov = makeOverview(100);
    vi.mocked(appStore.GetAppOverviewByAppID).mockReturnValue(ov as unknown as SteamAppOverview);
    registerMetadataPatches({ "10": makeMeta({ average_rating: 88, steam_categories: [1, 2] }) }, APP_ID_MAP);

    await applyAllMetadata();

    expect(ov.controller_support).toBe(2);
    expect(ov.metacritic_score).toBe(88);
    expect([...ov.m_setStoreCategories]).toEqual([1, 2]);
  });

  it("retries an app whose overview isn't loaded yet, then applies once it appears", async () => {
    const ov = makeOverview(100);
    vi.mocked(appStore.GetAppOverviewByAppID)
      .mockReturnValueOnce(null) // first pass: appStore not populated yet → silent skip today
      .mockReturnValue(ov as unknown as SteamAppOverview); // retry: overview now present
    registerMetadataPatches({ "10": makeMeta({ average_rating: 70 }) }, APP_ID_MAP);

    const done = applyAllMetadata();
    // The first synchronous pass (0ms) ran against a null overview → nothing applied yet.
    expect(ov.controller_support).toBe(0);

    await vi.advanceTimersByTimeAsync(1000); // second attempt fires at +1s
    await done;

    expect(ov.controller_support).toBe(2);
    expect(ov.metacritic_score).toBe(70);
  });

  it("is idempotent — repeated applies don't duplicate categories or corrupt state", async () => {
    const ov = makeOverview(100);
    vi.mocked(appStore.GetAppOverviewByAppID).mockReturnValue(ov as unknown as SteamAppOverview);
    registerMetadataPatches({ "10": makeMeta({ steam_categories: [5, 5, 7] }) }, APP_ID_MAP);

    await applyAllMetadata();
    await applyAllMetadata(); // a second pass must be safe (retries re-apply)

    expect([...ov.m_setStoreCategories].sort((a, b) => a - b)).toEqual([5, 7]);
    expect(ov.controller_support).toBe(2);
  });
});
