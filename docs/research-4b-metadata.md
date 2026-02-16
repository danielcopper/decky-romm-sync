# Phase 4B Research: Native Metadata via Store Patching

## MetaDeck Store Patching (from EmuDeck/MetaDeck source)

### Global Store Objects

Steam exposes these on `window` — just need `declare global` for TypeScript:

```typescript
declare global {
    let appStore: AppStore;
    let appDetailsStore: AppDetailsStore;
    let appDetailsCache: {
        SetCachedDataForApp(app_id: number, key: string, number: number, data: any): void;
    }
}
```

### Required Imports

```typescript
import { afterPatch, replacePatch, callOriginal, Patch } from "@decky/ui";
import { runInAction } from "mobx";
```

### Critical Helper: stateTransaction

Steam's store objects are MobX observables. Must wrap mutations:

```typescript
function stateTransaction<T>(block: () => T) {
    const prev = window["__mobxGlobals"].allowStateChanges;
    window["__mobxGlobals"].allowStateChanges = true;
    const r = runInAction(block);
    window["__mobxGlobals"].allowStateChanges = prev;
    return r;
}
```

### Non-Steam Shortcut Detection

`overview.app_type == 1073741824` identifies non-Steam shortcuts.

### Patch 1: GetDescriptions (description text)

Patched on `appDetailsStore.__proto__` with `replacePatch`:

```typescript
replacePatch(appDetailsStore.__proto__, "GetDescriptions", (args) => {
    const overview = appStore.GetAppOverviewByAppID(args[0]);
    if (overview.app_type == 1073741824) {
        let appData = appDetailsStore.GetAppData(args[0]);
        if (appData) {
            const description = "..."; // our metadata
            stateTransaction(() => {
                appData.descriptionsData = {
                    strFullDescription: description,
                    strSnippet: description
                };
                appDetailsCache.SetCachedDataForApp(args[0], "descriptions", 1, appData.descriptionsData);
            });
            return appData.descriptionsData;
        }
    }
    return callOriginal;
});
```

**Data shape:** `{ strFullDescription: ReactNode, strSnippet: ReactNode }`

### Patch 2: GetAssociations (developer/publisher)

Patched on `appDetailsStore.__proto__` with `replacePatch`:

```typescript
replacePatch(appDetailsStore.__proto__, "GetAssociations", (args) => {
    if (appStore.GetAppOverviewByAppID(args[0]).app_type == 1073741824) {
        let appData = appDetailsStore.GetAppData(args[0]);
        if (appData && !appData?.associationData) {
            stateTransaction(() => {
                appData.associationData = {
                    rgDevelopers: [{ strName: "Dev Name", strURL: "" }],
                    rgPublishers: [{ strName: "Pub Name", strURL: "" }],
                    rgFranchises: []
                };
                appDetailsCache.SetCachedDataForApp(args[0], "associations", 1, appData.associationData);
            });
        }
    }
    return callOriginal;
});
```

**Data shape:**
```typescript
{
    rgDevelopers: { strName: string, strURL: string }[],
    rgPublishers: { strName: string, strURL: string }[],
    rgFranchises: { strName: string, strURL: string }[]
}
```

### Patch 3: GetCanonicalReleaseDate (release date)

Patched on `appStore.allApps[0].__proto__` with `afterPatch`:

```typescript
afterPatch(appStore.allApps[0].__proto__, "GetCanonicalReleaseDate", function (_, ret) {
    if (this.app_type == 1073741824) {
        const releaseDate = ...; // unix timestamp
        if (releaseDate) return releaseDate;
    }
    return ret;
});
```

**Returns:** Unix timestamp (number).
**Note:** Uses `function` (not arrow) to get `this` context (the SteamAppOverview instance).

### Patch 4: BHasStoreCategory (genres/categories)

Patched on `appStore.allApps[0].__proto__` with `replacePatch`:

```typescript
replacePatch(appStore.allApps[0].__proto__, "BHasStoreCategory", function (args) {
    if ((this as SteamAppOverview).app_type == 1073741824) {
        const categories = [...]; // Steam StoreCategory enum values
        if (categories.includes(args[0])) return true;
    }
    return callOriginal;
});
```

**Key technique:** Uses `appStore.allApps[0].__proto__` to get shared SteamAppOverview prototype.

### Patch 5: BIsModOrShortcut (hide "non-Steam" label)

Patched on `appStore.allApps[0].__proto__` with `afterPatch`:

```typescript
afterPatch(appStore.allApps[0].__proto__, "BIsModOrShortcut", function (_, ret) {
    if (ret === true) {
        // Return false to make Steam treat shortcuts as "real" games
        // This enables full detail page with descriptions, associations, etc.
        return false;
    }
    return ret;
});
```

