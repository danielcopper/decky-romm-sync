# Progress Reporting Improvement Plan

> **Goal:** Make every phase of the sync pipeline feel fast, informative, and predictable to the user — even when the underlying work is I/O-bound and variable.
>
> **Prerequisite reading:** [PROGRESS-REPORTING.md](PROGRESS-REPORTING.md) documents the current pipeline in detail.

---

## 🚨 THE #1 PROBLEM: "Fetching ROMs" Is a Dead Zone

**User report:** *"Fetching roms sits and spins for a very long time without conveying any progress or indication about what it is doing."*

This is the single biggest UX failure in the entire sync pipeline. Here's what happens:

1. User presses "Sync Library"
2. UI shows a spinner with `"Fetching platforms..."` (fast, 1-2s)
3. UI shows a spinner with `"Fetching SNES... 423 total (5/14)"` — **and stays there for 30-120+ seconds**
4. The spinner gives no indication of:
   - How many ROMs there are in total across all platforms
   - What percentage is complete
   - How much longer it will take (no ETA)
   - Whether it's actually doing anything or frozen
5. User stares at a message that barely changes, with a generic spinner, having no idea if they should wait 10 seconds or 10 minutes

**This is the LONGEST phase of the entire sync** — for a library with 14 platforms and 8,500 ROMs, the fetch phase can take 60-120 seconds. And for the entire duration, the user gets a spinner with no bar, no percentage, no ETA.

### Root Cause (One Missing Parameter)

In `library.py` line ~883, the progress emission during ROM fetching:

```python
await self._emit_progress(
    "roms",
    current=progress["roms_found"],
    message=f"Fetching {platform_name}... {progress['roms_found']} total ({progress['done']}/{progress['total']})",
    sub_phase=f"platform:{platform_name}",
)
```

**`total=` is never passed.** The frontend receives `total=0`, which triggers the indeterminate spinner path instead of a progress bar. The ETA estimator skips updates because it has no total to work with.

Meanwhile, the data to fix this **already exists**: every platform object returned from `_fetch_enabled_platforms()` has a `rom_count` field. We just never sum them up and pass them as `total`. A ~5 line fix would give users a real progress bar with ETA for the entire fetch phase.

### Why This Matters More Than Anything Else

| Phase | Duration | User sees | Impact |
|---|---|---|---|
| **Fetch platforms** | 1-2s | Spinner + "Fetching platforms..." | Low — fast enough |
| **Fetch ROMs** | **30-120s** | **Spinner only, no bar, no ETA** | **🔴 CRITICAL — longest phase, zero feedback** |
| Fetch collections | 3-10s | Frozen on last fetch message | Medium |
| Download artwork | 10-60s | Progress bar + ETA ✅ | Low — already good |
| Apply shortcuts | 5-30s | Progress bar + step count ✅ | Low — already good |

The fetch phase is **60-70% of total sync time** and it's the only phase with **zero meaningful progress indication**. Everything after it already has bars and ETAs.

---

## Current State: What's Already Good

Before listing all gaps, credit where it's due — the current system has:

- ✅ `_emit_progress` central emitter with phase/step/total structure
- ✅ ETA estimator (EMA-based, from `ETAEstimator` in `perf.py`)
- ✅ `PerfCollector` with HTTP latency, phase timings, counters
- ✅ Frontend ETA display (`formatEta` in MainPage.tsx)
- ✅ Step indicators (`[1/2] Downloading artwork...`)
- ✅ Safety timeout with heartbeat
- ✅ 250ms polling for smooth UI updates

---

## Gap Analysis: What's Broken or Missing

### Gap 1 (CRITICAL): Fetch Phase Is a Black Hole

**The `platforms` and `roms` phases have `total = 0`**, meaning:
- No progress bar (indeterminate spinner only)
- No percentage complete
- ETA estimator has nothing to work with
- User sees `"Fetching SNES... 423 found (5/14)"` but has no sense of how long "5/14" will take
- **This phase is 60-70% of total sync wall-clock time**

**Why it matters:** The fetch phase is often the LONGEST phase (30-120+ seconds depending on library size and network latency). Users stare at a spinner with no indication of how much longer they'll wait. This is the **single most reported frustration** with the sync UX.

