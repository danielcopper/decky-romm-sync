# Progress Reporting in decky-romm-sync

> **Comprehensive reference for the sync progress pipeline** — every event, every phase, every field, from Python backend through EventEmitter to the React frontend.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [The Two Sync Paths](#the-two-sync-paths)
- [Phase Sequence Diagrams](#phase-sequence-diagrams)
  - [Preview + Apply Flow (Default)](#preview--apply-flow-default)
  - [Full Sync Flow (Legacy / Skip-Preview)](#full-sync-flow-legacy--skip-preview)
- [Backend: `_emit_progress` — The Central Emitter](#backend-_emit_progress--the-central-emitter)
  - [Function Signature](#function-signature)
  - [SyncProgress Payload](#syncprogress-payload)
- [Phase-by-Phase Reference](#phase-by-phase-reference)
  - [Phase: `platforms`](#phase-platforms)
  - [Phase: `roms`](#phase-roms)
  - [Phase: `applying` (Artwork Download)](#phase-applying-artwork-download)
  - [Phase: `applying` (Shortcut Application)](#phase-applying-shortcut-application)
  - [Phase: `applying` (Shortcut Removal)](#phase-applying-shortcut-removal)
  - [Phase: `done`](#phase-done)
  - [Phase: `error`](#phase-error)
  - [Phase: `cancelled`](#phase-cancelled)
- [The `sync_apply` Event — Backend→Frontend Handoff](#the-sync_apply-event--backendfrontend-handoff)
- [The `sync_complete` Event — Frontend→Backend Report-Back](#the-sync_complete-event--frontendbackend-report-back)
- [Frontend Progress Rendering](#frontend-progress-rendering)
  - [syncProgress Module Store](#syncprogress-module-store)
  - [MainPage Polling Loop](#mainpage-polling-loop)
  - [Progress Text Formatting](#progress-text-formatting)
  - [Progress Bar Calculation](#progress-bar-calculation)
- [Safety Timeout / Heartbeat Mechanism](#safety-timeout--heartbeat-mechanism)
- [Cancellation Flow](#cancellation-flow)
- [Dynamic Step Planning](#dynamic-step-planning)
- [Complete Event Timeline — Preview + Apply Example](#complete-event-timeline--preview--apply-example)
- [File Reference](#file-reference)

---

## Architecture Overview

Progress reporting flows across three layers:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Python Backend (py_modules/)                                       │
│                                                                     │
│  LibraryService._emit_progress()                                    │
│       │                                                             │
│       ├── Updates self._sync_progress dict (in-memory)              │
│       └── Calls self._emit("sync_progress", payload)                │
│                    │                                                │
│  ArtworkService.download_artwork()                                  │
│       └── Calls emit_progress callback (→ _emit_progress)           │
└────────────────────┬────────────────────────────────────────────────┘
                     │  Decky EventEmitter (WebSocket)
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TypeScript Frontend (src/)                                         │
│                                                                     │
│  index.tsx: addEventListener("sync_progress", handler)              │
│       └── setSyncProgress(data)  →  module-level store              │
│                                                                     │
│  syncManager.ts: addEventListener("sync_apply", handler)            │
│       └── updateSyncProgress()  →  same module-level store          │
│                                                                     │
│  MainPage.tsx: setInterval(250ms) polls getSyncProgress()           │
│       └── Renders ProgressBarWithInfo + text                        │
└─────────────────────────────────────────────────────────────────────┘
```

**Key insight:** There are TWO sources of progress updates:
1. **Backend** emits `sync_progress` events during fetching, artwork download, and completion
2. **Frontend** (`syncManager.ts`) writes directly to the same progress store during the shortcut application phase (because `SteamClient` APIs only exist in the browser runtime)

---

## The Two Sync Paths

### Path A: Preview + Apply (Default)

The user clicks "Sync Library" → backend fetches all data → returns a preview summary → user reviews and clicks "Apply" → apply phase runs. This is the default flow.

| Step | Who | Method |
|------|-----|--------|
| 1. Fetch | Backend | `sync_preview()` → `_fetch_and_prepare()` |
| 2. Preview | Frontend | Displays summary UI (new/changed/removed counts) |
| 3. Apply | Backend | `sync_apply_delta(preview_id)` → artwork + emit `sync_apply` |
| 4. Shortcuts | Frontend | `syncManager.ts` processes `sync_apply` event |
| 5. Report | Frontend→Backend | `reportSyncResults()` → `report_sync_results()` |

### Path B: Full Sync (Skip-Preview or Legacy)

When the "Skip Preview" toggle is ON, or in the legacy `start_sync()` path:

| Step | Who | Method |
|------|-----|--------|
| 1. Fetch | Backend | `_do_sync()` → `_fetch_and_prepare()` |
| 2. Artwork | Backend | `_download_artwork()` |
| 3. Emit | Backend | Emits `sync_apply` with ALL shortcuts |
| 4. Shortcuts | Frontend | `syncManager.ts` processes `sync_apply` event |
| 5. Report | Frontend→Backend | `reportSyncResults()` → `report_sync_results()` |

### Path A+B Shortcut: Auto-Apply

When "Skip Preview" is ON and there ARE changes, `MainPage.tsx` calls `syncPreview()` then immediately calls `syncApplyDelta()` without showing the preview UI, effectively combining both paths.

---

## Phase Sequence Diagrams

### Preview + Apply Flow (Default)

```
TIME ──────────────────────────────────────────────────────────────────►

Backend Phases (sync_preview):
  ┌──────────┐  ┌───────────────────────────┐  ┌──────┐
  │ platforms │  │ roms (per-platform pages)  │  │ done │
  │           │  │                            │  │      │
  │ "Fetching │  │ "Fetching SNES...          │  │ run= │
  │ platforms"│  │  327 found (3/14)"         │  │ false│
  └──────────┘  └───────────────────────────┘  └──────┘

  ← user reviews preview UI →

Backend Phases (sync_apply_delta):
  ┌────────────────────────────┐  ┌───────────────────────┐
  │ applying (artwork DL)      │  │ applying (shortcuts)   │
  │ step=1/N                   │  │ step=2/N               │
  │ "Downloading artwork 3/50" │  │ "Applying shortcuts…"  │
  └────────────────────────────┘  └───────────┬───────────┘
                                               │
                                    sync_apply event
                                               │
Frontend Phases (syncManager.ts):              ▼
  ┌─────────────────────────┐  ┌──────────────────────┐  ┌──────────────┐
  │ applying (new shortcuts) │  │ applying (removals)   │  │ done         │
  │ "Applying 5/23"          │  │ "Removing 2/4"        │  │ run=false    │
  └─────────────────────────┘  └──────────────────────┘  └──────────────┘
                                                            │
                                                    reportSyncResults()
                                                            │
Backend:                                                    ▼
  ┌──────────────┐  ┌──────┐
  │ sync_complete │  │ done │
  │ (collections) │  │      │
  └──────────────┘  └──────┘
```

### Full Sync Flow (Legacy / Skip-Preview)

```
TIME ──────────────────────────────────────────────────────────────────►

Backend Phases (_do_sync):
  ┌──────────┐  ┌──────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
  │ platforms │  │ roms             │  │ applying (artwork)    │  │ applying          │
  │           │  │                  │  │ step=1/N              │  │ (shortcuts msg)   │
  └──────────┘  └──────────────────┘  │ "DL artwork 12/396"   │  │ step=2/N          │
                                      └──────────────────────┘  └────────┬─────────┘
                                                                          │
                                                               sync_apply event
                                                                          │
Frontend Phases (syncManager.ts):                                         ▼
  ┌─────────────────────────┐  ┌───────────────────┐  ┌──────┐
  │ applying (shortcuts)    │  │ applying (removals)│  │ done │
  └─────────────────────────┘  └───────────────────┘  └──────┘
                                                         │
                                                 reportSyncResults()
                                                         │
Backend:                                                 ▼
  ┌──────────────┐  ┌──────┐
  │ sync_complete │  │ done │
  └──────────────┘  └──────┘
```

---

## Backend: `_emit_progress` — The Central Emitter

### Function Signature

```python
async def _emit_progress(
    self,
    phase: str,          # Phase name: "platforms", "roms", "applying", "done", "error", "cancelled"
    current: int = 0,    # Current item index (1-based during iteration)
    total: int = 0,      # Total items in this phase
    message: str = "",   # Human-readable status text
    running: bool = True, # False only for terminal states (done/error/cancelled)
    step: int = 0,       # Current step number in the multi-step plan (e.g., 1)
    total_steps: int = 0, # Total steps in the plan (e.g., 3)
) -> None:
```

**Location:** [py_modules/services/library.py](../py_modules/services/library.py) — `LibraryService._emit_progress()`

### What It Does

1. **Updates `self._sync_progress`** — an in-memory dict that `get_sync_progress()` returns when polled
2. **Emits `"sync_progress"` event** — via the Decky EventEmitter, which pushes to the frontend over WebSocket

### SyncProgress Payload

```python
{
    "running": bool,        # True while sync is active; False = terminal state
    "phase": str,           # One of: "platforms", "roms", "applying", "done", "error", "cancelled"
    "current": int,         # Current progress counter
    "total": int,           # Total items for this phase
    "message": str,         # Human-readable message shown in UI
    "step": int,            # Multi-step indicator (0 = not applicable)
    "totalSteps": int,      # Total steps in plan (0 = not applicable)
}
```

**Note:** The Python dict uses `"totalSteps"` (camelCase) to match the TypeScript interface directly — no snake_case conversion.

---

## Phase-by-Phase Reference

### Phase: `platforms`

**Emitted by:** `_fetch_and_prepare()` — Phase 1 of the fetch pipeline.

| Field | Value |
|-------|-------|
| `phase` | `"platforms"` |
| `current` | `0` |
| `total` | `0` |
| `message` | `"Fetching platforms..."` |
| `running` | `True` |
| `step` | `0` |
| `totalSteps` | `0` |

**When:** At the very start of `_fetch_and_prepare()`, before the API call to `list_platforms`. Emitted exactly **once** per sync.

**Code location:**
```python
# _fetch_and_prepare(), Phase 1
await self._emit_progress("platforms", message="Fetching platforms...")
```

---

### Phase: `roms`

**Emitted by:** `_fetch_and_prepare()`, `_full_fetch_platform_roms()`, and `_try_incremental_skip()` — Phase 2 of the fetch pipeline.

This phase is emitted **multiple times** as ROMs are fetched platform-by-platform, with paginated updates within each platform.

#### Initial emission

| Field | Value |
|-------|-------|
| `phase` | `"roms"` |
| `current` | `0` |
| `total` | `0` |
| `message` | `"Fetching ROMs..."` |
| `running` | `True` |

**When:** Once at the start of the ROM fetching loop.

#### Per-platform start (full fetch)

| Field | Value |
|-------|-------|
| `phase` | `"roms"` |
| `current` | cumulative ROM count so far |
| `total` | `0` (unknown total) |
| `message` | `"Fetching {platform_name}... {count} found ({pi}/{total_platforms})"` |

**When:** Before fetching the first page of ROMs for each platform.

#### Per-page update (full fetch)

Same as above, but `current` increases as each page of ROMs is fetched (page size = 50).

**When:** After each paginated API response within `_full_fetch_platform_roms()`.

#### Incremental skip (unchanged platform)

| Field | Value |
|-------|-------|
| `phase` | `"roms"` |
| `current` | cumulative ROM count (includes reconstructed) |
| `total` | `0` |
| `message` | `"{platform_name} unchanged ({pi}/{total_platforms})"` |

**When:** In `_try_incremental_skip()`, when a platform's `updated_after` check reveals zero changes and the server count matches the registry count. ROMs are reconstructed from the local registry instead of re-fetched.

**Example message sequence for 3 platforms:**
```
"Fetching ROMs..."
"Fetching Super Nintendo... 0 found (1/3)"
"Fetching Super Nintendo... 50 found (1/3)"
"Fetching Super Nintendo... 100 found (1/3)"
"Nintendo 64 unchanged (2/3)"
"Fetching Game Boy Advance... 429 found (3/3)"
"Fetching Game Boy Advance... 479 found (3/3)"
```

---

### Phase: `applying` (Artwork Download)

**Emitted by:** `ArtworkService.download_artwork()` via the `emit_progress` callback, and by the sync orchestrator before starting artwork.

#### Pre-artwork emission (from orchestrator)

| Field | Value |
|-------|-------|
| `phase` | `"applying"` |
| `current` | `0` |
| `total` | number of ROMs needing artwork |
| `message` | `"Downloading artwork 0/{total}"` |
| `step` | current step number (e.g., 1) |
| `totalSteps` | total steps in plan (e.g., 2) |

**When:** In `_do_sync()` or `sync_apply_delta()`, immediately before calling `_download_artwork()`.

#### Per-ROM artwork emission (from ArtworkService)

| Field | Value |
|-------|-------|
| `phase` | `"applying"` |
| `current` | `i + 1` (1-based index) |
| `total` | total ROMs |
| `message` | `"Downloading artwork {i+1}/{total}"` |
| `step` | passed through from `progress_step` parameter |
| `totalSteps` | passed through from `progress_total_steps` parameter |

**When:** Once per ROM inside `ArtworkService.download_artwork()`. Emitted for every ROM whether it needs download or already has cached artwork (the check happens after emission).

**Code location (ArtworkService):**
```python
await emit_progress(
    "applying",
    current=i + 1,
    total=total,
    message=f"Downloading artwork {i + 1}/{total}",
    step=progress_step,
    total_steps=progress_total_steps,
)
```

---

### Phase: `applying` (Shortcut Application)

**Emitted by:** Both the backend and frontend, at different stages.

#### Backend emission (pre-handoff)

| Field | Value |
|-------|-------|
| `phase` | `"applying"` |
| `current` | `0` |
| `total` | number of shortcuts to apply |
| `message` | `"Applying shortcuts 0/{total}"` |
| `step` | next step number (artwork step + 1) |
| `totalSteps` | total steps in plan |

**When:** Just before emitting the `sync_apply` event to hand off to the frontend.

#### Frontend emissions (during processing)

The frontend (`syncManager.ts`) takes over and writes directly to the progress store:

**New shortcuts:**
```typescript
updateSyncProgress({
  running: true, phase: "applying",
  current: i + 1, total: totalShortcuts,
  message: `Applying shortcuts ${i + 1}/${totalShortcuts}`,
  step: currentStep, totalSteps,
});
```

**Changed shortcuts (delta mode):**
```typescript
updateSyncProgress({
  current: idx + 1,
  message: `Updating shortcuts ${idx + 1}/${totalShortcuts}`,
});
```

**When:** Once per shortcut as `SteamClient.Apps.AddShortcut()` / `SetShortcutName()` etc. are called. The frontend inserts a 50ms delay between each shortcut to avoid overwhelming Steam.

---

### Phase: `applying` (Shortcut Removal)

**Emitted by:** Frontend (`syncManager.ts`) during the removal step.

| Field | Value |
|-------|-------|
| `phase` | `"applying"` |
| `current` | `i + 1` |
| `total` | number of stale shortcuts |
| `message` | `"Removing shortcuts {i+1}/{total}"` |
| `step` | current step number |
| `totalSteps` | total steps |

**When:** One per stale ROM during the removal loop. Each removal calls `SteamClient.Apps.RemoveShortcut()`.

---

### Phase: `done`

**Emitted by:** Multiple terminal points — always with `running: False`.

#### After successful sync (from `report_sync_results`)

| Field | Value |
|-------|-------|
| `phase` | `"done"` |
| `current` | total registry size |
| `total` | total registry size |
| `message` | `"Sync complete: {total} games from {N} platforms"` |
| `running` | `False` |

#### After cancelled sync (from `report_sync_results`)

| Field | Value |
|-------|-------|
| `phase` | `"done"` |
| `current` | number processed |
| `total` | total registry size |
| `message` | `"Sync cancelled: {processed} of {total} games processed"` |
| `running` | `False` |

#### After preview ready (from `sync_preview`)

| Field | Value |
|-------|-------|
| `phase` | `"done"` |
| `current` | `0` |
| `total` | `0` |
| `message` | `"Preview ready"` |
| `running` | `False` |

#### Safety timeout auto-complete

| Field | Value |
|-------|-------|
| `phase` | `"done"` |
| `current` | total ROMs from sync_stats |
| `total` | total ROMs from sync_stats |
| `message` | `"Sync complete: {N} games from {M} platforms"` |
| `running` | `False` |

#### Frontend-side done (from `syncManager.ts`)

```typescript
updateSyncProgress({
  running: false,
  phase: "done",
  message: cancelled
    ? `Sync cancelled (${count} processed)`
    : "Sync complete"
});
```

---

### Phase: `error`

**Emitted by:** Exception handlers in `_do_sync()` and `sync_preview()`.

| Field | Value |
|-------|-------|
| `phase` | `"error"` |
| `current` | `0` |
| `total` | `0` |
| `message` | Error message from `classify_error()` |
| `running` | `False` |

**When:** When a non-cancellation exception occurs during fetching or sync. The error message is classified by `classify_error()` into user-friendly text (e.g., "Authentication failed", "Connection refused", "Server error").

**In `_do_sync()`**, there's also a direct `_sync_progress` write (bypassing `_emit_progress`) in the outer exception handler:
```python
self._sync_progress = {
    "running": False,
    "phase": "error",
    "current": 0,
    "total": 0,
    "message": f"Sync failed — {_msg}",
}
self._loop.create_task(self._emit("sync_progress", self._sync_progress))
```

---

### Phase: `cancelled`

**Emitted by:** `_finish_sync()` when cancellation is detected.

| Field | Value |
|-------|-------|
| `phase` | `"cancelled"` |
| `current` | last known current |
| `total` | last known total |
| `message` | `"Sync cancelled"` |
| `running` | `False` |

**When:** Called from `_do_sync()` or `sync_preview()` catch blocks when `SyncState.CANCELLING` triggers an `asyncio.CancelledError`.

---

## The `sync_apply` Event — Backend→Frontend Handoff

This is NOT a progress event — it's a **data transfer event** that triggers the frontend to start applying shortcuts via `SteamClient` APIs.

**Emitted by:** `_do_sync()` and `sync_apply_delta()`

**Payload (`SyncApplyData`):**
```typescript
{
  shortcuts: SyncAddItem[];           // New shortcuts to create
  changed_shortcuts?: SyncChangedItem[]; // Existing shortcuts to update (delta mode only)
  remove_rom_ids: number[];           // ROM IDs to remove
  next_step?: number;                 // Which step number the frontend should start at
  total_steps?: number;               // Total steps in the plan
}
```

**Handler:** `syncManager.ts` — `initSyncManager()` registers an `addEventListener("sync_apply", ...)` handler that:
1. Loops through `shortcuts` → calls `SteamClient.Apps.AddShortcut()` for each
2. Loops through `changed_shortcuts` → calls `SteamClient.Apps.SetShortcutName()` etc.
3. Fetches artwork base64 and applies via `SteamClient.Apps.SetCustomArtworkForApp()`
4. Loops through `remove_rom_ids` → calls `SteamClient.Apps.RemoveShortcut()`
5. Calls `reportSyncResults()` to notify the backend

Throughout steps 1-4, the frontend writes progress updates via `updateSyncProgress()`.

---

## The `sync_complete` Event — Frontend→Backend Report-Back

After the frontend finishes applying shortcuts, it calls `reportSyncResults(romIdToAppId, removedRomIds, cancelled)` which invokes the backend's `report_sync_results()`.

The backend then:
1. Finalizes cover art paths (renames staging files to `{app_id}p.png`)
2. Updates the shortcut registry in persistent state
3. Builds platform and collection app_id mappings
4. Saves `last_sync` timestamp
5. Emits `sync_complete` event back to the frontend with collection data
6. Emits final `"done"` phase progress

The frontend's `onSyncComplete` handler (in `index.tsx`) then:
- Shows a toast notification
- Creates/updates Steam collections (platform groups + RomM collections)
- Cleans stale collections

---

## Frontend Progress Rendering

### syncProgress Module Store

**Location:** [src/utils/syncProgress.ts](../src/utils/syncProgress.ts)

A module-level singleton store (not React state). Three functions:

```typescript
setSyncProgress(p: SyncProgress)     // Full replace (used by backend events)
updateSyncProgress(p: Partial<...>)  // Partial merge (used by syncManager)
getSyncProgress(): SyncProgress      // Read current state (used by polling)
```

**Updated by:**
- `index.tsx` event listener: `addEventListener("sync_progress", (data) => setSyncProgress(data))`
- `syncManager.ts`: `updateSyncProgress({...})` during shortcut processing

### MainPage Polling Loop

**Location:** [src/components/MainPage.tsx](../src/components/MainPage.tsx)

```typescript
const startPolling = (progressOnly = false) => {
  pollRef.current = setInterval(() => {
    const progress = getSyncProgress();  // Cheap read from module store
    setSyncProgress(progress);           // React state update → re-render

    if (!progressOnly && !progress.running) {
      stopPolling();
      setSyncing(false);
      setStatus(progress.message || "Sync finished");
      // Auto-clear status after 8 seconds
      statusTimeoutRef.current = setTimeout(() => setStatus(""), 8000);
      getSyncStats().then(setStats);     // Refresh stats display
    }
  }, 250);  // 250ms polling interval = 4 updates/second
};
```

**Key design choice:** The frontend polls a module-level variable at 250ms intervals instead of using React state subscriptions. This avoids async callable round-trips and works even when the QAM panel is closed and reopened.

### Progress Text Formatting

```typescript
const formatProgressText = (progress: SyncProgress | null): string => {
  if (!progress) return "Syncing...";
  const step = progress.step && progress.totalSteps
    ? `[${progress.step}/${progress.totalSteps}] `
    : "";
  const msg = progress.message || "Syncing...";
  // Truncate to ~40 chars to prevent multi-line jumping in the QAM panel
  const maxLen = 40 - step.length;
  const truncated = msg.length > maxLen
    ? msg.slice(0, maxLen - 1) + "…"
    : msg;
  return step + truncated;
};
```

**Example outputs:**
- `"Fetching platforms..."`
- `"Fetching Super Nintendo... 50 found (2/14)"`
- `"[1/2] Downloading artwork 23/396"`
- `"[2/2] Applying shortcuts 150/396"`

### Progress Bar Calculation

```typescript
const progressFraction = syncProgress?.total
  ? ((syncProgress.current ?? 0) / syncProgress.total) * 100
  : undefined;
```

When `total` is `0` (during the `platforms` and `roms` phases where total is unknown), `progressFraction` is `undefined` and the progress bar shows an indeterminate/spinner state. Once artwork or shortcut application begins with a known total, the bar shows actual percentage.

---

## Safety Timeout / Heartbeat Mechanism

**Problem:** After the backend emits `sync_apply`, the frontend processes shortcuts. If the frontend crashes, disconnects, or the QAM closes, the backend has no way to know sync is stuck — `running` stays `True` forever.

**Solution:** A heartbeat-based safety timeout.

### Backend Side

```python
def _start_safety_timeout(self, heartbeat_timeout_sec=30):
    async def _safety_timeout():
        while self._sync_progress.get("running"):
            await asyncio.sleep(10)                    # Check every 10 seconds
            elapsed = time.monotonic() - self._sync_last_heartbeat
            if elapsed > heartbeat_timeout_sec:        # 30 seconds with no heartbeat
                # Auto-complete the sync
                await self._emit_progress("done", ...)
                self._sync_state = SyncState.IDLE
                return
    self._loop.create_task(_safety_timeout())
```

### Frontend Side

```typescript
// syncManager.ts — inside the shortcut processing loop
if (Date.now() - lastHeartbeat > HEARTBEAT_INTERVAL_MS) {  // 10 seconds
    syncHeartbeat().catch(() => {});    // calls backend sync_heartbeat()
    lastHeartbeat = Date.now();
}
```

The backend's `sync_heartbeat()` resets `_sync_last_heartbeat = time.monotonic()`.

**Timeline:**
- Frontend sends heartbeat every 10s
- Backend checks every 10s
- If 30s pass without a heartbeat → backend auto-completes with "done" phase

---

## Cancellation Flow

```
User clicks "Cancel"
       │
       ▼
MainPage.tsx: requestSyncCancel() + cancelSync()
       │
       ├── requestSyncCancel() → sets _cancelRequested = true in syncManager
       │   └── syncManager loop checks this flag every iteration
       │       └── breaks out → calls reportSyncResults(cancelled=true)
       │
       └── cancelSync() → calls backend cancel_sync()
           └── self._sync_state = SyncState.CANCELLING
               └── _check_cancelling() raises CancelledError in fetch loops
                   └── caught by _do_sync() or sync_preview()
                       └── calls _finish_sync("Sync cancelled")
                           └── emits phase="cancelled", running=False
```

**Two cancellation paths:**
1. **During fetching (backend):** `_check_cancelling()` is called between API requests. It raises `CancelledError` which propagates to `_finish_sync()`.
2. **During shortcut application (frontend):** `_cancelRequested` flag is checked between each shortcut. Frontend breaks out and reports partial results.

---

## Dynamic Step Planning

Steps are calculated dynamically based on what work needs to be done:

```python
# In sync_apply_delta():
apply_steps = []
if has_artwork:    apply_steps.append("artwork")     # step 1
if has_shortcuts:  apply_steps.append("shortcuts")   # step 2
if has_removals:   apply_steps.append("removals")    # step 3
total_steps = len(apply_steps)
```

This means `totalSteps` can be 0, 1, 2, or 3 depending on the delta:
- Nothing to do: `totalSteps = 0`
- Only new shortcuts (no artwork, no removals): `totalSteps = 1`
- Artwork + shortcuts: `totalSteps = 2`
- Artwork + shortcuts + removals: `totalSteps = 3`

The step counter is passed to the frontend via the `sync_apply` event's `next_step` field, so the frontend continues from where the backend left off.

---

## Complete Event Timeline — Preview + Apply Example

A concrete example syncing a library with 2 new ROMs and 1 removal:

```
T=0.0s  [Backend]  sync_preview() called
T=0.0s  [Backend]  emit: phase="platforms", msg="Fetching platforms..."
T=0.3s  [Backend]  emit: phase="roms", msg="Fetching ROMs..."
T=0.5s  [Backend]  emit: phase="roms", msg="Fetching SNES... 0 found (1/2)"
T=0.8s  [Backend]  emit: phase="roms", msg="Fetching SNES... 50 found (1/2)"
T=1.0s  [Backend]  emit: phase="roms", msg="Fetching SNES... 100 found (1/2)"
T=1.2s  [Backend]  emit: phase="roms", msg="N64 unchanged (2/2)"
T=1.3s  [Backend]  emit: phase="done", msg="Preview ready", running=false
T=1.3s  [Backend]  returns preview summary: {new: 2, changed: 0, remove: 1}

        ← User reviews preview →

T=5.0s  [Frontend] User clicks "Apply"
T=5.0s  [Backend]  sync_apply_delta(preview_id) called
T=5.0s  [Backend]  emit: phase="applying", step=1/2, msg="Downloading artwork 0/2"
T=5.2s  [Backend]  emit: phase="applying", step=1/2, msg="Downloading artwork 1/2"
T=5.5s  [Backend]  emit: phase="applying", step=1/2, msg="Downloading artwork 2/2"
T=5.6s  [Backend]  emit: phase="applying", step=2/2, msg="Applying shortcuts 0/2"
T=5.6s  [Backend]  emit: "sync_apply" event → {shortcuts: [...], remove_rom_ids: [42]}
T=5.6s  [Backend]  starts safety timeout (30s heartbeat check)

T=5.7s  [Frontend] syncManager receives "sync_apply"
T=5.7s  [Frontend] updateProgress: phase="applying", step=2/2, msg="Applying shortcuts 1/2"
T=5.8s  [Frontend] SteamClient.Apps.AddShortcut(...)
T=5.9s  [Frontend] updateProgress: msg="Applying shortcuts 2/2"
T=6.0s  [Frontend] SteamClient.Apps.AddShortcut(...)
T=6.5s  [Frontend] Artwork base64 fetch + SetCustomArtworkForApp
T=7.0s  [Frontend] updateProgress: phase="applying", step=2/2, msg="Removing shortcuts 1/1"
T=7.1s  [Frontend] SteamClient.Apps.RemoveShortcut(...)
T=7.2s  [Frontend] reportSyncResults(romIdToAppId, removedIds, false)

T=7.2s  [Backend]  report_sync_results() — finalizes registry
T=7.3s  [Backend]  emit: "sync_complete" → {platform_app_ids: {...}, total_games: 2}
T=7.3s  [Backend]  emit: phase="done", msg="Sync complete: 102 games from 2 platforms", running=false
T=7.3s  [Backend]  SyncState → IDLE

T=7.4s  [Frontend] onSyncComplete → toast notification, collection updates
T=7.5s  [Frontend] updateProgress: phase="done", msg="Sync complete", running=false
T=7.5s  [Frontend] Polling loop detects running=false → stops polling, shows status
```

---

## File Reference

| File | Role in Progress Reporting |
|------|---------------------------|
| [py_modules/services/library.py](../py_modules/services/library.py) | Central sync engine — `_emit_progress()`, `_do_sync()`, `sync_preview()`, `sync_apply_delta()`, `report_sync_results()`, `_start_safety_timeout()` |
| [py_modules/services/artwork.py](../py_modules/services/artwork.py) | `download_artwork()` — calls `emit_progress` callback during artwork download loop |
| [py_modules/domain/sync_state.py](../py_modules/domain/sync_state.py) | `SyncState` enum: `IDLE`, `RUNNING`, `CANCELLING` |
| [src/utils/syncProgress.ts](../src/utils/syncProgress.ts) | Module-level progress store: `setSyncProgress`, `updateSyncProgress`, `getSyncProgress` |
| [src/utils/syncManager.ts](../src/utils/syncManager.ts) | Handles `sync_apply` event — applies shortcuts via SteamClient, writes progress during frontend phase |
| [src/components/MainPage.tsx](../src/components/MainPage.tsx) | Polls progress store at 250ms, renders `ProgressBarWithInfo`, formats text with step indicators |
| [src/index.tsx](../src/index.tsx) | Registers `sync_progress` event listener → `setSyncProgress()`. Registers `sync_complete` handler → toasts + collections |
| [src/types/index.ts](../src/types/index.ts) | `SyncProgress` interface definition |
| [src/api/backend.ts](../src/api/backend.ts) | `syncHeartbeat()`, `reportSyncResults()`, `syncPreview()`, `syncApplyDelta()` callable wrappers |
