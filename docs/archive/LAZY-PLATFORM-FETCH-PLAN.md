# Lazy Per-Platform ROM Fetching Plan

> **Created:** April 3, 2026
> **Status:** 📋 Plan — awaiting approval
> **Goal:** Stop eagerly fetching ALL ROM titles for ALL platforms/collections up front. Only fetch ROM titles when that specific platform or collection is about to be synced (shortcuts applied).

---

## 1. The Problem

Today, both `_do_sync()` and `_fetch_and_prepare()` follow this flow:

```
Phase 1: Fetch platform list            ← FAST (1–2 sec, metadata only)
Phase 2: asyncio.gather(ALL platforms)  ← SLOW — fetches every ROM for every platform
Phase 3: Fetch ALL collection ROMs      ← SLOW — paginated fetch for every collection
Phase 4: Build shortcuts from ALL ROMs  ← CPU-only, instant
Phase 5: Classify, emit, apply          ← Blocked until Phase 2+3 complete
```

For a library with 14 platforms and ~8,500 ROMs, **Phase 2 alone takes 30–120+ seconds** while the user stares at a spinner. All 8,500 ROM records are fetched and held in memory before a single shortcut is created.

This is profoundly wasteful because:

- The user may only care about syncing a few platforms at a time.
- Even when syncing all platforms, the per-platform shortcut work could start as soon as that one platform's ROMs are fetched — there's no reason to wait for the 13th platform's ROMs before applying the 1st platform's shortcuts.
- The `build_shortcuts_data()` function is a **pure per-ROM transform** — zero cross-platform dependencies.
- The accordion UI already works per-platform — it just needs `(name, slug, rom_count)` from the platform list.

### What Actually Needs All ROMs

Only **two things** require the complete set of ROM IDs across all platforms:

1. **Stale detection** — `current_ids = {sd["rom_id"] for sd in shortcuts_data}; stale = registry - current_ids`. This needs to know every ROM that *should* exist to identify ones that no longer do.
2. **Collection dedup** — `_fetch_collection_roms(platform_rom_ids)` uses the set of already-fetched ROM IDs to avoid double-counting ROMs that belong to both a platform and a collection.

Both of these can be solved without eagerly fetching everything.

---

## 2. Proposed Architecture: Fetch-Apply-Per-Platform Pipeline

### New Flow

```
Phase 1: Fetch platform list                    ← unchanged (fast)
         Emit sync_plan to frontend              ← unchanged

Phase 2: FOR EACH platform (sequentially or bounded-concurrent):
           a) Fetch this platform's ROMs         ← paginated API call
           b) Build shortcuts for this platform  ← pure transform
           c) Accumulate rom_ids into global set ← for stale detection later
           d) Emit sync_apply_platform event     ← frontend starts applying immediately
           e) (Optional) Cache metadata

Phase 3: Fetch collection ROMs                   ← uses accumulated platform_rom_ids
         Emit sync_apply_collections event

Phase 4: Stale reconciliation                    ← compare accumulated IDs vs registry
         Emit sync_apply_removals event

Phase 5: Emit sync_apply_done
```

### Key Differences

| Aspect | Before (Eager) | After (Lazy) |
|---|---|---|
| **When ROMs are fetched** | All at once before any apply | Per-platform, just before that platform's apply |
| **Time to first shortcut** | 30–120s (after ALL fetches) | 2–5s (after first platform fetches) |
| **Memory** | All ROMs in memory simultaneously | Only current platform's ROMs + accumulated ID set |
| **User perception** | Frozen spinner for minutes | Platforms start appearing immediately |
| **Stale detection** | Immediate (has all data) | Deferred to final pass (same accuracy) |
| **Collection dedup** | Immediate | Uses incrementally-accumulated `platform_rom_ids` |

---

## 3. Detailed Task Breakdown

### Task 1: Restructure `_do_sync()` to Fetch-Apply per Platform

**Files:** `py_modules/services/library.py`

**Current code (lines 1248–1268):**
```python
# Phase 2: Concurrent fetch of all platform ROMs
tasks = [
    self._fetch_one_platform(platform, registry, last_sync, sem, progress)
    for platform in platforms
]
results = await asyncio.gather(*tasks)
all_roms = [rom for platform_roms in results for rom in platform_roms]
```

