# decky-romm-sync Feature Roadmap

> **Created:** April 5, 2026
> **Status:** Active — this is the single source of truth for all planned work
> **Baseline:** v0.15.0 (released April 1, 2026)
> **PR #214:** Closed (mega-PR). Features cherry-picked into individual PRs below.
>
> **Replaces:** All previously scattered planning docs (archived to `docs/archive/`).

---

## Principles

1. **One feature = one PR.** No mega-PRs. Each PR is 200–800 lines, reviewable in one sitting.
2. **Each feature delivers user value.** No "infrastructure-only" PRs that the user can't see or benefit from.
3. **Test before merge.** Every PR has unit tests (80% coverage gate), a manual smoke test on Deck, and a regression check (full sync cycle).
4. **Measure before and after.** Feature 1 establishes performance instrumentation. Every subsequent feature records before/after numbers.
5. **Ship and stabilize.** After each merge, deploy to Deck and run a full sync before starting the next feature.

---

## Feature Status Key

| Icon | Meaning |
|------|---------|
| ⬜ | Not started |
| 🔨 | In progress |
| ✅ | Merged & deployed |
| 🚫 | Cancelled / descoped |

---

## Phase 1 — Foundation & Safety

*Zero behavioral risk. Measurement, protection, and data integrity. These features make everything that follows safer and measurable.*

### Feature 1: Performance Instrumentation ⬜

**Value:** We can measure before/after for every subsequent feature. Establishes a performance baseline.

**Scope:**
- `PerfCollector` class — phase timing (context manager), HTTP request tracking (method/path/latency/status/bytes), counters, gauges
- `ETAEstimator` class — exponential moving average estimator with configurable alpha, minimum sample threshold, elapsed/items-per-sec properties
- Sync lifecycle hooks: `start_sync()` / `end_sync()` on PerfCollector
- Human-readable performance report logged at INFO level on sync completion
- Wire into `http.py` via `set_perf_collector()` — every API request/download records timing

**Files:** `py_modules/lib/perf.py` (new), `py_modules/adapters/romm/http.py`, `py_modules/bootstrap.py`

**Tests:** PerfCollector lifecycle, phase timing, HTTP tracking, ETAEstimator sample/alpha behavior

**Risk:** None — purely additive, no behavioral changes

**PR size:** ~550 lines

---

### Feature 2: System Collection Protection ⬜

**Value:** Prevents the catastrophic bug that deleted Steam's Favorites/Hidden collections.

**Background:** In April 2026, a bug in collection naming caused `clearAllRomMCollections()` to match ALL collections (including system ones) because `"anything".startsWith("")` is `true` in JS. This corrupted Steam's Library state.

**Scope:**
- `SYSTEM_COLLECTION_IDS` constant set (`favorite`, `hidden`, etc.) in `collections.ts`
- `isRomMCollection(name)` — only matches `"RomM: "` prefix (never empty prefix)
- `drainAndDelete(collectionId)` — refuses to operate on system collections
- `isCollectionSafeToDelete()` exported for syncManager.ts

**Files:** `src/utils/collections.ts`, `src/utils/syncManager.ts`

**Tests:** Unit tests for each guard function, edge cases (empty prefix, system collection names)

**Risk:** None — guard code only, no behavioral changes for correct inputs

**PR size:** ~150 lines

---

### Feature 3: State Backup & Recovery ⬜

**Value:** Prevents total state loss from mid-sync crashes or corruption.

**Scope:**
- Rolling `.prev` state file — on every `save_state()`, rename current `state.json` → `state.json.prev` before writing new
- Empty-registry detection on load — if `shortcut_registry` is empty but `.prev` has entries, auto-recover from `.prev`
- Log a warning on recovery so the user knows it happened

**Files:** `py_modules/adapters/persistence.py`

**Tests:** Normal save/load cycle, recovery from empty state, recovery from corrupted JSON, `.prev` file rotation

**Risk:** Very low — filesystem operations with defensive error handling

**PR size:** ~120 lines

---

### Feature 4: Removal Guard Setting ⬜

