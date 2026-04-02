# Per-Platform Sync Pipeline & Accordion UX

> **Created:** April 2, 2026
> **Status:** 📋 Plan — awaiting approval
> **Supersedes:** [DUAL-BAR-PROGRESS-UX.md](DUAL-BAR-PROGRESS-UX.md) (4-phase stepper + dual progress bars)

---

## 1. Overview

### What We're Changing

Replace the **4-phase abstract stepper** (Connect → Fetch → Collect → Apply) and
**dual progress bar** layout with a **per-platform accordion** that shows each
platform's sync lifecycle as a single expandable row.

### Why

The 4-phase stepper is meaningless to the user. They don't think in terms of
"fetch ROMs" vs "apply shortcuts." They think: **"Which of my platforms are done?
Which one is happening now? How many are left?"**

The accordion answers those questions at a glance — every platform is a row,
the active one expands to show details, and checkmarks accumulate as the sync
progresses.

### Design Decision Log

| # | Decision | Chosen | Why |
|---|---|---|---|
| 1 | Top-level progress indicator | **Per-platform accordion** (option E) | Uses vertical space well; shows full queue + inline detail for active platform; natural for scrollable QAM panel |
| 2 | Rejected: 4-phase stepper | ❌ | Abstract phases (Connect/Fetch/Collect/Apply) don't match user mental model |
| 3 | Rejected: horizontal dot/segment bar | ❌ (options C/D) | 310px too narrow for many platform names; vertical is the cheap resource |
| 4 | Rejected: chip layout | ❌ (option F) | Wrapping chip layout gets messy at 310px with long platform names |
| 5 | Fetch architecture | **Concurrent fetch, ordered emission** | All platform fetches start concurrently (fast); events emitted in sorted order (clean UX) |
| 6 | Collection handling | **Deferred to end** | Collections are cross-platform; build them after all platforms are processed |
| 7 | Preview/delta flow | **Unchanged for now** | Minimize risk; can unify in a follow-up |

---

## 2. UX Design — Accordion Wireframes

### Layout Anatomy

```
┌─────────────────────────────────────────┐
│  [Optional header — fetch phase only]   │
│                                         │
│  {icon}  {Platform Name}    {counter}   │  ← row (collapsed)
│  {icon}  {Platform Name}    {counter}   │  ← row (collapsed)
│  {icon}  {Platform Name}   {x/total}    │  ← row (EXPANDED)
│     {progress bar}                      │
│     {current game title}                │
│     {ETA}                               │
│  {icon}  {Platform Name}    {counter}   │  ← row (collapsed)
│  ...                                    │
│                                         │
│  [Footer — overall summary + ETA]       │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

**Row icons:**
- `○` — pending (dim)
- `⟳` — active / in-progress (highlighted, only one at a time)
- `✓` — done (green/accent)

**Counter (right side):**
- Pending: total ROM count (dim), e.g. `396`
- Active: progress fraction, e.g. `87/329`
- Done: total ROM count (dim), e.g. `329`

### State 1: Connecting (~1s)

Before the platform list is known. No accordion yet.

```
┌─────────────────────────────────────────┐
│                                         │
│  Connecting to RomM...                  │
│  ░░░░░░░░░▓▓░░░░░░░░░░░░░░░░░░░░░░░░░ │
│                                         │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

### State 2: Fetching Library (5–20s)

Platform list received via `sync_plan`. Accordion rendered with all platforms
pending. A compact header shows overall fetch progress.