**Critical:** This is the most impactful patch — without it, Steam won't show descriptions/associations for non-Steam apps even if patched.

MetaDeck uses a complex bypass counter system to handle edge cases (GetGameID, GetPrimaryAppID, BHasRecentlyLaunched). We may need similar logic or can start simpler.

### Patch 6: Direct Property Mutation (controller support, etc.)

Can directly mutate MobX-observed properties inside `stateTransaction`:

```typescript
stateTransaction(() => {
    overview.controller_support = 2; // full controller support
    overview.metacritic_score = 85;
    overview.m_setStoreCategories.add(28); // full controller support category
});
```

### Patch Lifecycle

- Register patches on plugin load (in `definePlugin`)
- Store `Patch` references from `afterPatch`/`replacePatch` return values
- Call `patch.unpatch()` on plugin dismount
- Patches survive QAM close/reopen (they're on prototypes, not component instances)

### Dependencies

- `mobx` — likely already in Decky's runtime (MetaDeck imports `runInAction` directly)
- `@decky/ui` v4+ — for `afterPatch`, `replacePatch`, `callOriginal`

---

## RomM API Metadata

### Key Discovery

The list endpoint (`/api/roms?platform_ids=X`) returns the SAME fields as the detail endpoint (`/api/roms/{id}`) — including `metadatum`, `igdb_metadata`, `summary`, `merged_screenshots`. No extra per-ROM API calls needed during sync.

### `metadatum` (merged/canonical — best source)

```json
{
  "genres": ["Role-playing (RPG)", "Sport"],
  "franchises": ["Mario"],
  "collections": ["Mario Golf"],
  "companies": ["Camelot Software Planning", "Nintendo"],
  "game_modes": ["Single player", "Split screen"],
  "age_ratings": ["0", "3", "A", "E", "G"],
  "player_count": "1-4",
  "first_release_date": 1082592000000,
  "average_rating": 79.665
}
```

**Note:** `first_release_date` in `metadatum` is **milliseconds** since epoch. In `igdb_metadata` it's **seconds**.

### Top-level ROM fields

- **`summary`**: Full text game description (always present when igdb_id exists)
- **`alternative_names`**: Localized titles
- **`merged_screenshots`**: Server-local paths like `/assets/romm/resources/roms/57/4409/screenshots/0.jpg` (need RomM base URL + auth to fetch)
- **`youtube_video_id`**: string|null
- **`regions`**: `["USA"]`

### Developer vs Publisher

Companies are a **flat list** — no explicit dev/pub role distinction. Heuristic: first = developer, last = publisher (unreliable). The `igdb_metadata.companies` may have more structure but still no explicit role field.

### `igdb_metadata` (more detailed)

- `total_rating` / `aggregated_rating` (strings)
- `age_ratings`: Structured array with `{rating, category, rating_cover_url}`
- `similar_games`: Array of `{id, name, slug, type, cover_url}`
- `platforms`: Other platforms the game was released on
- `multiplayer_modes`, `expansions`, `dlcs`, `remasters`, `remakes`

### Metadata Availability

Even obscure platforms have rich metadata when `igdb_id` is present. `companies` may be empty for some games, but `summary`, `genres`, `game_modes`, `first_release_date`, and `average_rating` are consistently populated.

### Fields to Use for Phase 4B

| RomM Field | Steam Patch Target | Notes |
|---|---|---|
| `summary` | `GetDescriptions` → `strFullDescription` + `strSnippet` | Always available with igdb_id |
| `metadatum.companies` | `GetAssociations` → `rgDevelopers` + `rgPublishers` | Flat list, first=dev heuristic |
| `metadatum.first_release_date` | `GetCanonicalReleaseDate` | Divide by 1000 for unix timestamp |
| `metadatum.genres` | `BHasStoreCategory` | Need to map genre strings → Steam StoreCategory enum values |
| `metadatum.average_rating` | `overview.metacritic_score` | Direct property mutation |
| `metadatum.game_modes` | `overview.m_setStoreCategories` | Map "Single player" etc. to Steam category IDs |
| `merged_screenshots` | TBD (Phase 4C) | Needs auth to fetch |

---

## Implementation Plan (Draft)

1. **Backend**: New callable `get_rom_metadata(rom_id)` — fetches from RomM, caches in `metadata_cache.json` (7-day TTL)
2. **Frontend**: New `src/patches/metadataPatches.ts` — registers all store patches on plugin load
3. **Frontend**: `GameDetailPanel` triggers metadata fetch on mount, patches update reactively
4. **Frontend**: Metadata cache loaded into memory on plugin load, patches read from it
5. **Cleanup**: All patches `.unpatch()` on plugin dismount