**Value:** Users can safely unsync platforms/collections without losing shortcuts. Prevents accidental mass deletion.

**Scope:**
- New `remove_on_unsync` setting (default `true` for backward compatibility)
- Respected in `_classify_roms()` (delta path) — when `false`, stale list is empty
- Respected in `_do_sync()` (full sync) — when `false`, skip removal emission
- `save_remove_on_unsync` callable exposed in `main.py`
- UI toggle in Settings > Advanced: "Remove Shortcuts on Unsync"

**Files:** `py_modules/services/library.py`, `main.py`, `src/components/SettingsPage.tsx`, `src/api/backend.ts`, `src/types/index.ts`

**Tests:** Guard on/off, classification correctness with guard enabled/disabled

**Risk:** Low — additive setting, default preserves existing behavior

**PR size:** ~200 lines

---

## Phase 2 — Performance

*The core performance wins. Each independently measurable against the baseline from Feature 1. Target: full sync 10–15 min → 2–3 min.*

### Feature 5: HTTP Compression + Larger Page Sizes ⬜

**Value:** ~5× fewer HTTP round trips and reduced payload size. Simplest performance win.

**Scope:**
- Send `Accept-Encoding: gzip` header on all API requests in `http.py`
- Transparent decompression (aiohttp handles this automatically)
- Increase page size from 50 → 250 ROMs per API request
- PerfCollector records bytes-before vs bytes-after compression for the report

**Files:** `py_modules/adapters/romm/http.py`

**Tests:** Verify gzip header sent, verify page size parameter, mock compressed responses

**Risk:** Low — RomM server supports gzip; larger pages are a parameter change

**PR size:** ~80 lines

**Expected impact:** Fetch phase HTTP round trips reduced ~5× (e.g., 40 requests → 8 for a 2,000-ROM platform)

---

### Feature 6: Collection List Caching ⬜

**Value:** Collections tab switching becomes instant instead of 3–5s wait each time.

**Scope:**
- `_collections_cache` with 5-minute TTL in LibraryService
- Reused across `get_collections`, `set_all_collections_sync`, `_fetch_collection_roms`
- Both user collections and franchise collections fetched in parallel via `asyncio.gather()`
- Cache invalidated at sync start and on `clear_sync_cache()`

**Files:** `py_modules/services/library.py`

**Tests:** TTL hit/miss/expiry, invalidation on sync start, parallel fetch correctness

**Risk:** Low — cache with TTL, invalidation on mutation

**PR size:** ~150 lines

**Expected impact:** Tab switches 3–5s → <100ms (cache hit)

---

### Feature 7: Concurrent Platform Fetching ⬜

**Value:** The single biggest performance win — 4–8× speedup on the fetch phase which is 60–80% of total sync time.

**Scope:**
- `AdaptiveSemaphore` class — async concurrency limiter that adjusts capacity up/down based on sliding window of task latencies vs configurable thresholds
- Default 4 concurrent platform fetches, range 1–8
- Three isolated (thread-safe, no shared list mutation) methods:
  - `_fetch_one_platform()` — semaphore-bounded dispatcher
  - `_try_incremental_skip_isolated()` — returns `(skipped, roms)` tuple
  - `_full_fetch_platform_roms_isolated()` — paginated fetch with per-platform progress
- Results collected via `asyncio.gather()`, flattened into `all_roms`
- Adaptive concurrency logged: e.g., `Fetch concurrency adapted: 4 → 6 (3 adjustments)`

**Files:** `py_modules/lib/adaptive_semaphore.py` (new), `py_modules/services/library.py`

**Tests:** Acquire/release, concurrency bounds, adaptive adjustment, isolated fetcher correctness, pagination, incremental skip

**Risk:** Moderate — concurrency bugs are subtle. Isolation of each fetch method is critical.

**PR size:** ~500 lines

**Expected impact:** Fetch phase 60–120s → 15–30s

---

### Feature 8: Concurrent Artwork Downloads ⬜

**Value:** 8× speedup on artwork phase. Currently downloads covers one-at-a-time.