```
┌─────────────────────────────────────────┐
│  Fetching library...  3/14 platforms    │
│  ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
│  142 / 3,400 ROMs                       │
│─────────────────────────────────────────│
│  ○  Atari 2600                     42   │
│  ○  GameCube                      396   │
│  ○  N64                           329   │
│  ○  NES                         1,111   │
│  ○  PlayStation                 1,978   │
│  ○  PS2                           637   │
│  ○  SNES                          827   │
│                                         │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

- Platforms listed alphabetically (fixed order throughout sync)
- All rows are `○` pending — no individual platform fetch status shown
- The header progress bar tracks overall ROMs fetched across all platforms
- When the first `sync_apply_platform` event arrives, transition to State 3

### State 3: First Platform Applying

The fetch header collapses. The first platform's row expands.

```
┌─────────────────────────────────────────┐
│  ⟳  Atari 2600                  12/42   │
│     ████████████████░░░░░░░░░░░░░░░░░░ │
│     Pitfall! (USA)                      │
│     ~8s remaining                       │
│  ○  GameCube                      396   │
│  ○  N64                           329   │
│  ○  NES                         1,111   │
│  ○  PlayStation                 1,978   │
│  ○  PS2                           637   │
│  ○  SNES                          827   │
│                                         │
│  1 of 7 platforms · ~8m remaining       │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

- Only one row is expanded at a time (the active platform)
- Progress bar = shortcuts created / total for this platform
- Game title in italic (the shortcut currently being created)
- Footer shows overall platform count + global ETA

### State 4: Mid-Sync (Some Done, One Active, Rest Pending)

```
┌─────────────────────────────────────────┐
│  ✓  Atari 2600                     42   │
│  ✓  GameCube                      396   │
│  ⟳  N64                        87/329   │
│     ██████████████░░░░░░░░░░░░░░░░░░░░ │
│     GoldenEye 007 (USA)                 │
│     ~45s remaining                      │
│  ○  NES                         1,111   │
│  ○  PlayStation                 1,978   │
│  ○  PS2                           637   │
│  ○  SNES                          827   │
│                                         │
│  3 of 7 platforms · ~6m remaining       │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

### State 5: Artwork Draining (Within a Platform)

When all shortcuts for a platform are created but artwork downloads are still
in flight, the expanded row shows artwork status:

```
│  ⟳  N64                       329/329   │
│     ████████████████████████████████████ │
│     Finishing artwork (3 remaining)...   │
│     ~5s remaining                       │
```

This is a transient sub-state within the active platform. The row stays
expanded until artwork is fully drained, then transitions to `✓`.

### State 6: Collections Phase

After all platforms are `✓`, collections are built. This appears as a
separate section below the platform list — collections are cross-platform
and don't belong in the per-platform accordion.

```
┌─────────────────────────────────────────┐
│  ✓  Atari 2600                     42   │
│  ✓  GameCube                      396   │
│  ✓  N64                           329   │
│  ✓  NES                         1,111   │
│  ✓  PlayStation                 1,978   │
│  ✓  PS2                           637   │
│  ✓  SNES                          827   │
│                                         │
│  Building collections...          5/8   │
│  ████████████████████████░░░░░░░░░░░░░ │
│  Best of SNES                           │
│                                         │
│  7 of 7 platforms · ~12s remaining      │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

### State 7: Removing Stale Shortcuts

Only shown if `remove_on_unsync` is enabled and there are stale shortcuts.

