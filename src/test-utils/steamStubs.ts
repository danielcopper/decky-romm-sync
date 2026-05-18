// Per-test stubs for the Steam Deck globals `collectionStore` and `appStore`.
// Use in `beforeEach` to control the non-Steam app enumeration surface for
// components that read these globals (DangerZone, RomMPlaySection,
// RomMGameInfoPanel).
//
// The shapes here mirror the relevant slices of the ambient declarations in
// `src/types/steam.d.ts` — only the fields the consumers actually read need
// to be present, but `SteamAppOverview` requires `appid`, `display_name`, and
// `strDisplayName`, so `stubAppStore` accepts a permissive partial shape and
// fills in defaults at lookup time.

import { vi } from "vitest";

type OverviewLike = Partial<Pick<SteamAppOverview, "appid" | "display_name" | "strDisplayName">>;

export function stubCollectionStore(appIds: number[]): void {
  vi.stubGlobal("collectionStore", {
    deckDesktopApps: { apps: new Map(appIds.map((id) => [id, {}])) },
    userCollections: [],
  });
}

export function stubAppStore(overviews: Record<number, OverviewLike>): void {
  vi.stubGlobal("appStore", {
    GetAppOverviewByAppID: vi.fn((id: number): SteamAppOverview | null => {
      const o = overviews[id];
      if (!o) return null;
      // Fill in the required SteamAppOverview fields so callers that read
      // `appid` / `display_name` / `strDisplayName` see consistent shapes.
      return {
        appid: o.appid ?? id,
        display_name: o.display_name ?? "",
        strDisplayName: o.strDisplayName ?? "",
      };
    }),
    allApps: [],
  });
}
