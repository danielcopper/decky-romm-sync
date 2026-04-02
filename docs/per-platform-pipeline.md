# Per-Platform Sync Pipeline

> **Created:** April 1, 2026
> **Status:** Implementing

---

## Problem

The sync pipeline currently operates in **phase-oriented** mode. Every step runs across ALL titles before the next step begins:

```
CURRENT (WRONG):

Step 1 — Fetch 500 titles from RomM        ← all platforms at once
Step 2 — Build shortcut data for 500        ← all platforms at once
Step 3 — Create 500 Steam shortcuts         ← all platforms at once
Step 4 — Download artwork for 500           ← all platforms at once
Step 5 — Build collections + finalize       ← all platforms at once

Result: Shortcuts only appear on the Deck AFTER step 5 completes.
If cancelled at any point, ZERO platforms are fully complete.
```

This means:
- The user sees no value until the entire operation finishes
- If the sync is interrupted (cancel, crash, sleep, network drop), nothing is usable
- A 25-platform sync that takes 15 minutes delivers zero value at minute 14

---

## Desired Behavior

The pipeline should operate in **platform-oriented** mode. Each platform goes through ALL steps before the next platform begins:

```
DESIRED (CORRECT):

Xbox (1/5):
  Step 1 — Fetch Xbox titles from RomM
  Step 2 — Build Xbox shortcut data
  Step 3 — Create Xbox Steam shortcuts
  Step 4 — Download Xbox artwork
  Step 5 — Build Xbox collections + persist
  ✅ Xbox is FULLY VISIBLE on the Deck

NES (2/5):
  Step 1 — Fetch NES titles from RomM
  Step 2 — Build NES shortcut data
  Step 3 — Create NES Steam shortcuts
  Step 4 — Download NES artwork
  Step 5 — Build NES collections + persist
  ✅ NES is FULLY VISIBLE on the Deck

SNES (3/5):
  ...
  ✅ SNES is FULLY VISIBLE on the Deck

(and so on for each platform)
```

**Key properties:**
1. After platform X finishes, its shortcuts are visible in Steam with artwork and collections
2. If cancelled after platform X, platforms 1..X are 100% complete and usable
3. Platforms X+1..N are untouched (will be picked up on next sync)
4. Progress shows which platform is being processed and overall progress

---

## Solution Architecture

### Backend (library.py)

The backend fetches all platform data upfront (fast, ~10-30s parallel fetch), then emits
**one event per platform** instead of a single monolithic event:

```
_fetch_and_prepare()          ← unchanged, fast parallel fetch
    │
    ▼
Group shortcuts by platform_name
    │
    ▼
For each platform:
    emit("sync_apply_platform", { platform data })
    await asyncio.sleep(0.05)   ← small delay to prevent event flooding
    │
    ▼
emit("sync_apply_removals", { stale rom_ids })
    │
    ▼
emit("sync_apply_done", {})   ← signals frontend no more events
```

Three new event types replace the single `sync_apply`:
- **`sync_apply_platform`** — one platform's shortcuts + collection memberships
- **`sync_apply_removals`** — stale ROM IDs to remove (cross-platform, at end)
- **`sync_apply_done`** — signals the frontend that all platforms have been emitted

### Frontend (syncManager.ts)

The frontend uses a **queue + processing loop**:

```
Event arrives: sync_apply_platform
    │
    ▼
Push to queue
    │
    ▼
Processing loop (started on first event):
    While queue has items OR not done:
        Dequeue platform
        Create shortcuts  ← immediately visible in Steam
        Drain artwork     ← covers appear on shortcuts
        Build collections ← platform appears in collections
        Flush persistence ← registry updated in backend
        ✅ Platform complete
    │
    ▼
Process removals
    │
    ▼
Finalize (reportSyncFinalized)
```

### Progress Display

```
Step 2 of 3
[████████████░░░░░░░░░░░░░░░░░░] 34%

Xbox (1/5) — Adding 67/100 — Halo 3

156 / 500 • 3m12s elapsed
```

The global progress bar tracks total shortcuts processed across all platforms.
The message shows the current platform name, platform index, and per-title progress.

### Event Payloads

```typescript
interface SyncApplyPlatformData {
  platform_name: string;
  platform_index: number;      // 1-based
  total_platforms: number;
  total_shortcuts_all: number;  // grand total across all platforms
  shortcuts_before: number;     // cumulative count before this platform
  shortcuts: SyncAddItem[];
  changed_shortcuts?: SyncChangedItem[];
  collection_memberships?: Record<string, number[]>;
  step: number;
  total_steps: number;
}

interface SyncApplyRemovalsData {
  remove_rom_ids: number[];
}

interface SyncApplyDoneData {
  total_platforms: number;
  total_shortcuts: number;
  total_removals: number;
}
```

---

## Files Modified

| File | Change |
|---|---|
| `py_modules/services/library.py` | New `_emit_per_platform()` helper; `_do_sync()` and `sync_apply_delta()` use it |
| `src/types/index.ts` | New `SyncApplyPlatformData`, `SyncApplyRemovalsData`, `SyncApplyDoneData` |
| `src/utils/syncManager.ts` | Queue-based per-platform event processing; three event listeners |
| `src/index.tsx` | Updated `initSyncManager()` return type handling |

---

## Why Not Per-Platform Fetch?

The backend fetches all platforms in parallel (~10-30s total). Making the fetch sequential
per-platform would be slower overall (N serial API calls vs N parallel). The 10-30s upfront
fetch is acceptable — the apply phase (minutes) is where per-platform processing matters.

The fetch phase shows "Fetching library..." with its own progress. The per-platform
processing begins immediately after the fetch completes.