**Root cause:** `_emit_progress("roms", ...)` never passes `total=`. We DO know the number of platforms upfront, and each platform announces its `rom_count` in the platform list response — we just don't use it. A 5-line fix would give the entire fetch phase a real progress bar + ETA.

### Gap 2: No Progress During Collection Fetching (Phase 3)

Collection ROM fetching (`_fetch_collection_roms`) emits **zero progress events**. The user sees the last `roms` phase message frozen on screen until artwork starts. For users with many enabled collections, this is a silent multi-second stall.

### Gap 3: "Prepare Shortcuts" Phase Is Invisible

Phase 4 (`_build_shortcuts_data`) runs synchronously with no progress emission. For large libraries (10k+ ROMs), this CPU-bound step takes noticeable time with zero feedback.

### Gap 4: Frontend Shortcut Application Has No Sub-Phase Breakdown

`syncManager.ts` processes three sub-phases sequentially:
1. Add new shortcuts (SteamClient.Apps.AddShortcut)
2. Update changed shortcuts (SetShortcutName, etc.)
3. Batch artwork fetch (getArtworkBase64 + SetCustomArtworkForApp)
4. Remove stale shortcuts

But the user only sees `"Applying shortcuts 47/120"` — there's no distinction between "adding new", "updating changed", and "fetching artwork". The artwork batch fetch is particularly opaque: it happens between shortcut updates and removals with zero progress.

### Gap 5: Indeterminate ↔ Determinate Transitions Are Jarring

The UI switches between:
- **Spinner + text** (phases with `total = 0`: platforms, roms)
- **Progress bar + text** (phases with `total > 0`: artwork, applying)

This switch happens mid-sync with no visual continuity. The spinner suddenly becomes a progress bar at 0%, which feels like a restart rather than forward progress.

### Gap 6: Step Indicator Only Covers Apply Phase

`[1/2] Downloading artwork` and `[2/2] Applying shortcuts` only appear during the apply phase. The fetch phase has no step indicators at all, so the user can't tell "I'm in step 1 of 5 (fetching) before we even get to applying."

### Gap 7: No "Elapsed Time" Displayed

The backend calculates `elapsedSec` and sends it in every progress event, but **the frontend never shows it**. Users have no reference for how long the sync has been running.

### Gap 8: ETA Is Only Available During Stepped Phases

The ETA estimator only updates when `step > 0 and current > 0 and total > 0`. During the entire fetch phase (platforms + ROMs + collections), ETA is never calculated. The fetch phase is where users most want an ETA.

### Gap 9: No Aggregate "Overall Progress" Across All Phases

Each phase has its own `current/total`, but there's no global progress. A sync with 14 platforms, 5000 ROMs, 200 artworks, and 50 removals has no single 0-100% indicator spanning the entire operation.

### Gap 10: Post-Sync Report Is Only in Logs

`PerfCollector.format_report()` produces a detailed text report, but it's only logged. The user never sees sync performance data — how long each phase took, how many HTTP requests were made, what the network latency was.

---

## The Improvement Proposals

### Improvement 1: Platform-Aware Fetch Progress

**Problem:** Gaps 1, 8 — fetch is a black hole with no ETA.

**Solution:** Use the platform list response to calculate a total ROM estimate BEFORE fetching begins, then track actual progress against it.

```
Before:  "Fetching SNES... 423 found (5/14)"     [spinner, no ETA]
After:   "Snes 423/827 · 2,140/8,500 ROMs (5/14)" [bar at 25%, ETA 12s]
```

**Backend changes:**
- After `_fetch_enabled_platforms()`, sum all `platform.rom_count` values → `estimated_total_roms`
- Pass `total=estimated_total_roms` in every `roms` phase emission
- `current` = cumulative ROMs fetched so far (already tracked in `progress["roms_found"]`)
- Feed `ETAEstimator` during the ROM phase (platform-granularity: call `_eta.update(roms_found)` after each platform completes)

**Frontend changes:**
- `roms` phase now has a real `total` → progress bar shows instead of spinner
- ETA works because the estimator has data points

**Risk:** `rom_count` is an estimate (platforms may have changed since the list was fetched). The bar may overshoot or undershoot by a few percent. Acceptable.

### Improvement 2: Collection Fetch Progress