```
┌─────────────────────────────────────────┐
│  ✓  Atari 2600                     42   │
│  ✓  GameCube                      396   │
│  ✓  (... 5 more platforms)              │
│  ✓  SNES                          827   │
│                                         │
│  Cleaning up...                  12/15   │
│  ████████████████████████████████░░░░░░ │
│                                         │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

### State 8: Complete

All rows checked. Summary message replaces the footer.

```
┌─────────────────────────────────────────┐
│  ✓  Atari 2600                     42   │
│  ✓  GameCube                      396   │
│  ✓  N64                           329   │
│  ✓  NES                         1,111   │
│  ✓  PlayStation                 1,978   │
│  ✓  PS2                           637   │
│  ✓  SNES                          827   │
│                                         │
│  ✓  Sync complete — 5,320 games         │
│     7 platforms · 8 collections         │
│     6m 12s elapsed                      │
│                                         │
│  [ Sync Library ]  [ Force Full Sync ]  │
└─────────────────────────────────────────┘
```

### State 9: Cancelled

The active platform shows its partial progress. Unprocessed platforms stay `○`.

```
┌─────────────────────────────────────────┐
│  ✓  Atari 2600                     42   │
│  ✓  GameCube                      396   │
│  ✗  N64                        87/329   │  ← partial (cancelled icon)
│  ○  NES                         1,111   │
│  ○  PlayStation                 1,978   │
│  ○  PS2                           637   │
│  ○  SNES                          827   │
│                                         │
│  ⚠ Sync cancelled — 525 of 5,320       │
│     2 platforms complete                │
│                                         │
│  [ Sync Library ]  [ Force Full Sync ]  │
└─────────────────────────────────────────┘
```

### State 10: Error

```
┌─────────────────────────────────────────┐
│  ✓  Atari 2600                     42   │
│  ✓  GameCube                      396   │
│  ✗  N64                            —    │  ← failed
│                                         │
│  ✗  Connection lost                     │
│     Check your network and RomM server  │
│                                         │
│  [ Retry ]  [ Force Full Sync ]         │
└─────────────────────────────────────────┘
```

### Truncation for Many Platforms (14+)

When more than **8 platforms** are syncing, apply progressive truncation:

**During sync (some done, one active, rest pending):**
- Always show: active platform (expanded) + 2 nearest completed + 2 nearest pending
- Collapse the rest into summary rows

```
┌─────────────────────────────────────────┐
│  ✓  (3 platforms complete)              │  ← collapsed summary
│  ✓  GameCube                      396   │  ← nearest completed
│  ⟳  N64                        87/329   │  ← active (expanded)
│     ██████████████░░░░░░░░░░░░░░░░░░░░ │
│     GoldenEye 007 (USA)                 │
│     ~45s remaining                      │
│  ○  NES                         1,111   │  ← next pending
│  ○  PlayStation                 1,978   │
│  ○  (6 more platforms)                  │  ← collapsed summary
│                                         │
│  4 of 14 platforms · ~12m remaining     │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

**After sync complete:** collapse to a single summary + expand button:

```
│  ✓  All 14 platforms synced             │
│     (tap to expand)                     │
```

---

## 3. Event Protocol

### New Events

#### `sync_plan`

Emitted once at the start of sync, after the platform list is fetched.
Gives the frontend the full platform list for rendering the accordion.

```json
{
  "platforms": [
    { "name": "Atari 2600", "slug": "atari2600", "rom_count": 42 },
    { "name": "GameCube", "slug": "gc", "rom_count": 396 },
    { "name": "N64", "slug": "n64", "rom_count": 329 }
  ],
  "has_collections": true,
  "estimated_total_roms": 5320
}
```

#### `sync_apply_collections`

Emitted once after all platforms are processed. Contains the full
collection membership data for the frontend to build Steam collections.

```json
{
  "platform_app_ids": {
    "GameCube": [123456, 789012, ...],
    "N64": [345678, ...]
  },
  "collection_memberships": {
    "Best of SNES": [111, 222, 333],
    "Mario Franchise": [444, 555]
  },
  "total_collections": 8
}
```

The frontend translates `rom_id`s to `app_id`s using its accumulated
mapping from the apply phase, then calls `appendToCollections()` and
`appendToRomMCollections()` in a single pass.

### Modified Events

#### `sync_apply_platform` (changed)

**Removed field:** `collection_memberships` — moved to `sync_apply_collections`.

**Added field:** `rom_count` — estimated ROM count for this platform (from
the platform list), so the frontend can show it in the pending rows even
before shortcuts data arrives.

Everything else stays the same: `platform_name`, `platform_index`,
`total_platforms`, `total_shortcuts_all`, `shortcuts_before`, `shortcuts`,
`changed_shortcuts`, `step`, `total_steps`.

