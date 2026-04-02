# Sync Progress Fix Plan — Unified Steps, Interleaved Artwork, Per-Title Progress

> **Created:** July 2025
> **Status:** Implementing
> **Context:** User observed three critical UX issues during sync:
> 1. Step numbering is inconsistent ("Step 3 of 5" jumps to "Step 2 of 2")
> 2. Artwork downloads ALL covers before creating ANY shortcuts (defeats incremental persistence)
> 3. No per-title or per-collection progress display

---

## 🔍 Root Cause Analysis

### Problem 1: Step Numbering Jumps

**How sync works today (skip-preview mode):**

```
syncPreview() calls _fetch_and_prepare():
  Step 1/5: Fetching platforms        ← preliminary_total = fetch_steps + 2
  Step 2/5: Fetching ROMs
  Step 3/5: Fetching collections
  → "Preview ready" (running=false)

syncApplyDelta() starts its OWN step counter:
  Step 1/2: Downloading artwork       ← total_steps = len(apply_steps)
  Step 2/2: Applying shortcuts
```

The user sees **"Step 3 of 5"** then **"Step 1 of 2"** — the counter resets because
`sync_apply_delta()` creates an independent step plan starting from 1. The preview
and apply phases don't share a step counter.

**Root cause:** `sync_apply_delta()` calculates `total_steps = len(apply_steps)` and
counts from 1, ignoring `fetch_step_count` from the preview phase.

### Problem 2: Artwork Downloads Block Shortcut Creation

**Current flow:**

```
Backend (_do_sync or sync_apply_delta):
  1. _fetch_and_prepare()  →  fetches all platforms/ROMs
  2. _download_artwork()   →  downloads ALL covers from RomM (BLOCKING)
  3. emit("sync_apply")    →  tells frontend to create shortcuts

Frontend (syncManager.ts):
  4. For each shortcut:
     a. addShortcut()           →  creates Steam shortcut
     b. getArtworkBase64()      →  reads ALREADY-DOWNLOADED file from disk
     c. SetCustomArtworkForApp  →  sets Steam custom artwork
     d. batch flush every 20    →  persists to registry
```

Step 2 downloads ALL cover images (network calls to RomM) before ANY shortcuts
are created. With 1000+ ROMs, this means 5-15 minutes of "Downloading artwork"
before the user sees any shortcuts appear. If cancelled during artwork download,
zero shortcuts are saved.

The incremental persistence we built (batch flush every 20 shortcuts) only helps
during step 4 — but step 2 is the bottleneck.

**Root cause:** `_download_artwork()` is a batch operation that runs before
`sync_apply` is emitted.

### Problem 3: No Per-Title/Collection Progress

The current progress message is:

```
Applying shortcuts 42/1000
```

The user wants to see:

```
Applying changes 42/1000 — Mario Kart 8 Deluxe
```

**Root cause:** The frontend has `item.name` available in the shortcut loop but
doesn't include it in the progress message.

---

## 📐 Architecture Fix

### Core Change: Move Artwork Download Into Per-Shortcut Loop

Instead of:
```
Download ALL artwork (blocking) → Create ALL shortcuts (incremental)
```

New flow:
```
For each shortcut:
  Create shortcut → Download artwork → Set Steam artwork → Batch flush
```

**How this works:**

1. Backend emits `sync_apply` IMMEDIATELY after fetch (no artwork step)
2. Backend includes `cover_url` in each shortcut data item
3. Frontend processes shortcuts and calls new `downloadAndGetArtwork(romId)` callable
4. This callable downloads ONE cover from RomM (if not cached) and returns base64
5. Frontend sets Steam custom artwork and continues to next shortcut
6. Batch flush every 20 shortcuts persists registry