**New approach:**
```python
# Phase 2: Per-platform fetch → build → emit pipeline
all_rom_ids: set[int] = set()
shortcuts_before = 0

for i, platform in enumerate(sorted_platforms):
    self._check_cancelling()

    # 2a. Fetch this platform's ROMs
    platform_roms = await self._fetch_one_platform(
        platform, registry, last_sync, sem, progress
    )

    # 2b. Build shortcut data for this platform only
    platform_shortcuts = self._build_shortcuts_data(platform_roms)

    # 2c. Accumulate IDs for stale detection
    for rom in platform_roms:
        all_rom_ids.add(rom["id"])

    # 2d. Cache metadata
    if self._metadata_service is not None:
        for rom in platform_roms:
            ...

    # 2e. Store in _pending_sync for report_sync_results
    for sd in platform_shortcuts:
        self._pending_sync[sd["rom_id"]] = sd

    # 2f. Emit per-platform event — frontend applies immediately
    await self._emit("sync_apply_platform", {
        "platform_name": platform["name"],
        "platform_index": i + 1,
        "total_platforms": total_platforms,
        "total_shortcuts_all": estimated_total_roms,
        "shortcuts_before": shortcuts_before,
        "shortcuts": platform_shortcuts,
        "rom_count": len(platform_shortcuts),
    })
    shortcuts_before += len(platform_shortcuts)
```

**What this eliminates:**
- The `asyncio.gather` of ALL platforms
- The `all_roms` mega-list
- The separate `_emit_per_platform()` pass (now inline)
- The 30–120s blank wait

**What this preserves:**
- The `_fetch_one_platform()` logic (incremental skip, pagination, etc.)
- The adaptive semaphore (can still use it for bounded concurrency within a platform's pages)
- The metadata caching
- The per-platform accordion events

#### Concurrency Decision

Two sub-options for platform iteration:

**A) Sequential (simpler, recommended for v1):** Process one platform at a time. Each platform's fetch+emit is ~2–8s. User sees platforms appearing one by one. Simple to implement, easy to debug.

**B) Bounded pipeline (future optimization):** Use a producer-consumer pattern where N platforms fetch concurrently, but emit/apply in order. More complex but faster for many small platforms.

**Recommendation: A for this PR.** The user's core complaint is "it fetches everything before doing anything." Sequential per-platform fetch+apply solves that completely. Concurrent fetch is a future optimization if sequential proves too slow.

### Task 2: Restructure `_fetch_and_prepare()` for Preview Flow

**Files:** `py_modules/services/library.py`

The preview flow (`sync_preview()`) calls `_fetch_and_prepare()` which returns `(all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids)`. The preview needs these to compute `_classify_roms()`.

**Options:**

**A) Keep `_fetch_and_prepare()` eager for preview, only make `_do_sync()` lazy.**
- Preview is a less common path — the user explicitly asks for it.
- It needs all data for stale detection counts.
- Simpler to leave preview eager for now.

**B) Make preview lazy too — defer stale counts until after all platforms fetched.**
- Consistent behavior, but more complex.

**Recommendation: A for this PR.** Preview is invoked less often, and users expect it to "think" before showing results. Keep `_fetch_and_prepare()` as-is. The big win is making `_do_sync()` lazy since that's the primary path.

### Task 3: Defer Stale Detection to Final Pass

**Files:** `py_modules/services/library.py`

**Current:** `_classify_roms()` at line 520 computes `stale = registry - current_ids`. This requires `current_ids` to include ALL fetched ROMs.

**New approach:** After the per-platform loop completes, compute stale detection using the accumulated `all_rom_ids`:

```python
# Phase 4: Stale reconciliation (after all platforms + collections processed)
if self._settings.get("remove_on_unsync", True):
    stale_rom_ids = [
        int(rid) for rid in registry
        if int(rid) not in all_rom_ids
    ]
else:
    stale_rom_ids = []

if stale_rom_ids:
    await self._emit("sync_apply_removals", {"remove_rom_ids": stale_rom_ids})
```

**No accuracy loss** — the stale set is identical because `all_rom_ids` is the union of all per-platform fetches, which is exactly what the eager path computed.

### Task 4: Adjust Collection Fetching to Use Accumulated IDs

**Files:** `py_modules/services/library.py`

**Current:** `_fetch_collection_roms(platform_rom_ids)` is called with the complete set of platform ROM IDs computed from `all_roms`.

**New:** `_fetch_collection_roms(all_rom_ids)` — called after the per-platform loop, using the incrementally-accumulated set. **Zero code change needed inside `_fetch_collection_roms()` itself** — it already takes a `set[int]` parameter.

### Task 5: Update SyncAccordion to Handle Inline Events

**Files:** `src/utils/syncAccordion.ts`, `src/utils/syncManager.ts`

**Current:** The frontend receives events in this order:
1. `sync_plan` → renders accordion with all platforms pending
2. `sync_fetch_platform` → marks each platform as "fetching" / "fetched" during Phase 2
3. (long wait while Phase 2+3+4 complete)
4. `sync_apply_platform` → processes shortcuts per platform
5. `sync_apply_collections` / `sync_apply_removals` / `sync_apply_done`

**New:** Events collapse steps 2–4:
1. `sync_plan` → renders accordion (unchanged)
2. For each platform: `sync_apply_platform` arrives with `phase: "fetch_and_apply"` → platform transitions `pending → fetching → applying → done` without a separate fetch event
3. `sync_apply_collections` / `sync_apply_removals` / `sync_apply_done`