**Problem:** Gap 2 — silent stall during collection fetching.

**Solution:** Add a dedicated `collections` sub-phase with per-collection progress.

```
Before:  [frozen on last roms message for 3-8 seconds]
After:   "Fetching collections 2/5 (Favorites)"   [bar at 40%]
```

**Backend changes:**
- Before `_fetch_collection_roms()`, emit `_emit_progress("collections", current=0, total=len(enabled_ids), message="Fetching collections...")`
- Inside `_fetch_single_collection_roms()`, emit progress after each collection completes
- Use `sub_phase=f"collection:{coll_name}"` for detailed tracking

**Frontend changes:**
- Recognize `phase="collections"` as a determinate phase (has current/total)
- Same rendering as `roms` phase

### Improvement 3: Global Step Indicator Spanning All Phases

**Problem:** Gaps 5, 6, 9 — no overall progress continuity.

**Solution:** Define a master step plan at the START of the sync that covers EVERY phase, not just the apply phase.

```
Current step plan (apply-only):
  [1/2] Downloading artwork    [2/2] Applying shortcuts

Proposed step plan (full sync):
  [1/5] Fetching platforms
  [2/5] Fetching ROMs (14 platforms)
  [3/5] Fetching collections
  [4/5] Downloading artwork
  [5/5] Applying shortcuts
```

**Backend changes:**
- Calculate the full step plan before the first emission:
  ```python
  steps = ["platforms", "roms"]
  if has_collections: steps.append("collections")
  steps.append("prepare")  # may skip if trivial
  if has_artwork: steps.append("artwork")
  steps.append("shortcuts")
  if has_removals: steps.append("removals")
  total_steps = len(steps)
  ```
- Every `_emit_progress` call includes the global `step` and `total_steps`
- The apply phase continues from whatever step number the fetch phase ended on

**Frontend changes:**
- Step indicator `[2/5]` always appears, even during fetch phases
- More importantly: `step/totalSteps` drives a secondary "overall" progress that's always visible

### Improvement 4: Aggregate Progress Bar

**Problem:** Gap 9 — no single overall percentage.

**Solution:** Introduce an `overallProgress` field (0.0–1.0) computed from the global step plan, weighted by expected duration.

```python
# Weight each step by estimated relative duration
STEP_WEIGHTS = {
    "platforms": 0.05,    # Fast API call
    "roms": 0.45,         # Usually the longest phase
    "collections": 0.10,  # Variable
    "prepare": 0.02,      # CPU-only, fast
    "artwork": 0.25,      # Network-bound, per-ROM
    "shortcuts": 0.10,    # SteamClient calls, per-ROM
    "removals": 0.03,     # Fast
}
```

Each phase contributes its `(current/total) * weight` to the overall bar. This gives a single 0-100% bar that advances throughout the entire sync, not resetting between phases.

**Frontend changes:**
- Add a new `overallProgress` field to `SyncProgress`
- Render a persistent top-level `ProgressBarWithInfo` that always shows overall %
- The existing per-phase text updates below it with detail

### Improvement 5: Frontend Shortcut Sub-Phase Breakdown

**Problem:** Gap 4 — shortcut application is opaque.

**Solution:** Break the `syncManager.ts` applying loop into labeled sub-phases.

```
Before:  "Applying shortcuts 47/120"
After:   "[5/6] Adding new shortcuts 12/23"
         "[5/6] Updating shortcuts 5/8"
         "[5/6] Setting artwork 3/31"
         "[6/6] Removing shortcuts 2/4"
```

**Frontend changes (`syncManager.ts`):**
- Use distinct messages for each sub-loop:
  - `"Adding shortcuts {i}/{totalNew}"`
  - `"Updating shortcuts {i}/{totalChanged}"`
  - `"Setting artwork {i}/{artworkTargets.length}"`
  - `"Removing shortcuts {i}/{totalRemovals}"`
- Include `sub_phase` field for each (e.g., `subPhase: "add"`, `"update"`, `"artwork_apply"`, `"remove"`)
- Artwork batch fetch gets its own progress counter instead of being invisible

### Improvement 6: Elapsed Time Display

**Problem:** Gap 7 — elapsed time calculated but never shown.

**Solution:** Show elapsed time in the UI after the sync has been running for a configurable threshold (e.g., 5 seconds).