#### `sync_progress` (simplified)

During the **fetching** phase (before `sync_apply_platform` events start),
the backend still emits `sync_progress` events with:

```json
{
  "running": true,
  "phase": "fetching",
  "current": 142,
  "total": 3400,
  "message": "Fetching library... 3/14 platforms",
  "platformCurrent": 3,
  "platformTotal": 14,
  "elapsedSec": 4.2,
  "etaSec": 12.8
}
```

The frontend uses this to update the fetch header in State 2.

**Removed fields** (no longer needed):
- `step` / `totalSteps` — the 4-phase stepper is gone
- `stepLabel` — replaced by accordion row states

**Kept fields:**
- `platformCurrent` / `platformTotal` — repurposed: platforms fetched (during fetch), platform index (during apply)
- `platformLabel` — current platform name
- `subMessage` — current game title
- `elapsedSec` / `etaSec` / `itemsPerSec` — ETA data

### Unchanged Events

- `sync_apply_removals` — same as before
- `sync_apply_done` — same as before (total_platforms, total_shortcuts, total_removals, remove_on_unsync)

### Removed Concepts

- **4-phase step numbering** (`step=1` through `step=4`) — eliminated
- **Step labels** (`"Connecting"`, `"Fetching ROMs"`, `"Fetching collections"`, `"Applying changes"`) — eliminated
- **Dual progress bars** (overall + platform) — replaced by accordion with single bar per active platform

---

## 4. Backend Architecture

### Current Flow (Before)

```
_do_sync()
  ├── _fetch_and_prepare()           ← monolithic, fetches ALL
  │     ├── Phase 1: fetch platforms
  │     ├── Phase 2: fetch ALL ROMs (concurrent, asyncio.gather)
  │     ├── Phase 3: fetch ALL collections
  │     └── Phase 4: prepare shortcuts
  ├── compute stale rom_ids
  └── _emit_per_platform()           ← emits all at once (post-hoc grouping)
        ├── sync_apply_platform × N
        ├── sync_apply_removals
        └── sync_apply_done
```

### New Flow (After)

```
_do_sync()
  ├── fetch platform list             ← 1 API call (~1s)
  ├── emit sync_plan                  ← NEW: frontend renders accordion
  │
  ├── concurrent fetch all platforms  ← same AdaptiveSemaphore, fast
  │     (ROMs fetched in parallel, results buffered)
  │
  ├── ordered per-platform emission   ← NEW: emit as each completes, in sorted order
  │     for each platform (sorted alphabetically):
  │       ├── await fetch result (may already be done)
  │       ├── build shortcuts for this platform
  │       ├── emit sync_apply_platform
  │       └── sleep(50ms)
  │
  ├── fetch collection-only ROMs      ← same as before
  ├── emit sync_apply_platform for collection-only ROMs (grouped by their platform)
  ├── emit sync_apply_collections     ← NEW: full membership data
  │
  ├── compute stale rom_ids
  ├── emit sync_apply_removals
  └── emit sync_apply_done
```

### Key Method Changes

#### `_fetch_and_prepare()` → **split into pieces**

The monolithic `_fetch_and_prepare` is broken apart:

1. **`_fetch_enabled_platforms()`** — unchanged, returns platform list
2. **`_start_concurrent_fetch(platforms)`** — NEW: starts all platform fetches
   concurrently using AdaptiveSemaphore, returns a dict of
   `{platform_name: asyncio.Task}`
3. **`_emit_platforms_in_order(tasks, platforms)`** — NEW: awaits each task
   in sorted order, builds shortcuts, emits `sync_apply_platform` for each
4. **`_fetch_collection_roms()`** — unchanged (runs after all platforms)

#### `_emit_per_platform()` → **eliminated**

The post-hoc grouping logic in `_emit_per_platform` is no longer needed.
Each platform's shortcuts are built and emitted inline as its fetch
completes, in `_emit_platforms_in_order`.

