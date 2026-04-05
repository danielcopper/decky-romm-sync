# Incremental Shortcut Persistence Plan

> **Problem:** If you sync a 5,000-game library and cancel after an hour, zero shortcuts are persisted. The entire sync is all-or-nothing because the shortcut registry is only written once, at the very end, when `reportSyncResults()` is called.
>
> **Goal:** Persist shortcuts incrementally — as each one is created (or in small batches) — so that cancellation preserves all work completed up to that point.

---

## Table of Contents

- [Root Cause Analysis](#root-cause-analysis)
- [Current Data Flow (The Problem)](#current-data-flow-the-problem)
- [Proposed Data Flow (The Fix)](#proposed-data-flow-the-fix)
- [Design Constraints](#design-constraints)
- [Implementation Plan](#implementation-plan)
  - [Phase 1: Incremental Registry Writes from Frontend](#phase-1-incremental-registry-writes-from-frontend)
  - [Phase 2: Incremental Artwork Finalization](#phase-2-incremental-artwork-finalization)
  - [Phase 3: Cancellation-Aware Finalization](#phase-3-cancellation-aware-finalization)
  - [Phase 4: Collection Membership Deferred Reconciliation](#phase-4-collection-membership-deferred-reconciliation)
- [Detailed File Changes](#detailed-file-changes)
- [Migration & Backward Compatibility](#migration--backward-compatibility)
- [Testing Strategy](#testing-strategy)
- [Risk Assessment](#risk-assessment)
- [Alternatives Considered](#alternatives-considered)

---

## Root Cause Analysis

The problem spans both the Python backend and TypeScript frontend. Here's exactly why cancellation loses all work:

### Backend Side (`library.py`)

1. `_do_sync()` / `sync_apply_delta()` fetches all ROMs, downloads all artwork, builds all shortcut data
2. It emits a **single** `sync_apply` event containing the **entire** shortcuts array (potentially thousands of items)
3. It stores shortcuts in `self._pending_sync` (an in-memory dict) — this is never written to disk
4. The only place `shortcut_registry` (persistent state) gets updated is `_report_sync_results_io()`, which is called by `report_sync_results()` — and that only runs **after the frontend finishes processing ALL shortcuts**

### Frontend Side (`syncManager.ts`)

1. Receives the `sync_apply` event with `data.shortcuts` (full array)
2. Iterates through every shortcut, calling `SteamClient.Apps.AddShortcut()` one at a time
3. Accumulates `romIdToAppId` mapping in a **local variable** (not persisted anywhere)
4. Only after the entire loop finishes (or is cancelled), calls `reportSyncResults(romIdToAppId, removedRomIds, cancelled)`
5. If cancelled, `reportSyncResults` IS called with `cancelled=true` — but only with the shortcuts processed so far

### The Critical Gap

Actually, looking at the cancellation path more carefully:

```typescript
// syncManager.ts lines ~200-220
if (_cancelRequested) {
    logInfo(`Cancel requested after processing ${i + 1}/${totalShortcuts} shortcuts`);
    cancelled = true;
    break;
}
// ... later ...
await reportSyncResults(romIdToAppId, removedRomIds, cancelled);
```

**The frontend DOES call `reportSyncResults` on cancellation** with the partial `romIdToAppId`. So the shortcuts created before cancellation ARE reported back. But there are still problems:

1. **The backend's `_pending_sync` must still be alive** — if the backend times out or resets state, the report-back finds nothing to save
2. **Artwork finalization only happens in `_report_sync_results_io`** — cover art file renames (from temp names to `appId`-based names) only happen in bulk at the end
3. **Collection memberships are all-or-nothing** — `_pending_collection_memberships` is consumed once in `_report_sync_results_io` and cleared
4. **`last_sync` timestamp only updates at the end** — if you cancel 90% through, the next sync will re-fetch everything because `last_sync` wasn't updated
5. **Safety timeout risk** — the heartbeat-based safety timeout (`_start_safety_timeout`) can fire and reset sync state to IDLE before the frontend finishes its long loop, orphaning `_pending_sync`
6. **No incremental state saves** — if the plugin crashes, restarts, or the Deck goes to sleep mid-sync, all accumulated `romIdToAppId` in the frontend's local variable is lost

### The Real User-Facing Problem

The user sees shortcuts being created in real-time (Steam shows them appearing), but:
- If the **plugin crashes or Deck sleeps**, the registry doesn't know about any of them → they become orphaned Steam shortcuts that the plugin can't manage
- The **safety timeout** (30s without heartbeat) can trigger during a long artwork batch, resetting backend state
- Even on clean cancellation, **artwork isn't finalized** for the shortcuts that WERE created — cover art shows as broken/missing
- **Platform collections aren't created** for partially synced data — the user gets shortcuts but no organizational structure

---

## Current Data Flow (The Problem)

```
Backend                              Frontend                        Disk
───────                              ────────                        ────

_do_sync()
  │
  ├── fetch platforms (5-30s)
  ├── fetch ROMs (10-120s)           
  ├── fetch collections (5-30s)
  ├── download artwork (30-600s)
  │
  ├── emit("sync_apply", {
  │     shortcuts: [ALL 5000],       ──→ sync_apply listener
  │     remove_rom_ids: [...]             │
  │   })                                  │
  │                                       ├── for each shortcut:        
  │   _pending_sync = {all 5000}          │   AddShortcut()            → Steam adds shortcut
  │   (IN MEMORY ONLY)                    │   romIdToAppId[id] = appId   (local JS variable)
  │                                       │   delay(50ms)
  │                                       │   ... (this loop takes 5-60+ min for 5000 items)
  │                                       │
  │                                       │   IF CANCEL HERE:
  │                                       │   romIdToAppId has 2500 entries
  │                                       │   but registry has 0
  │                                       │
  │                                       ├── for artwork batch:
  │                                       │   getArtworkBase64() × 5000
  │                                       │   SetCustomArtworkForApp()
  │                                       │
  │                                       ├── for removals:
  │                                       │   RemoveShortcut() × N
  │                                       │
  │                                       └── reportSyncResults(         
  │                                             romIdToAppId,            
  │                                             removedRomIds)           
  │                                             │
  │   ◄────────────────────────────────────────┘
  │
  └── _report_sync_results_io()
        ├── for each rom: update shortcut_registry    ──→ state.json (FIRST WRITE)
        ├── finalize cover art paths
        ├── build collection mappings
        ├── set last_sync timestamp
        └── save_state()                              ──→ state.json (SAVED)
```

**The single point of failure:** Everything between `sync_apply` emission and `reportSyncResults()` completion is transient. If anything interrupts this window (cancel, crash, sleep, timeout), the work is lost or orphaned.

---

## Proposed Data Flow (The Fix)

```
Backend                              Frontend                        Disk
───────                              ────────                        ────

_do_sync()
  │
  ├── fetch platforms
  ├── fetch ROMs
  ├── fetch collections
  ├── download artwork
  │
  ├── emit("sync_apply", {
  │     shortcuts: [ALL 5000],       ──→ sync_apply listener
  │     remove_rom_ids: [...]             │
  │   })                                  │
  │                                       ├── for each shortcut:        
  │   _pending_sync = {all 5000}          │   AddShortcut()            → Steam adds shortcut
  │                                       │   romIdToAppId[id] = appId
  │                                       │
  │                                       │   EVERY 10 SHORTCUTS (or every 5s):
  │                                       │   ├── reportIncrementalResults(batch)
  │   ◄──────────────────────────────────────┘
  │   _incremental_report_io(batch)
  │     ├── update shortcut_registry (10 entries)   ──→ state.json ✅
  │     ├── finalize cover art (10 entries)
  │     └── save_state()
  │   ────────────────────────────────────────►
  │                                       │
  │                                       │   IF CANCEL HERE:
  │                                       │   2500 shortcuts already persisted ✅
  │                                       │   remaining 2500 not yet created
  │                                       │   clean state, nothing orphaned
  │                                       │
  │                                       ├── artwork: interleaved with shortcuts
  │                                       │   (or done in the same incremental batch)
  │                                       │
  │                                       ├── removals (incremental too)
  │                                       │
  │                                       └── reportSyncFinalized(        
  │                                             finalRomIdToAppId,         
  │                                             removedRomIds,
  │                                             cancelled)
  │   ◄────────────────────────────────────────┘
  │
  └── _finalize_sync_io()
        ├── build collection mappings (uses full registry)
        ├── set last_sync timestamp
        ├── emit sync_complete with platform/collection data
        └── save_state()                              ──→ state.json (FINAL)
```

**Key change:** The registry is now updated incrementally during the sync loop, not just at the end. The final `reportSyncFinalized` call only handles collection reconciliation and the `last_sync` timestamp.

---

## Design Constraints

### C1: SteamClient API is Frontend-Only
`SteamClient.Apps.AddShortcut()` only exists in the browser runtime. The backend cannot create shortcuts directly. Therefore, the frontend must remain the driver of the shortcut creation loop.

### C2: `app_id` is Only Known After Creation
Steam assigns the `app_id` when `AddShortcut()` is called. The backend can't predict it. The frontend MUST report each `app_id` back to the backend.

### C3: Artwork Finalization Needs `app_id`
Cover art files are renamed from temp paths to `{app_id}p.png` format. This can only happen after the frontend reports the `app_id`.

### C4: State Writes Must Be Atomic
`state.json` is the persistent store. Writes must be complete (no partial JSON). The existing `save_state()` does atomic write (write to `.tmp`, then `os.replace`), so this is already handled.

### C5: Don't Thrash Disk I/O
Writing `state.json` after every single shortcut (5000 writes) would be excessive. Batching every 10-25 shortcuts or every 5 seconds is the right granularity.

### C6: Collection Mappings Require Full Registry
Platform collections and RomM collection memberships are computed from the complete `shortcut_registry`. These can only be finalized at the end, but that's fine — they're a presentation concern, not a data integrity concern.

### C7: Backward Compatibility
The existing `reportSyncResults` API must continue to work for any frontend that hasn't been updated yet. The new incremental API should be additive.

---

## Implementation Plan

### Phase 1: Incremental Registry Writes from Frontend

**The core fix.** Add a new backend method that the frontend calls every N shortcuts to persist progress.

#### 1.1 New Backend Method: `report_incremental_results`

```python
# library.py — new method on LibraryService

async def report_incremental_results(self, rom_id_to_app_id: dict, removed_rom_ids: list) -> dict:
    """Persist a batch of shortcut results incrementally during sync.
    
    Called by the frontend every BATCH_SIZE shortcuts (e.g., every 10-25).
    Updates shortcut_registry and saves state to disk immediately.
    Does NOT handle collection memberships or last_sync — those are deferred
    to report_sync_finalized().
    
    Returns: {"success": True, "persisted": <count>}
    """
    await self._loop.run_in_executor(
        None, self._incremental_report_io, rom_id_to_app_id, removed_rom_ids
    )
    return {"success": True, "persisted": len(rom_id_to_app_id) + len(removed_rom_ids)}
```

#### 1.2 New Backend IO Helper: `_incremental_report_io`

```python
def _incremental_report_io(self, rom_id_to_app_id: dict, removed_rom_ids: list):
    """Sync helper: update registry + finalize artwork for a batch, then save state."""
    grid = self._steam_config.grid_dir()
    
    for rom_id_str, app_id in rom_id_to_app_id.items():
        pending = self._pending_sync.get(int(rom_id_str), {})
        cover_path = self._finalize_cover_path(grid, pending.get("cover_path", ""), app_id, rom_id_str)
        self._state["shortcut_registry"][rom_id_str] = self._build_registry_entry(
            pending, app_id, cover_path
        )

    for rom_id in removed_rom_ids:
        self._state["shortcut_registry"].pop(str(rom_id), None)

    # Apply Steam Input mode for this batch
    steam_input_mode = self._settings.get("steam_input_mode", "default")
    if steam_input_mode != "default" and rom_id_to_app_id:
        try:
            self._steam_config.set_steam_input_config(
                [int(aid) for aid in rom_id_to_app_id.values()], mode=steam_input_mode
            )
        except Exception as e:
            self._logger.error(f"Failed to set Steam Input config for batch: {e}")

    self._save_state()
```

#### 1.3 Expose in `main.py`

```python
# main.py — new callable

async def report_incremental_results(self, rom_id_to_app_id, removed_rom_ids):
    return await self._sync_service.report_incremental_results(rom_id_to_app_id, removed_rom_ids)
```

#### 1.4 Frontend Callable Declaration

```typescript
// backend.ts — new callable
export const reportIncrementalResults = callable<[Record<string, number>, number[]], { success: boolean; persisted: number }>("report_incremental_results");
```

#### 1.5 Frontend: Batch-Report Loop in `syncManager.ts`

The key change in `syncManager.ts` — report results in batches during the shortcut creation loop:

```typescript
const BATCH_SIZE = 20; // Persist every 20 shortcuts
const BATCH_TIMEOUT_MS = 5000; // Or every 5 seconds, whichever comes first

let batchRomIdToAppId: Record<string, number> = {};
let batchRemovedRomIds: number[] = [];
let lastFlushTime = Date.now();
let totalPersisted = 0;

async function flushBatch(): Promise<void> {
    if (Object.keys(batchRomIdToAppId).length === 0 && batchRemovedRomIds.length === 0) return;
    
    const toSend = { ...batchRomIdToAppId };
    const toRemove = [...batchRemovedRomIds];
    batchRomIdToAppId = {};
    batchRemovedRomIds = [];
    lastFlushTime = Date.now();
    
    try {
        const result = await reportIncrementalResults(toSend, toRemove);
        if (result.success) {
            totalPersisted += result.persisted;
        }
    } catch (e) {
        logError(`Failed to persist batch: ${e}`);
        // Re-add to accumulator so they'll be retried in the next batch
        // or caught by the final reportSyncFinalized
        Object.assign(batchRomIdToAppId, toSend);
        batchRemovedRomIds.push(...toRemove);
    }
}

// Inside the shortcut creation loop:
for (let i = 0; i < data.shortcuts.length; i++) {
    const item = data.shortcuts[i];
    // ... existing AddShortcut logic ...
    
    if (appId) {
        romIdToAppId[String(item.rom_id)] = appId;        // full accumulator (for final report)
        batchRomIdToAppId[String(item.rom_id)] = appId;   // batch accumulator
    }
    
    // Flush batch when threshold reached
    const batchCount = Object.keys(batchRomIdToAppId).length;
    const timeSinceFlush = Date.now() - lastFlushTime;
    if (batchCount >= BATCH_SIZE || timeSinceFlush >= BATCH_TIMEOUT_MS) {
        await flushBatch();
    }
    
    // ... existing delay, heartbeat, cancel check ...
}

// Flush any remaining items before moving to next phase
await flushBatch();
```

#### 1.6 Refactor `report_sync_results` → `report_sync_finalized`

The existing `report_sync_results` becomes a finalization step. Since incremental batches have already persisted most of the registry, the finalizer only needs to:

1. Persist any remaining shortcuts not yet flushed
2. Build collection memberships
3. Set `last_sync` timestamp
4. Emit `sync_complete`

For **backward compatibility**, keep the existing `report_sync_results` method working as-is. The new `report_sync_finalized` is an additional callable the updated frontend uses. Old frontends that still call `report_sync_results` will continue to work (they just won't benefit from incremental persistence).

```python
async def report_sync_finalized(self, remaining_rom_id_to_app_id, removed_rom_ids, cancelled=False):
    """Called after all incremental batches. Handles finalization:
    - Persist any remaining shortcuts not yet in an incremental batch
    - Build platform & collection app_id mappings
    - Set last_sync timestamp
    - Emit sync_complete event
    """
    # First, persist any stragglers (shortcuts added after the last flush)
    if remaining_rom_id_to_app_id or removed_rom_ids:
        await self._loop.run_in_executor(
            None, self._incremental_report_io, remaining_rom_id_to_app_id, removed_rom_ids
        )
    
    # Then run finalization (collections, timestamps)
    platform_app_ids, romm_collection_app_ids = await self._loop.run_in_executor(
        None, self._finalize_sync_io, cancelled
    )
    
    # ... emit sync_complete, update progress, etc. (similar to existing report_sync_results)
```

### Phase 2: Incremental Artwork Finalization

Currently, artwork goes through two stages:
1. **Backend downloads cover art** to temp files named by `rom_id` during the artwork phase
2. **`_report_sync_results_io` renames** them to `{app_id}p.png` after the frontend reports back

With incremental reporting, artwork finalization happens inside `_incremental_report_io` — the `_finalize_cover_path` call already handles the rename. **No separate phase 2 work is needed** — it's already handled by the Phase 1 design above.

The one thing to verify: `_finalize_cover_path` (delegated to `ArtworkService.finalize_cover_path`) must be safe to call multiple times for different batches. Looking at the code, it operates on individual files keyed by `rom_id`, so batch safety is inherent.

### Phase 3: Cancellation-Aware Finalization

When the user cancels mid-sync, the flow should be:

1. Frontend sets `_cancelRequested = true`
2. Current batch loop breaks
3. **Flush any pending batch** (shortcuts created since last flush)
4. Call `reportSyncFinalized` with `cancelled=true`
5. Backend persists the final batch, builds partial collections, sets a conditional `last_sync`

```typescript
// syncManager.ts — cancellation path
if (_cancelRequested) {
    logInfo(`Cancel requested after processing ${i + 1}/${totalShortcuts} shortcuts`);
    cancelled = true;
    await flushBatch(); // ← NEW: persist everything created so far
    break;
}
```

#### On Cancellation, Should We Update `last_sync`?

**No.** If sync was cancelled partway through, we should NOT update `last_sync`. This ensures the next sync will re-fetch everything and detect the shortcuts that were already created (they'll be classified as "unchanged" and skipped). This is the safe choice.

However, we SHOULD persist the shortcuts created so far into the registry — that way:
- The user sees them in Steam immediately
- The next sync classifies them as "unchanged" (no wasted work)
- Artwork is already finalized for them

### Phase 4: Collection Membership Deferred Reconciliation

Collection memberships (platform groups + RomM collections) are computed from the full `shortcut_registry` at sync end. This is correct behavior — partial collection data would be confusing.

On cancellation:
- **Do NOT create/update collections.** The registry is partial — collection memberships would be incomplete and misleading.
- **Do NOT remove existing collections.** They represent the last good sync state.
- Let the next full (non-cancelled) sync rebuild collections from the complete registry.

On successful completion:
- Build collections as before, from the full registry.
- This is unchanged from current behavior.

---

## Detailed File Changes

### `py_modules/services/library.py`

| Change | Lines | Description |
|--------|-------|-------------|
| Add `report_incremental_results()` | New method | Async method: calls `_incremental_report_io` in executor |
| Add `_incremental_report_io()` | New method | Sync helper: batch registry update + artwork finalization + save_state |
| Add `report_sync_finalized()` | New method | Async method: persist stragglers + finalization (collections, timestamp) |
| Add `_finalize_sync_io()` | New method | Sync helper: collection mapping + last_sync + save_state (extracted from `_report_sync_results_io`) |
| Keep `report_sync_results()` | Existing | Unchanged — backward compat for old frontends |
| Keep `_report_sync_results_io()` | Existing | Unchanged — backward compat |

### `main.py`

| Change | Lines | Description |
|--------|-------|-------------|
| Add `report_incremental_results()` | New method | Decky callable → delegates to `self._sync_service.report_incremental_results()` |
| Add `report_sync_finalized()` | New method | Decky callable → delegates to `self._sync_service.report_sync_finalized()` |

### `src/api/backend.ts`

| Change | Description |
|--------|-------------|
| Add `reportIncrementalResults` callable | `callable<[Record<string, number>, number[]], { success: boolean; persisted: number }>` |
| Add `reportSyncFinalized` callable | `callable<[Record<string, number>, number[], boolean], { success: boolean }>` |

### `src/utils/syncManager.ts`

| Change | Description |
|--------|-------------|
| Add batch accumulator variables | `batchRomIdToAppId`, `batchRemovedRomIds`, `lastFlushTime`, `totalPersisted` |
| Add `flushBatch()` helper | Calls `reportIncrementalResults`, handles errors with retry accumulation |
| Modify shortcut creation loop | Add batch threshold check after each shortcut |
| Modify cancellation path | Call `flushBatch()` before breaking |
| Modify removal loop | Accumulate into `batchRemovedRomIds`, flush periodically |
| Change final report | Call `reportSyncFinalized` instead of `reportSyncResults` (with fallback) |
| Interleave artwork with creation | Move `getArtworkBase64` + `SetCustomArtworkForApp` into the per-shortcut loop instead of batching after all shortcuts |

### `src/types/index.ts` (or equivalent)

No type changes needed — the new callables use existing primitive types.

---

## Interleaving Artwork with Shortcut Creation

Currently, the frontend flow is:
1. Create ALL shortcuts (loop 1)
2. Fetch ALL artwork (loop 2)
3. Remove stale shortcuts (loop 3)

This means if you cancel during loop 1, you have shortcuts with no artwork. If you cancel during loop 2, shortcuts exist but some have no covers.

**Proposed change:** Interleave artwork fetching into the shortcut creation loop:

```typescript
for (let i = 0; i < data.shortcuts.length; i++) {
    const item = data.shortcuts[i];
    
    // 1. Create the shortcut
    const appId = await addShortcut(item);
    if (appId) {
        romIdToAppId[String(item.rom_id)] = appId;
        batchRomIdToAppId[String(item.rom_id)] = appId;
        
        // 2. Immediately fetch and apply artwork for THIS shortcut
        try {
            const artResult = await getArtworkBase64(item.rom_id);
            if (artResult.base64) {
                await SteamClient.Apps.SetCustomArtworkForApp(appId, artResult.base64, "png", 0);
            }
        } catch (artErr) {
            logError(`Artwork failed for ${item.name}: ${artErr}`);
        }
    }
    
    // 3. Batch-persist to backend
    if (Object.keys(batchRomIdToAppId).length >= BATCH_SIZE) {
        await flushBatch();
    }
    
    await delay(50);
    // ... heartbeat, cancel check ...
}
```

**Trade-off:** This is slightly slower per-shortcut (artwork fetch is serialized with shortcut creation) but ensures every persisted shortcut also has its artwork. A parallel approach (fire artwork fetch, don't await) could maintain current speed while still getting artwork done:

```typescript
// Fire-and-forget artwork (doesn't block the loop)
if (appId) {
    artworkPromises.push(
        getArtworkBase64(item.rom_id).then(async (artResult) => {
            if (artResult.base64) {
                await SteamClient.Apps.SetCustomArtworkForApp(appId, artResult.base64, "png", 0);
            }
        }).catch((e) => logError(`Artwork failed for ${item.name}: ${e}`))
    );
}
// Limit in-flight artwork promises
if (artworkPromises.length >= ART_CONCURRENCY) {
    await Promise.race(artworkPromises);
    artworkPromises = artworkPromises.filter(p => /* still pending */);
}
```

**Recommendation:** Use the parallel approach. It maintains shortcut creation speed while ensuring artwork is in-flight. The batch persistence includes the `cover_path` from the backend's pre-download, and `_finalize_cover_path` handles the rename when the batch is flushed.

---

## Migration & Backward Compatibility

### Frontend → Backend Version Mismatch

If an old frontend (without incremental reporting) talks to a new backend:
- It calls `reportSyncResults()` as before → works unchanged
- The new `report_incremental_results` and `report_sync_finalized` callables simply go unused

If a new frontend talks to an old backend:
- `reportIncrementalResults()` will throw (callable not found)
- Frontend should **catch and fall back** to the old `reportSyncResults` path:

```typescript
let useIncremental = true;

async function flushBatch(): Promise<void> {
    if (!useIncremental) return; // Fall back to old path
    if (Object.keys(batchRomIdToAppId).length === 0) return;
    
    try {
        await reportIncrementalResults({ ...batchRomIdToAppId }, [...batchRemovedRomIds]);
        batchRomIdToAppId = {};
        batchRemovedRomIds = [];
    } catch (e) {
        logWarn(`Incremental reporting not available, falling back to batch mode: ${e}`);
        useIncremental = false;
        // Don't clear accumulators — they'll be included in the final reportSyncResults
    }
}
```

### State Format

No changes to `state.json` schema. `shortcut_registry` entries have the same shape — they're just written more frequently.

---

## Testing Strategy

### Unit Tests (Python)

| Test | What It Verifies |
|------|-----------------|
| `test_incremental_report_persists_batch` | `_incremental_report_io` updates registry and calls `save_state` |
| `test_incremental_report_finalizes_artwork` | `_finalize_cover_path` is called for each item in the batch |
| `test_incremental_report_steam_input` | Steam Input config is applied per-batch |
| `test_finalize_sync_builds_collections` | `_finalize_sync_io` builds platform + collection mappings from full registry |
| `test_finalize_sync_sets_last_sync` | `last_sync` is set on success, NOT on cancellation |
| `test_incremental_then_finalize_full_flow` | Simulate 3 incremental batches + finalize → registry has all entries |
| `test_incremental_partial_cancel` | 2 batches + cancel → registry has batch 1 + batch 2 entries only |
| `test_backward_compat_report_sync_results` | Old `report_sync_results` still works identically |

### Integration Tests (Frontend mock)

| Test | What It Verifies |
|------|-----------------|
| `test_flush_batch_on_threshold` | `flushBatch` fires every BATCH_SIZE shortcuts |
| `test_flush_batch_on_timeout` | `flushBatch` fires after BATCH_TIMEOUT_MS even if below threshold |
| `test_cancel_flushes_remaining` | Cancellation triggers final `flushBatch` before break |
| `test_fallback_to_old_api` | If `reportIncrementalResults` throws, falls back to `reportSyncResults` |

### Manual Testing

1. Start a large sync (100+ shortcuts)
2. Cancel after ~30s
3. Verify: some shortcuts exist in Steam AND in `state.json` registry
4. Re-sync: verify cancelled shortcuts are classified as "unchanged"
5. Full sync to completion: verify collections are correct

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Increased disk I/O from frequent state saves | Low | Low | Batch size of 20 = ~250 writes for 5000 ROMs. state.json is <1MB. SD card can handle this. |
| Race condition: backend processes batch while frontend creates next shortcut | Low | Low | `_incremental_report_io` runs in executor (separate thread), but only touches `shortcut_registry` which frontend doesn't read during sync. No shared mutable state conflict. |
| Batch flush fails, shortcuts created but not registered | Medium | Medium | Error handling re-accumulates failed items. Final `reportSyncFinalized` catches stragglers. If everything fails, `reportSyncResults` (backward compat) is the safety net. |
| `_pending_sync` is cleared before all incremental batches complete | Low | High | `_pending_sync` is only cleared in `_report_sync_results_io` and `_finalize_sync_io`. Incremental batches read from it but don't clear it. Clear only happens in the final step. |
| Plugin restart mid-sync loses in-flight batch | Medium | Low | Shortcuts created in Steam survive restart. The next sync detects them via `getExistingRomMShortcuts()` and reconciles. This is already the recovery path for any crash. |

---

## Alternatives Considered

### Alternative A: Backend-Driven Chunked `sync_apply` Events

Instead of one `sync_apply` event with all shortcuts, emit multiple `sync_apply` events with chunks of 20-50 shortcuts each.

**Pros:**
- Backend controls the pacing
- Could interleave artwork download with each chunk

**Cons:**
- Major refactor of the event protocol
- Frontend's `sync_apply` listener would need to handle being called multiple times per sync
- `_isSyncRunning` guard would block subsequent events
- Much more complex state management (which chunk are we on? what if a chunk event is lost?)

**Verdict: Rejected.** The frontend-driven incremental approach is simpler and doesn't change the event protocol.

### Alternative B: Write Shortcuts to VDF Directly from Backend

Bypass `SteamClient.Apps.AddShortcut()` entirely. Have the backend write `shortcuts.vdf` directly and tell Steam to reload.

**Pros:**
- Backend controls everything, no frontend loop needed
- Atomic VDF writes = instant persistence

**Cons:**
- Steam doesn't have a reliable "reload shortcuts.vdf" API
- Steam must be restarted for VDF changes to take effect — terrible UX in Game Mode
- `SteamClient.Apps.AddShortcut()` does additional internal registration that VDF writes can't replicate
- `SteamConfigAdapter.write_shortcuts()` is already deprecated in the codebase for this reason

**Verdict: Rejected.** SteamClient API is the only reliable way to create shortcuts in Game Mode.

### Alternative C: Save Full `romIdToAppId` to a Temp File from Frontend

Frontend periodically writes `romIdToAppId` to a file (e.g., `/tmp/sync-progress.json`). If the plugin crashes, it reads this file on restart.

**Pros:**
- Simple, no new backend API needed
- Crash recovery without backend cooperation

**Cons:**
- File I/O from frontend JS is unreliable (Decky sandbox, async issues)
- Doesn't finalize artwork
- Doesn't update the canonical `shortcut_registry`
- Two sources of truth is a recipe for bugs

**Verdict: Rejected.** The backend API approach is cleaner and keeps state management centralized.

---

## Summary

| What | Before | After |
|------|--------|-------|
| When shortcuts are persisted | Once, at the very end | Every 20 shortcuts (or 5 seconds) |
| Cancel after 1 hour of 5000-game sync | 0 shortcuts saved | ~2,500 shortcuts saved |
| Artwork for persisted shortcuts | Not finalized until end | Finalized in each batch |
| Plugin crash mid-sync | All progress lost | Last batch fully persisted |
| Collection memberships | All-or-nothing | Deferred to finalization (same as before, but safe) |
| `last_sync` timestamp | Set even on partial work | Only set on full completion |
| Backward compatibility | N/A | Old frontends work unchanged via `reportSyncResults` |
| New backend methods | 0 | 3 (`report_incremental_results`, `_incremental_report_io`, `report_sync_finalized`) |
| New frontend functions | 0 | 2 (`flushBatch`, `reportIncrementalResults` callable) |
| Estimated implementation effort | — | ~3-4 hours |
