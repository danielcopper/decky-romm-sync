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

### **A) Targeted Fix: Fetch Progress + Collections Phase** (Improvements 1 + 2)

Fix the two biggest UX pain points: the fetch black hole and the collection stall. Minimal scope, minimal risk, maximum user-facing impact per line of code.

**Scope:**
- Modify `_fetch_and_prepare()`: sum `rom_count` from platforms, pass as `total` in `roms` emissions
- Add `_emit_progress("collections", ...)` calls in `_fetch_collection_roms()`
- Feed ETAEstimator during fetch phase
- Frontend: no changes needed (already handles `total > 0` → bar)

**Effort:** ~2-3 hours
**Files touched:** 1 Python file (library.py)
**Risk:** Very low — additive changes only, no structural refactoring

---

### **B) Full Step Overhaul: Global Steps + Sub-Phases** (Improvements 1 + 2 + 3 + 5)

Fix fetch progress AND unify the entire sync into a single step plan visible from start to finish. Also break down the frontend shortcut phase into labeled sub-phases.

**Scope:**
- Everything in Option A
- Compute global step plan at sync start (platforms→roms→collections→artwork→shortcuts→removals)
- Pass `step/total_steps` in ALL emissions (not just apply phase)
- Refactor `syncManager.ts` to emit sub-phase labels ("Adding", "Updating", "Setting artwork", "Removing")
- Frontend shows `[2/6]` prefix throughout entire sync

**Effort:** ~5-6 hours
**Files touched:** 2 files (library.py, syncManager.ts), minor type changes (index.ts)
**Risk:** Low-medium — step numbering logic needs careful testing across both sync paths (preview+apply and full sync)

---

### **C) Visual Redesign: Overall Progress Bar + Elapsed** (Improvements 1 + 2 + 3 + 4 + 5 + 6 + 8)

The full UX transformation. A persistent overall progress bar replaces the spinner/bar switching, elapsed time is visible, and every phase has a clear visual identity.

**Scope:**
- Everything in Option B
- Add `overallProgress` (0.0-1.0) field computed with weighted step formula
- New frontend layout: persistent overall bar + detail text line (replaces conditional spinner/bar)
- Show elapsed time after 5s threshold
- Smooth transitions between phases (no more jarring spinner→bar switch)

**Effort:** ~8-10 hours
**Files touched:** 3-4 files (library.py, syncManager.ts, MainPage.tsx, types/index.ts)
**Risk:** Medium — MainPage.tsx rendering changes need testing on actual Deck hardware for layout/overflow. Weighted progress formula needs tuning.

---

### **D) The Full Package: Everything + Post-Sync Summary** (All 8 Improvements)

Everything in Option C plus the enriched post-sync toast and performance summary.

**Scope:**
- Everything in Option C
- Enrich `sync_complete` event with counts + timing
- Richer toast message in `index.tsx`
- Optional: dismissible summary card in MainPage showing last sync's performance data

**Effort:** ~10-13 hours
**Files touched:** 5+ files (library.py, syncManager.ts, MainPage.tsx, types/index.ts, index.tsx)
**Risk:** Medium — same as C, plus toast/summary card adds another UI surface to test

---

## Recommendation: **C**

Option C is the sweet spot. Here's why:

1. **Option A is too narrow.** It fixes the worst gap (fetch progress) but leaves the overall experience fragmented — phases still have no continuity, the spinner/bar switch persists, and the frontend shortcut phase stays opaque. The user still can't answer "how far through the whole sync am I?"

2. **Option B is the logical minimum** but stops short of the visual payoff. You fix the data pipeline (correct totals, global steps, sub-phases) but the UI still has the jarring spinner→bar transition and no overall indicator. You do all the hard backend work without the frontend payoff.

3. **Option C delivers the complete visual transformation.** The persistent overall progress bar is the single change that makes the biggest perceptual difference — users always see forward motion, always know the percentage, and never experience the "did it freeze?" moment. Combined with the global step plan and sub-phase labels, every second of the sync is accounted for in the UI.

4. **Option D adds nice-to-haves but not essentials.** The enriched toast is a 30-minute cherry on top that can be done as a follow-up. It doesn't justify scoping it into the same PR.

**Recommended implementation order within Option C:**
1. Backend: Platform-aware fetch progress (Improvement 1) — unlocks everything else
2. Backend: Collection fetch progress (Improvement 2) — quick win
3. Backend: Global step plan (Improvement 3) — structural change
4. Frontend: Sub-phase labels in syncManager.ts (Improvement 5)
5. Backend: Overall progress calculation (Improvement 4)
6. Frontend: New MainPage layout with overall bar + elapsed (Improvements 6 + 8)
7. Test on Deck hardware, tune weights, verify QAM panel layout

Each step is independently committable and testable.