#### `_emit_progress()` → **simplified**

Remove `step`, `total_steps`, `step_label` keyword params. These were
artifacts of the 4-phase model. The `platformCurrent`/`platformTotal`
params are kept and repurposed for fetch-phase progress.

### Collection Handling

Collections are cross-platform (a "Best of" collection can span N64, SNES,
GameCube). They cannot be processed per-platform.

**New flow:**
1. All platforms processed → frontend has `romIdToAppId` mapping
2. Backend fetches collection ROMs (deduplicating against platform ROMs)
3. If there are collection-only ROMs (ROMs in collections but not in any
   enabled platform), emit `sync_apply_platform` events for them (grouped
   by their platform). This reuses the existing per-platform processing.
4. Backend emits `sync_apply_collections` with full membership data
5. Frontend translates rom_ids → app_ids, builds Steam collections in one pass

### Preview / Delta Flow

**No changes for now.** The `sync_preview()` + `sync_apply_delta()` flow
continues to use the current `_fetch_and_prepare()` → `_emit_per_platform()`
path. This works fine because preview fetches everything upfront by design.

Follow-up task: unify preview flow with the streaming pipeline.

---

## 5. Frontend Architecture

### State Management

Two separate stores (both module-level, like current `syncProgress.ts`):

#### `syncProgress.ts` — overall sync progress (simplified)

```typescript
interface SyncProgress {
  running: boolean;
  phase?: string;     // "connecting" | "fetching" | "applying" |
                      // "collections" | "removals" | "finalizing" |
                      // "done" | "error" | "cancelled"
  current?: number;   // Overall items processed (during fetch: ROMs; during apply: shortcuts)
  total?: number;     // Overall items total
  message?: string;   // Human-readable status message

  // Fetch-phase progress (shown in header during State 2)
  platformsFetched?: number;   // Platforms whose fetch has completed
  platformsTotal?: number;     // Total platforms

  // ETA (applies globally)
  elapsedSec?: number;
  etaSec?: number | null;

  // Backward compat — kept for subMessage (game title)
  subMessage?: string;
}
```

**Removed fields:** `step`, `totalSteps`, `stepLabel`, `platformCurrent`,
`platformTotal`, `platformLabel` — all replaced by accordion state.

#### `syncAccordion.ts` — NEW: per-platform accordion state

```typescript
type PlatformStatus = "pending" | "applying" | "done" | "partial" | "error";

interface PlatformRow {
  name: string;
  slug: string;
  romCount: number;          // Estimated total ROMs (from sync_plan)
  status: PlatformStatus;
  shortcutsProcessed: number; // How many shortcuts created so far
  shortcutsTotal: number;     // Total shortcuts for this platform
  currentGame?: string;       // Game title currently being processed
}

interface AccordionState {
  platforms: PlatformRow[];      // Full list, fixed order (alphabetical)
  activePlatformIndex: number;   // Which row is expanded (-1 = none)
  collectionsProgress?: { current: number; total: number; label?: string };
  removalsProgress?: { current: number; total: number };
}
```

Functions:

```typescript
function initAccordion(platforms: SyncPlanPlatform[]): void;
function setActivePlatform(index: number): void;
function updatePlatformProgress(name: string, processed: number, total: number, currentGame?: string): void;
function markPlatformDone(name: string): void;
function markPlatformError(name: string): void;
function markPlatformPartial(name: string, processed: number, total: number): void;
function getAccordionState(): AccordionState;
function resetAccordion(): void;
```

### Accordion Component (MainPage.tsx)

The accordion replaces the entire "syncing" section in `MainPage.tsx`.

**Component tree:**