```
Before:  "[1/2] Downloading artwork 50/396 · 24s"
After:   "[1/2] Downloading artwork 50/396 · 24s left · 18s elapsed"
```

**Frontend changes:**
- `formatProgressText` already appends ETA; add elapsed when `elapsedSec > 5`
- Alternatively, show elapsed in a more subtle secondary line/area rather than appending to the same string

### Improvement 7: Post-Sync Summary Toast

**Problem:** Gap 10 — performance report buried in logs.

**Solution:** After sync completes, show a richer toast and optionally a dismissible summary card.

```
Current toast:  "Sync complete! 396 games added."
Proposed toast: "Sync complete! 396 games · 2m 14s · 23 new, 5 updated, 2 removed"
```

**Backend changes:**
- `report_sync_results` already has all the data. Enrich the `sync_complete` event payload:
  ```python
  {
      "total_games": 396,
      "new_count": 23,
      "changed_count": 5,
      "removed_count": 2,
      "elapsed_sec": 134,
      "platform_count": 14,
  }
  ```

**Frontend changes:**
- `onSyncComplete` in `index.tsx` uses the richer payload for the toast body
- MainPage status line shows a one-line summary that auto-clears after 15 seconds

### Improvement 8: Smooth Spinner-to-Bar Transition

**Problem:** Gap 5 — jarring visual switch from spinner to progress bar.

**Solution:** With Improvement 4's overall progress bar, the indeterminate spinner is replaced by a thin "overall" bar that starts at 0% and always shows. Phase-specific detail appears below it.

**UI layout during sync:**
```
┌────────────────────────────────────────────┐
│  ████████░░░░░░░░░░░░░░░░░░  32%  · 1m 12s│  ← overall progress (always determinate)
│  [2/5] Fetching N64 · 329/8500 ROMs       │  ← per-phase detail text
│  Cancel Sync                               │
└────────────────────────────────────────────┘
```

When the overall bar is present, individual phases don't need their own bars — the single overall bar + descriptive text is cleaner and avoids the jarring switch.

---

## Implementation Options

### **A) Kill the Spinner: Fetch Progress Bar + ETA** (Improvement 1 only)

**The surgical fix for the #1 problem.** Sum platform `rom_count` values before fetching starts, pass as `total` in every `roms` emission, feed the ETAEstimator. Result: the 30-120s fetch phase gets a real progress bar and ETA instead of a dead spinner.

```
Before:  🔄 "Fetching SNES... 423 found (5/14)"     [spinner, no ETA, 90 seconds of nothing]
After:   ████████░░░░░░░░░░░ 25%  "SNES · 2,140/8,500 ROMs (5/14) · ~38s left"
```

**What changes:**
- `_fetch_and_prepare()`: after fetching platforms, sum `platform.rom_count` → `estimated_total_roms`
- Every `_emit_progress("roms", ...)` call gets `total=estimated_total_roms`
- ETAEstimator is fed after each platform completes (already have `_eta.start()` in place)
- Frontend: **zero changes needed** — it already shows a bar when `total > 0`

**Scope:** ~5-10 lines of Python in one function
**Effort:** ~30 minutes
**Files touched:** 1 (library.py)
**Risk:** Near-zero — additive only, no structural changes. `rom_count` is an estimate that may overshoot/undershoot by a few percent, but that's fine.

---

### **B) Fix All Silent Phases** (Improvements 1 + 2 + 3)

Everything in A, plus: add progress to collection fetching (currently a silent multi-second stall after ROM fetch), and unify the entire sync into a global step plan visible from start to finish.

```
Before:  [spinner for 90s] → [frozen for 8s] → [bar appears at 0%]
After:   [1/5] Fetching platforms             ████████████████████ 100%
         [2/5] Fetching ROMs (14 platforms)   ████████░░░░░░░░░░░  25% · ~38s
         [3/5] Fetching collections (2/5)     ████████████░░░░░░░  60%
         [4/5] Downloading artwork 50/396     ████░░░░░░░░░░░░░░░  13% · ~24s
         [5/5] Applying shortcuts 12/50       ████████░░░░░░░░░░░  24%
```