**Benefits:**
- First shortcut appears within seconds of sync start (no artwork wait)
- Cancel at any point → all processed shortcuts are saved with artwork
- Progress shows real-time title names
- Artwork download parallelism preserved (frontend's ART_CONCURRENCY=8)

### Unified Step Plan

**Before (full sync):**
```
Step 1/5: Fetching platforms
Step 2/5: Fetching ROMs
Step 3/5: Fetching collections
Step 4/5: Downloading artwork        ← REMOVED
Step 5/5: Applying shortcuts
```

**After (full sync):**
```
Step 1/4: Fetching platforms
Step 2/4: Fetching ROMs
Step 3/4: Fetching collections
Step 4/4: Applying changes            ← artwork + shortcuts + removals
```

**Before (delta sync - preview + apply):**
```
Preview:  Step 1/5, 2/5, 3/5
Apply:    Step 1/2, 2/2              ← RESETS!
```

**After (delta sync - preview + apply):**
```
Preview:  Step 1/4, 2/4, 3/4
Apply:    Step 4/4                    ← CONTINUES!
```

**Key changes:**
- `preliminary_total_steps` changes from `fetch_steps + 2` to `fetch_steps + 1`
- `sync_apply_delta` reads `fetch_step_count` from stored delta and continues numbering
- Artwork, shortcuts, and removals are folded into ONE "Applying changes" step
- Frontend no longer increments `currentStep` between phases

### Per-Title Progress Messages

During the apply step, messages show current title:

```
Step 4/4
[============================-----] 42/1000
Applying changes 42/1000 — Mario Kart 8 Deluxe
~3m 12s remaining                    8m 45s elapsed
```

During removals (same step, combined progress):

```
Step 4/4
[================================-] 1008/1015
Removing stale 8/15
~0m 4s remaining                     12m 30s elapsed
```

---

## 📋 Implementation Checklist

### Backend Changes

#### 1. `py_modules/domain/shortcut_data.py`
- [ ] Add `"cover_url"` field to `build_shortcuts_data()` output
  - Source: `rom.get("path_cover_large") or rom.get("path_cover_small") or ""`

#### 2. `py_modules/services/artwork.py`
- [ ] Add `download_and_get_artwork_base64(rom_id, pending_sync)` method
  - Check `existing_cover_path()` for cache hit → return base64
  - If miss, get `cover_url` from `pending_sync[rom_id]["cover_url"]`
  - Download to staging file (`romm_{rom_id}_cover.png`)
  - Update `pending_sync[rom_id]["cover_path"]` with staging path
  - Return `{"base64": "..."}` or `{"base64": None}`

#### 3. `py_modules/services/library.py`

**`_fetch_and_prepare()`:**
- [ ] Change `preliminary_total_steps = len(fetch_steps) + 1` (was +2)

**`sync_preview()`:**
- [ ] Store `fetch_step_count` in `_pending_delta` dict

**`sync_apply_delta()`:**
- [ ] Remove entire artwork download step (the `if has_artwork:` block)
- [ ] Read `fetch_step_count` from delta
- [ ] Calculate `total_steps = fetch_step_count + 1` (one apply step)
- [ ] Set `next_step = fetch_step_count + 1`
- [ ] Include `total_changes` as shortcuts + removals combined

**`_do_sync()`:**
- [ ] Remove entire artwork download step (the `if has_artwork:` block)
- [ ] Simplify to `full_total_steps = fetch_step_count + 1`
- [ ] Remove `cover_paths` logic (cover_url is in shortcut data now)
- [ ] Don't set `sd["cover_path"]` from cover_paths

#### 4. `main.py`
- [ ] Add `async def download_and_get_artwork(self, rom_id)` callable
  - Delegates to `self._artwork_service.download_and_get_artwork_base64()`

### Frontend Changes

#### 5. `src/api/backend.ts`
- [ ] Add `downloadAndGetArtwork` callable declaration

#### 6. `src/utils/syncManager.ts`
- [ ] Replace `getArtworkBase64` with `downloadAndGetArtwork` in `enqueueArtwork()`
- [ ] Add `item.name` to progress messages during shortcut creation
- [ ] Fold removals into the same step number (don't increment `currentStep`)
- [ ] Use combined total for progress bar (totalShortcuts + totalRemovals)
- [ ] Remove `currentStep++` between shortcut and removal phases

#### 7. `src/types/index.ts`
- No changes needed (SyncAddItem already has cover_path)

### Test Updates

#### 8. `tests/test_incremental_sync.py`
- [ ] Update tests that reference artwork download step
- [ ] Verify step numbering expectations match new plan

---

## ⚠️ Edge Cases & Risks

### Artwork-Registry Timing

When the frontend calls `downloadAndGetArtwork()` in parallel (up to 8), artwork
completes asynchronously. When batch flush runs every 20 shortcuts, some items in
the batch may have artwork still in-flight.

**Mitigation:** `_incremental_report_io` uses `pending.get("cover_path", "")`.
If artwork hasn't completed, cover_path is empty — the registry entry is still
saved but without cover_path. The staging file will exist on disk once artwork
completes, and `existing_cover_path()` will find it on next sync. The important
thing is the shortcut is persisted.

Before `reportSyncFinalized()`, the frontend drains all artwork promises
(`await Promise.allSettled(artworkQueue)`), so the final registry update
catches all cover paths.

### Backward Compatibility

- `getArtworkBase64` callable is preserved (not removed)
- `_download_artwork` method is preserved (not removed, just not called in sync flow)
- Old frontend versions that don't call `downloadAndGetArtwork` will still work
  with the backend (they just won't get artwork during sync)

### Cancel Safety

Cancel can happen at any point during the apply step. The existing cancel
mechanism works unchanged:
1. `_cancelRequested` flag is set
2. Next loop iteration checks the flag
3. `flushBatch()` persists any buffered shortcuts
4. `reportSyncFinalized(cancelled=true)` handles cleanup

---

## 📊 Expected Results

| Metric | Before | After |
|---|---|---|
| Time before first shortcut | 5-15 min (all artwork) | ~5 sec (immediate) |
| Cancel safety | Shortcuts only after artwork done | Shortcuts from first batch |
| Step continuity | Jumps (3/5 → 2/2) | Continuous (1/4 → 4/4) |
| Per-title visibility | None | Current title shown |
| Artwork approach | Batch before shortcuts | Per-title, parallel (8 concurrent) |

---

## Files Modified

| File | Changes |
|---|---|
| `py_modules/domain/shortcut_data.py` | Add `cover_url` field |
| `py_modules/services/artwork.py` | Add `download_and_get_artwork_base64` |
| `py_modules/services/library.py` | Remove artwork step, fix step numbering |
| `main.py` | Add `download_and_get_artwork` callable |
| `src/api/backend.ts` | Add callable declaration |
| `src/utils/syncManager.ts` | Refactor apply loop |
| `tests/test_incremental_sync.py` | Update step expectations |