```
<SyncAccordion>
  <FetchHeader />          — visible during "fetching" phase only
  {platforms.map(p =>
    <PlatformRow
      key={p.slug}
      platform={p}
      expanded={p === activePlatform}
    />
  )}
  <TruncationRow />        — visible when > 8 platforms
  <CollectionsFooter />    — visible during "collections" phase
  <RemovalsFooter />       — visible during "removals" phase
  <SyncFooter />           — overall summary + ETA + cancel button
</SyncAccordion>
```

**PlatformRow rendering by status:**

| Status | Icon | Name style | Counter | Expanded? |
|---|---|---|---|---|
| `pending` | `○` (gray) | Dim (0.45 opacity) | ROM count (dim) | No |
| `applying` | `⟳` (white, animated) | Bold white | `87/329` (white) | **Yes** — shows progress bar + game title + ETA |
| `done` | `✓` (green) | Normal (0.7 opacity) | ROM count (dim) | No |
| `partial` | `✗` (yellow) | Normal | `87/329` (dim) | No |
| `error` | `✗` (red) | Normal | `—` | No |

### syncManager.ts Changes

#### New event handlers

```typescript
// In initSyncManager():
addEventListener("sync_plan",             handleSyncPlan);
addEventListener("sync_apply_collections", handleSyncApplyCollections);
```

#### `handleSyncPlan(data: SyncPlanData)`

1. Store platform list → call `initAccordion(data.platforms)`
2. Update `syncProgress.phase = "fetching"`

#### Processing loop changes

The existing `startProcessingLoop()` largely stays the same. Key changes:

1. **On dequeue `sync_apply_platform`:** Before processing shortcuts, call
   `setActivePlatform(platform_index - 1)`. As each shortcut is created,
   call `updatePlatformProgress(name, i, total, game_name)` instead of the
   current `updateSyncProgress()` calls.

2. **After draining artwork for a platform:** Call `markPlatformDone(name)`.

3. **Remove per-platform collection building.** Don't call
   `appendToCollections` or `appendToRomMCollections` per-platform anymore.

4. **On `sync_apply_collections`:** NEW handler. Build ALL collections at
   once from the received membership data:
   ```typescript
   for (const [platformName, appIds] of Object.entries(data.platform_app_ids)) {
     await appendToCollections({ [platformName]: appIds });
   }
   for (const [collName, romIds] of Object.entries(data.collection_memberships)) {
     const appIds = romIds.map(rid => romIdToAppId[String(rid)]).filter(Boolean);
     await appendToRomMCollections({ [collName]: appIds });
   }
   ```

5. **Stale collection cleanup** stays at the end, unchanged.

6. **On cancel:** Call `markPlatformPartial(currentPlatformName, processed, total)`.

### MainPage.tsx Changes

Replace the entire dual-bar syncing section (the `<PanelSectionRow>` with
the step label, overall bar, platform card, sub-message, and ETA row) with
the `<SyncAccordion />` component.

The `SyncAccordion` reads from both `getSyncProgress()` and
`getAccordionState()` on a polling interval (same 250ms `setInterval`
pattern already used).

---

## 6. Data Model — Full Type Definitions

### New Types

```typescript
// Event payloads
interface SyncPlanData {
  platforms: SyncPlanPlatform[];
  has_collections: boolean;
  estimated_total_roms: number;
}

interface SyncPlanPlatform {
  name: string;
  slug: string;
  rom_count: number;
}

interface SyncApplyCollectionsData {
  platform_app_ids: Record<string, number[]>;
  collection_memberships: Record<string, number[]>;
  total_collections: number;
}
```

### Modified Types

```typescript
// SyncApplyPlatformData — remove collection_memberships
interface SyncApplyPlatformData {
  platform_name: string;
  platform_index: number;
  total_platforms: number;
  total_shortcuts_all: number;
  shortcuts_before: number;
  shortcuts: SyncAddItem[];
  changed_shortcuts?: SyncChangedItem[];
  rom_count: number;           // NEW: from platform list
  step: number;                // kept for backward compat (always 0)
  total_steps: number;         // kept for backward compat (always 0)
  // REMOVED: collection_memberships
}
```