**Scope:**
- `AdaptiveSemaphore` (6 default, range 2–12) for artwork downloads
- Replace sequential `for` loop with `asyncio.gather()` over semaphore-bounded tasks
- Each task: skip-if-exists → download → error logging → progress emission
- Adaptive adjustments logged at end of artwork phase

**Files:** `py_modules/services/artwork.py`

**Tests:** Semaphore bounds, skip-if-exists, cancellation, all-fail graceful handling

**Risk:** Moderate — same concurrency concerns as Feature 7, but simpler (no pagination)

**PR size:** ~250 lines

**Expected impact:** Artwork phase 8 min → ~1 min

---

### Feature 9: Shortcut Creation Rate Limiting ⬜

**Value:** Prevents the Steam Library crash caused by creating 600+ shortcuts at once.

**Background:** In April 2026, syncing 662 ROMs as non-Steam shortcuts caused Steam to fire `CAPIJobRequestUserStats` for each shortcut. All fail (non-Steam games have no stats), creating an API storm that crashes the Library page.

**Scope:**
- Rate-limit shortcut creation in `syncManager.ts`: batches of 25 shortcuts, 500ms inter-batch delay
- Between batches, yield to the event loop (`await new Promise(r => setTimeout(r, 500))`)
- Progress updates between batches (not just between individual shortcuts)
- Log batch timing for PerfCollector

**Files:** `src/utils/syncManager.ts`

**Tests:** Batch size enforcement, inter-batch delay, progress emission between batches, cancel mid-batch

**Risk:** Low — throttling only, same shortcuts created, just slower

**PR size:** ~100 lines

**Expected impact:** Prevents crash at 600+ shortcuts; adds ~12s overhead for 600 shortcuts (25 batches × 500ms)

---

## Phase 3 — Sync UX Overhaul

*User-facing improvements that make sync informative and beautiful. The per-platform accordion is the crown jewel — it replaces the abstract 4-phase stepper with something users actually understand.*

### Feature 10: Fetch Phase Progress Bar + ETA ⬜

**Value:** Fixes the #1 UX complaint — the fetch phase (60–80% of sync time) currently shows only an indeterminate spinner. This gives it a real progress bar and ETA.

**Background:** `_emit_progress("roms", ...)` never passes `total=`. The frontend receives `total=0`, triggering the indeterminate spinner. The data to fix this already exists — every platform has a `rom_count` field. A ~5 line backend fix + frontend rendering.

**Scope:**
- Backend: Sum `rom_count` across all enabled platforms, pass as `total` to `_emit_progress` during fetch
- Backend: Pass accumulated `roms_found` as `current` during fetch
- Frontend: `MainPage.tsx` renders determinate progress bar when `total > 0`
- Frontend: ETA display using `ETAEstimator` from Feature 1
- Frontend: Show `"Fetching ROMs — 142 / 3,400 (~2m12s remaining)"` instead of spinner

**Files:** `py_modules/services/library.py` (~5 lines), `src/components/MainPage.tsx` (~20 lines)

**Tests:** Verify `total` is set in progress events during fetch phase

**Risk:** Low — additive UI change, backward compatible

**PR size:** ~80 lines

---

### Feature 11: Per-Platform Sync Accordion ⬜

**Value:** Users see exactly which platforms are done, which is active, and how many remain. Replaces the abstract 4-phase stepper with an intuitive per-platform view.