**What changes (on top of A):**
- `_fetch_collection_roms()`: emit `"collections"` phase with `current/total` per-collection
- Compute global step plan before first emission (`["platforms","roms","collections","artwork","shortcuts"]`)
- Every `_emit_progress` call includes global `step` and `total_steps`
- The step indicator `[2/5]` appears in ALL phases, not just apply

**Effort:** ~3-4 hours
**Files touched:** 1-2 (library.py, minor type update in types/index.ts)
**Risk:** Low — all backend changes, frontend already handles step/totalSteps fields

---

### **C) Full Visual Overhaul** (Improvements 1 + 2 + 3 + 4 + 5 + 6 + 8)

Everything in B, plus: persistent overall progress bar, frontend shortcut sub-phase labels, elapsed time display, and smooth visual transitions (no more jarring spinner↔bar switch).

```
┌────────────────────────────────────────────────┐
│  ████████████░░░░░░░░░░░░░░░░  38%  · 1m 12s  │  ← overall progress (always visible)
│  [2/5] Fetching N64 · 2,140/8,500 ROMs · ~38s │  ← per-phase detail
│  Cancel Sync                                   │
└────────────────────────────────────────────────┘
```

**What changes (on top of B):**
- Backend: `overallProgress` (0.0–1.0) computed from weighted step formula
- Frontend: persistent overall `ProgressBarWithInfo` replaces the spinner/bar switch
- Frontend: `syncManager.ts` sub-phase labels ("Adding 12/23", "Setting artwork 3/31", "Removing 2/4")
- Frontend: elapsed time shown after 5s threshold
- No more jarring spinner→bar transition — always a bar

**Effort:** ~8-10 hours
**Files touched:** 4 (library.py, syncManager.ts, MainPage.tsx, types/index.ts)
**Risk:** Medium — MainPage.tsx rendering changes need Deck hardware testing. Weight formula needs tuning.

---

### **D) The Full Package** (All 8 Improvements)

Everything in C, plus enriched post-sync toast with counts/timing and optional summary card.

```
Toast: "Sync complete! 396 games · 2m 14s · 23 new, 5 updated, 2 removed"
```

**Effort:** ~10-13 hours
**Files touched:** 5+ (everything in C + index.tsx)
**Risk:** Medium — same as C plus additional UI surface

---

## Recommendation: **B**

The fetch spinner is so bad that **any option is worth doing**, but here's the analysis:

1. **Option A is a valid "ship it now" fix.** It's 30 minutes and kills the #1 problem. If you want immediate relief, this is it. But the rest of the sync still feels disjointed — silent collection stalls, no step continuity, no overall sense of progress.

2. **Option B is the right balance.** It fixes the fetch black hole (A), eliminates the collection silence, AND gives the user a unified step indicator from start to finish. Every phase says `[2/5] Doing X...` so the user always knows where they are in the overall process. All backend changes, near-zero frontend risk since the existing UI already handles `step/totalSteps` and `total > 0 → bar`. The 3-4 hour investment pays off in a sync that feels coherent instead of fragmented.

3. **Option C is the "premium" version** but the effort doubles (8-10h) for marginal perceptual gain over B. The overall progress bar and elapsed time are nice, but once you have per-phase bars + step indicators + ETA from B, the biggest UX complaints are already solved. C makes sense as a follow-up PR, not the same scope.

4. **Option D** adds polish that belongs in a separate PR.

**Why B over A:** Option A fixes the fetch phase but the user still experiences: a 3-8 second silent stall after fetching (collections), no step indicator during fetch phases, and no sense of "I'm in step 2 of 5 across the whole sync." B solves all three for ~3 extra hours. That's the ROI sweet spot.

**Why B over C:** C's visual redesign (persistent overall bar, elapsed time, syncManager sub-phases) requires MainPage.tsx refactoring that needs Deck hardware testing. B is all backend and can be merged with high confidence. Ship B now, iterate to C later.

**Recommended implementation order within Option B:**
1. **Fetch progress bar** (Improvement 1) — the #1 fix, 30 min, instantly testable
2. **Collection fetch progress** (Improvement 2) — quick win, another 30 min
3. **Global step plan** (Improvement 3) — structural change, ~2-3h, makes the whole sync coherent
4. Test full sync end-to-end, verify no regressions

Each step is independently committable and testable.