### Removed from SyncProgress

```typescript
// These fields are REMOVED:
step?: number;           // → replaced by accordion state
totalSteps?: number;     // → replaced by accordion state
stepLabel?: string;      // → replaced by accordion row status
platformCurrent?: number; // → replaced by accordion platformRow.shortcutsProcessed
platformTotal?: number;   // → replaced by accordion platformRow.shortcutsTotal
platformLabel?: string;   // → replaced by accordion activePlatform.name
```

---

## 7. Implementation Steps

Ordered by dependency. Estimated total: **~600 lines of changes**.

### Phase A: Types & State (no behavior change)

| # | Task | File | Est. |
|---|---|---|---|
| A1 | Add `SyncPlanData`, `SyncPlanPlatform`, `SyncApplyCollectionsData` types | `src/types/index.ts` | 20 lines |
| A2 | Create `syncAccordion.ts` module with state + functions | `src/utils/syncAccordion.ts` | 80 lines |
| A3 | Remove `step`/`totalSteps`/`stepLabel` from `SyncProgress` (keep `platformCurrent`/`platformTotal` for fetch phase) | `src/types/index.ts` | 5 lines |

### Phase B: Backend — streaming per-platform emit

| # | Task | File | Est. |
|---|---|---|---|
| B1 | Add `sync_plan` emission after fetching platform list | `library.py` | 15 lines |
| B2 | Refactor `_do_sync()` to fetch concurrently, emit per-platform in order | `library.py` | 100 lines |
| B3 | Remove `collection_memberships` from `sync_apply_platform` payload | `library.py` | 10 lines |
| B4 | Add `sync_apply_collections` emission after all platforms | `library.py` | 30 lines |
| B5 | Remove `step`/`total_steps`/`step_label` from `_emit_progress()` | `library.py` | 20 lines |
| B6 | Delete `_fetch_and_prepare()` and `_emit_per_platform()` (dead code after B2) | `library.py` | -200 lines |

### Phase C: Frontend — event handling & state

| # | Task | File | Est. |
|---|---|---|---|
| C1 | Register `sync_plan` and `sync_apply_collections` listeners in `initSyncManager()` | `syncManager.ts` | 15 lines |
| C2 | Add `handleSyncPlan()` — init accordion state | `syncManager.ts` | 10 lines |
| C3 | Update platform processing loop to call accordion state functions | `syncManager.ts` | 40 lines |
| C4 | Add `handleSyncApplyCollections()` — build all collections at end | `syncManager.ts` | 30 lines |
| C5 | Remove per-platform `appendToCollections` / `appendToRomMCollections` calls | `syncManager.ts` | -20 lines |
| C6 | Remove `stepLabel`/`step`/`totalSteps` from all `updateSyncProgress()` calls | `syncManager.ts` | -30 lines |

### Phase D: Frontend — accordion UI

| # | Task | File | Est. |
|---|---|---|---|
| D1 | Build `SyncAccordion` component (or inline in MainPage) | `MainPage.tsx` | 200 lines |
| D2 | Replace dual-bar section with accordion | `MainPage.tsx` | -150 lines |
| D3 | Add `sync_plan` listener in `index.tsx` (alongside existing `sync_progress`) | `index.tsx` | 5 lines |

### Phase E: Build, Deploy, Test

| # | Task | Est. |
|---|---|---|
| E1 | `pnpm build` — verify clean build | 1 min |
| E2 | SCP `main.py` + `dist/index.js` to Deck | 1 min |
| E3 | `sudo systemctl restart plugin_loader` | 30s |
| E4 | Run full sync, verify accordion UX | 5 min |
| E5 | Test cancel mid-sync | 2 min |
| E6 | Test with many platforms (14+) | 2 min |

---

## 8. Migration & Backward Compatibility