**Impact on `syncManager.ts`:** Minimal — it already processes `sync_apply_platform` events in a queue. The only change is that these events arrive sooner (interleaved with fetch) rather than all at once after a long wait.

**Impact on `syncAccordion.ts`:** The `markPlatformFetching()` / `markPlatformFetched()` calls become optional hints. If the backend emits them, great; if it goes straight to `sync_apply_platform`, the accordion should transition directly.

### Task 6: Remove `_emit_per_platform()` Method

**Files:** `py_modules/services/library.py`

This method (lines 1358–1450) exists solely to group `all_roms` by platform and emit events after all data is collected. With the new per-platform pipeline, it's unnecessary — the emit happens inline during the per-platform loop (Task 1).

**Action:** Delete `_emit_per_platform()` and its call site in `_do_sync()`.

### Task 7: Update Performance Instrumentation

**Files:** `py_modules/lib/perf.py`, `py_modules/services/library.py`

**Current:** `time_phase("fetch_roms")` wraps the entire `asyncio.gather`. `time_phase("prepare_shortcuts")` wraps the entire `build_shortcuts_data` call.

**New:** With per-platform fetch+build, instrument per-platform:
```python
with self._perf.time_operation(f"platform:{platform['slug']}"):
    platform_roms = await self._fetch_one_platform(...)
    platform_shortcuts = self._build_shortcuts_data(platform_roms)
```

Also add aggregate phase timing (`time_phase("fetch_and_apply_platforms")`) around the entire loop for the perf report.

### Task 8: Update Tests

**Files:** `tests/services/test_concurrent_fetch.py`, `tests/services/test_library.py`, `tests/services/test_incremental_sync.py`

- `test_concurrent_fetch.py` tests the `asyncio.gather` pattern — update to test sequential per-platform flow.
- `test_library.py` has integration-style sync tests — update expected event order.
- `test_incremental_sync.py` tests the skip-unchanged logic — should be unaffected (works per-platform already).
- Add a new test validating that stale detection produces identical results between the old eager and new lazy approach.

---

## 4. Migration Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Stale detection accuracy** | Low | Mathematically identical — `all_rom_ids` is the same set either way. Add assert in dev mode. |
| **Collection dedup accuracy** | Low | `all_rom_ids` passed to `_fetch_collection_roms` is identical. No change inside that function. |
| **Event ordering change** | Medium | Frontend already handles `sync_apply_platform` events via queue. Events now arrive sooner but in the same format. |
| **Preview flow regression** | None | Preview path (`_fetch_and_prepare`) unchanged in this PR. |
| **Performance regression (sequential vs concurrent)** | Low | Sequential is actually fine: 14 platforms × 3s avg = 42s BUT user sees progress immediately. Before: 42s of nothing + apply. After: first platform in 3s, then steady stream. |
| **Large platforms dominate** | Medium | If one platform has 5,000 ROMs and takes 20s, the user sees it fetching/applying. The accordion shows which one is active. This is strictly better than the current "every platform blocked on the slowest one" behavior. |

---

## 5. What We Are NOT Changing

- `_fetch_and_prepare()` (preview path) — stays eager
- `_fetch_one_platform()` internals — pagination, incremental skip, adaptive semaphore (within a platform)
- `_fetch_collection_roms()` internals — same API, same dedup logic
- `build_shortcuts_data()` — pure function, already works on any subset
- Frontend `SyncAccordion` rendering — same component, events arrive faster
- `syncManager.ts` queue processing — same queue, events arrive sooner
- Collection management (naming, registry, safety caps) — untouched
- Save sync — completely separate system

---

## 6. Expected Outcome

| Metric | Before | After |
|---|---|---|
| **Time to first visible platform** | 30–120s | 2–5s |
| **Peak memory (ROM objects)** | 8,500+ dicts in `all_roms` | ~1 platform's worth (~600) + ID set |
| **User-perceived responsiveness** | Frozen spinner | Platforms stream in one by one |
| **Total sync wall time** | Same | Same or marginally faster (no gather overhead, no redundant grouping pass) |
| **Stale detection accuracy** | 100% | 100% (identical computation, deferred) |
| **Lines changed (est.)** | — | ~150 in library.py, ~20 in syncAccordion.ts, ~50 in tests |

---

## 7. Implementation Order

```
Task 1  ← Core: restructure _do_sync() loop           (largest change, library.py)
Task 3  ← Deferred stale detection                     (small, same file)
Task 4  ← Collection fetch uses accumulated IDs         (one-line change)
Task 6  ← Remove _emit_per_platform()                  (deletion, same file)
Task 7  ← Perf instrumentation update                  (small)
Task 5  ← Frontend accordion adaptation                (small, syncAccordion.ts)
Task 8  ← Test updates                                 (validation)
Task 2  ← (Optional) Lazy preview — deferred to follow-up PR
```