**Design:** See [UX Reference: Per-Platform Accordion](#ux-reference-per-platform-accordion) below.

**Scope:**
- New `SyncAccordion` React component with platform rows:
  - `○` pending → `⟳` active (expanded, shows progress bar + current title + cover thumbnail) → `✓` done
  - Only one row expanded at a time
  - Truncation for 14+ platforms (collapse distant completed/pending into summary rows)
- New `sync_plan` event from backend — emitted once at sync start with full platform list + ROM counts
- Footer: overall platform count + global ETA
- States: connecting, fetching, applying per-platform, collections phase, removing stale, complete, cancelled, error
- Cover art thumbnail (~40×60px) in expanded row, cycling as artwork downloads complete

**Files:**
- `src/components/SyncAccordion.tsx` (new, ~400 lines)
- `src/utils/syncAccordion.ts` (new, state management ~150 lines)
- `src/components/MainPage.tsx` (swap progress section for accordion)
- `src/types/index.ts` (SyncPlan type, SyncAccordionState)
- `py_modules/services/library.py` (emit `sync_plan` event)

**Tests:** Accordion state transitions, truncation logic, cancel/error states

**Risk:** Low-moderate — frontend-only rendering change, but substantial new component

**PR size:** ~700 lines

---

### Feature 12: Per-Platform Pipeline (Backend) ⬜

**Value:** Platforms appear on the Deck one-by-one during sync instead of all-at-once at the end. If sync is cancelled after 5 of 14 platforms, those 5 are fully usable.

**Scope:**
- Restructure `_do_sync()` from phase-oriented to platform-oriented:
  - Fetch platform list → emit `sync_plan` → for each platform sequentially: fetch ROMs → build shortcuts → emit `sync_apply_platform`
  - Collections built after all platforms (cross-platform concern)
  - Stale reconciliation at the end using accumulated `all_rom_ids`
- New events: `sync_apply_platform` (per-platform), `sync_apply_collections` (once), `sync_apply_removals` (once), `sync_apply_done` (once)
- Remove monolithic `sync_apply` event
- Frontend `syncManager.ts` processes platforms incrementally

**Files:**
- `py_modules/services/library.py` (restructure `_do_sync`, split `_fetch_and_prepare`)
- `src/utils/syncManager.ts` (handle new event types)
- `src/types/index.ts` (new event types)

**Tests:** Per-platform emission order, cancel mid-platform, stale detection accuracy, collection dedup

**Risk:** Moderate — changes the core sync protocol. Requires coordinated backend + frontend changes.

**PR size:** ~600 lines

**Dependency:** Feature 11 (accordion renders per-platform events)

---

### Feature 13: Interleaved Artwork + Incremental Persistence ⬜

**Value:** First shortcut appears within seconds (not after 5–15 min artwork batch). Cancel at any point preserves all work completed so far.

**Background:** Currently, artwork downloads ALL covers BEFORE creating ANY shortcuts. The incremental persistence we built only helps during shortcut creation, but artwork is the bottleneck. Moving artwork into the per-shortcut loop fixes both problems at once.

**Scope:**
- **Interleaved artwork:** Backend includes `cover_url` in each shortcut data item. Frontend calls new `downloadAndGetArtwork(romId)` callable per-shortcut (parallel, 8 concurrent). No more batch artwork phase.
- **Incremental persistence:** Every 10 shortcuts (or every 5s), frontend calls `reportIncrementalResults(batch)`. Backend updates `shortcut_registry` and finalizes cover art paths incrementally. No more all-or-nothing at the end.
- **Unified step numbering:** Remove the step counter reset between preview and apply. Steps count continuously (1/4 → 4/4, never 3/5 → 1/2).
- `reportSyncFinalized()` replaces `reportSyncResults()` for the final pass (collection reconciliation + `last_sync` timestamp only).

**Files:**
- `py_modules/services/artwork.py` (`download_and_get_artwork_base64` method)
- `py_modules/services/library.py` (`_incremental_report_io`, remove artwork phase)
- `py_modules/domain/shortcut_data.py` (add `cover_url` field)
- `main.py` (new callable)
- `src/utils/syncManager.ts` (refactor apply loop, incremental reporting)
- `src/api/backend.ts` (new callable)

**Tests:** Incremental report correctness, artwork cache hit/miss, cancel-at-any-point persistence, step numbering continuity

**Risk:** Moderate-high — touches the core sync data flow across backend and frontend

**PR size:** ~500 lines

**Dependency:** Feature 12 (per-platform pipeline provides the structure)

---

### Feature 14: Lazy Per-Platform Fetching (Optional) ⬜

**Value:** Faster initial load — only fetch ROM data for a platform when it's about to be applied, not all upfront.

**Note:** This is optional because Feature 12 already delivers most of the perceived speed benefit (platforms appear incrementally). Lazy fetching adds marginal improvement by not holding all ROM data in memory. Consider deferring if Features 7+12 make sync fast enough.

**Scope:**
- `_do_sync()` fetches only platform list upfront, not all ROMs
- Per-platform loop: fetch → build → emit (no separate fetch phase)
- Stale detection deferred to final pass using accumulated `all_rom_ids`
- Preview flow (`sync_preview`) remains eager (needs all data for summary)

**Files:** `py_modules/services/library.py`

**Tests:** Memory profile, stale detection accuracy, preview flow unchanged

**Risk:** Moderate — changes fetch flow; preview path must remain eager

**PR size:** ~200 lines

**Dependency:** Feature 12

---

## Phase 4 — Advanced Plugin Features

*The final plugin feature that requires careful re-introduction.*

### Feature 15: Game Detail Page Re-enablement ⬜

**Value:** Custom game detail UI returns — install status, BIOS status, download button, per-game core switching. This was disabled in April 2026 due to a Decky renderer crash.

**Background:** `registerGameDetailPatch()` → `routerHook.addPatch("/library/app/:appid", ...)` caused Decky to re-render ALL routes, crashing Steam's Library page with `GetAppCountWithToolsFilter` TypeError.

**Scope:**
- Deferred registration: don't patch on plugin load. Instead, hook into SteamClient navigation events
- Only register the game detail patch when the user navigates to a game detail page (lazy)
- Unregister on navigate-away to prevent stale patches
- Fallback: if registration crashes, catch the error, log it, and disable the feature (never crash the Library)

**Files:** `src/index.tsx`, `src/components/GameDetailPage.tsx` (or equivalent)

**Tests:** Navigate-to triggers registration, navigate-away triggers cleanup, crash-safe fallback

**Risk:** High — this is the exact feature that caused the Library crash. Requires careful testing in Game Mode.

**PR size:** ~200 lines

---

### Out of Scope (Not Plugin Work)

The following are server-side RomM/LaunchBox work or Deck-side emulator config work. They benefit from the plugin but don't involve plugin code changes. **Tracked in [`games/GAMING-ROADMAP.md`](../../../games/GAMING-ROADMAP.md).**

---

## Implementation Order & Dependencies

```
Phase 1 (Foundation):       1 → 2 → 3 → 4        (sequential, each builds confidence)
Phase 2 (Performance):      5 → 6 → 7 → 8 → 9    (5 first — simplest; 7,8 are the big wins)
Phase 3 (UX Overhaul):      10 → 11 → 12 → 13     (10 is quick; 11+12 are paired; 13 depends on 12)
                            14 — optional, evaluate after 12
Phase 4 (Advanced):         15                      (game detail — high risk, do last)
```

**Critical path:** 1 → 5 → 7 → 10 → 12 → 13 (instrumentation → performance → UX)

**Total plugin PRs:** 15 (plus Feature 14 if needed)

---

## Testing Protocol

Every feature follows this gate process before merge:

| Gate | What | Who |
|------|------|-----|
| **1. Unit tests** | pytest (Python) or Jest (TypeScript), 80% coverage gate | CI (SonarCloud) |
| **2. Type check** | basedpyright (Python), tsc (TypeScript) | CI |
| **3. Lint** | Ruff + import-linter (6 architecture contracts) | CI |
| **4. Build** | `pnpm build` succeeds | CI |
| **5. Deploy to Deck** | `scripts/deploy-to-deck.sh` → restart plugin | Manual |
| **6. Smoke test** | Exercise the specific feature on Deck in Game Mode | Manual |
| **7. Regression** | Full sync cycle, verify no existing functionality broke | Manual |
| **8. Perf measurement** | PerfCollector report before vs after (from Feature 1) | Manual |

---

## UX Reference: Per-Platform Accordion

*Preserved from the design work done in April 2026. This is the target UX for Features 11–13.*

### Layout Anatomy

```
┌─────────────────────────────────────────┐
│  {icon}  {Platform Name}    {counter}   │  ← row (collapsed)
│  {icon}  {Platform Name}    {counter}   │  ← row (collapsed)
│  {icon}  {Platform Name}   {x/total}    │  ← row (EXPANDED)
│     {progress bar}                      │
│     ┌──────┐                            │
│     │cover │  {current game title}      │
│     │ art  │  {ETA}                     │
│     └──────┘                            │
│  {icon}  {Platform Name}    {counter}   │  ← row (collapsed)
│                                         │
│  {footer — overall summary + ETA}       │
│  [ Cancel Sync ]                        │
└─────────────────────────────────────────┘
```

**Row icons:** `○` pending (dim) · `⟳` active (highlighted) · `✓` done (accent) · `✗` cancelled/error

### Key States

**Connecting** (~1s): No accordion yet. Indeterminate spinner + "Connecting to RomM..."

**Fetching Library** (5–20s): All platforms listed as `○` pending. Compact header shows overall fetch progress bar with ROM count (`142 / 3,400 ROMs`).

**Applying Per-Platform** (main state): One platform expanded at a time. Shows:
- Progress bar (shortcuts created / total for this platform)
- Cover art thumbnail (~40×60px) cycling as artwork downloads complete
- Current game title (italic)
- Per-platform ETA
- Footer: `3 of 14 platforms · ~6m remaining`

**Collections Phase**: After all platforms are `✓`. Separate section: `Building collections... 5/8` with progress bar.

**Removing Stale**: `Cleaning up... 12/15` with progress bar.

**Complete**: All rows `✓`. Summary: `✓ Sync complete — 5,320 games · 14 platforms · 8 collections · 6m12s elapsed`

**Cancelled**: Active platform shows partial progress with `✗`. Unprocessed platforms stay `○`. Summary: `⚠ Sync cancelled — 525 of 5,320 · 2 platforms complete`

**Truncation (14+ platforms)**: Show active platform (expanded) + 2 nearest completed + 2 nearest pending. Collapse rest into `✓ (3 platforms complete)` / `○ (6 more platforms)` summary rows.

---

## Archived Documents

The following documents were consolidated into this roadmap and moved to `docs/archive/`:

| Original Location | Content | Absorbed Into |
|---|---|---|
| `docs/DUAL-BAR-PROGRESS-UX.md` | Dual progress bar wireframes (superseded by accordion) | UX Reference section + Feature 11 |
| `docs/SYNC-PROGRESS-FIX-PLAN.md` | Unified steps, interleaved artwork, per-title progress | Features 12, 13 |
| `docs/PROGRESS-REPORTING-IMPROVEMENT-PLAN.md` | Fetch phase black hole analysis + gap analysis | Feature 10 |
| `docs/PROGRESS-REPORTING.md` | Comprehensive progress pipeline reference | Features 10, 11, 12 |
| `docs/PER-PLATFORM-PIPELINE-PLAN.md` | Per-platform accordion UX + event protocol + backend architecture | Features 11, 12 |
| `docs/per-platform-pipeline.md` | Per-platform pipeline overview | Feature 12 |
| `docs/INCREMENTAL-SHORTCUT-PERSISTENCE-PLAN.md` | Incremental persistence root cause + data flow | Feature 13 |
| `docs/LAZY-PLATFORM-FETCH-PLAN.md` | Lazy per-platform fetching plan | Feature 14 |
| `docs/TEST-COVERAGE-ANALYSIS.md` | Test suite audit (1,721 tests, 52 files) | Testing Protocol section |
| `games/decky-romm-sync-performance-plan.md` | 8-PR performance optimization strategy | Phase 2 (Features 5–9) |
| `games/DECKY-OPTIMIZATION-DEPLOYMENT.md` | April 3 deployment report (already shipped) | Historical — completed work |
| `games/sync-progress-split-label-plan.md` | subMessage split plan (superseded by accordion) | Feature 11 |