### What Gets Removed

| Item | Where | Why |
|---|---|---|
| `_fetch_and_prepare()` | `library.py` | Split into smaller methods |
| `_emit_per_platform()` | `library.py` | Inlined into new `_do_sync()` flow |
| `step` / `totalSteps` / `stepLabel` in `_emit_progress()` | `library.py` | 4-phase stepper eliminated |
| `step` / `totalSteps` / `stepLabel` in `SyncProgress` | `types/index.ts` | Replaced by accordion state |
| Dual-bar layout (step label row, platform card, two progress bars) | `MainPage.tsx` | Replaced by accordion |
| Per-platform collection building in processing loop | `syncManager.ts` | Moved to `handleSyncApplyCollections` |

### What Gets Kept

| Item | Where | Why |
|---|---|---|
| Queue mechanism (enqueue/dequeue) | `syncManager.ts` | Works perfectly, no change needed |
| Incremental batch persistence | `syncManager.ts` | Orthogonal to UX changes |
| Heartbeat mechanism | `syncManager.ts` | Orthogonal |
| Artwork pipeline (enqueueArtwork/drainArtwork) | `syncManager.ts` | Orthogonal |
| `sync_apply_removals` / `sync_apply_done` events | Both | Unchanged |
| Preview / delta flow | `library.py` | Deferred to follow-up |
| `AdaptiveSemaphore` for concurrent fetch | `library.py` | Still used for concurrent fetch |
| ETA estimator | `library.py` | Still used |

### Breaking Changes

**None for the user.** This is a UX improvement with no settings changes.

**Protocol changes** (backend ↔ frontend, same plugin):
- New events: `sync_plan`, `sync_apply_collections`
- Removed field: `collection_memberships` from `sync_apply_platform`
- Both files are deployed together (same SCP), so there's no version mismatch risk.

---

## 9. Testing Plan

| Test | What to Verify | How |
|---|---|---|
| **Happy path** | Full sync with 3+ platforms completes; accordion shows each platform progressing → done; collections built | Run sync, watch QAM panel |
| **Cancel mid-platform** | Active platform shows `partial`; pending platforms stay `○`; summary shows partial count | Click cancel during sync |
| **Cancel during fetch** | Accordion shows all `○` pending; no crash | Click cancel during "Fetching library..." phase |
| **Many platforms (14+)** | Truncation works; active platform always visible | Enable 14+ platforms, sync |
| **No collections** | Collections phase skipped; no `sync_apply_collections` event | Disable all collections, sync |
| **Collections only** | Collection-only ROMs processed correctly after platforms | Enable collections with ROMs not in any enabled platform |
| **Network error during fetch** | Error state shown; accordion shows partial progress | Kill RomM mid-sync |
| **Empty platform** | Platform with 0 ROMs shows `✓` immediately (no expanded state) | Enable a platform with 0 ROMs |
| **Single platform** | No truncation needed; accordion shows 1 row | Enable only 1 platform |

---

## 10. Future Optimizations

These are **not in scope** for this plan but noted for later:

1. **Pipelined fetch + apply overlap** — currently the backend emits all
   `sync_apply_platform` events rapidly after the concurrent fetch completes.
   A future optimization could start emitting as each platform's fetch
   completes, overlapping with frontend processing of earlier platforms.

2. **Unify preview/delta flow** — make the preview flow also use the
   streaming per-platform pipeline instead of the monolithic
   `_fetch_and_prepare()`.

3. **Per-platform incremental fetch** — skip fetching unchanged platforms
   (already partially implemented via `_try_incremental_skip`), but in the
   new pipeline, show skipped platforms as instant `✓` in the accordion.

4. **Animated transitions** — smooth expand/collapse animations for
   accordion rows, check mark appearance animations.

5. **Platform icons** — show small platform icons (SNES controller, PS logo,
   etc.) next to platform names in the accordion rows.
