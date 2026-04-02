# Sync Progress UX: Dual Progress Bars + Descriptive Step Labels

> **Created:** April 2, 2026
> **Status:** ✅ Implemented and deployed
> **Supersedes:** Single-bar progress with bare "Step X of Y" labels

---

## Overview

The sync progress UI uses **two progress bars** and **descriptive step labels** to
give the user a clear picture of both overall sync progress and the current
unit of work (platform / collection) at all times.

### Before vs After

| Element | Old UI | New UI |
|---|---|---|
| Step indicator | `Step 2 of 4` (dim, top) | **"Fetching ROMs"** bold white + `Step 2/4` right-aligned dim |
| Progress bars | 1 bar for everything | **2 bars** — overall (always) + current-unit card (conditional) |
| Platform context | Embedded in message text | Dedicated card with name + unit counter + its own bar |
| Numeric progress | Mixed into message | Dedicated line below overall bar: `142 / 3,400` |
| Game title | Same styling as message | Italic, dimmer, clearly subordinate |
| ETA row | 12px | 11px, 0.4 opacity — more subdued |

---

## Wireframes

### State 1: Step 1 — Connecting (indeterminate)

```
┌─────────────────────────────────────────────┐
│ Connecting                      Step 1/4    │
│ ░░░░░░░░░░░░░▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░ │  ← indeterminate
│                                             │
│                                             │
│                                             │
│ [ Cancel Sync ]                             │
└─────────────────────────────────────────────┘
```

No platform card. No ETA (nothing to estimate yet).

### State 2: Step 2 — Fetching ROMs (with platform card)

```
┌─────────────────────────────────────────────┐
│ Fetching ROMs                   Step 2/4    │  ← bold white + dim counter
│ ████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │  ← overall bar (142/3400 ROMs)
│ 142 / 3,400                                 │  ← numeric context
│ ┌─────────────────────────────────────────┐ │
│ │ GameCube                        3/14    │ │  ← platform label + unit counter
│ │ ██████████████████░░░░░░░░░░░░░░░░░░░░ │ │  ← platform progress bar
│ └─────────────────────────────────────────┘ │
│                                             │
│ ~2m12s remaining              38s elapsed   │  ← dim ETA row
│ [ Cancel Sync ]                             │
└─────────────────────────────────────────────┘
```

- **Overall bar**: ROMs fetched across all platforms / estimated total
- **Platform card**: Which platform is being fetched + platform count
- **Platform bar**: Platforms completed / total platforms

### State 3: Step 3 — Fetching Collections (with collection card)

```
┌─────────────────────────────────────────────┐
│ Fetching collections            Step 3/4    │
│ ██████████████████████████████░░░░░░░░░░░░░ │  ← overall bar (5/8)
│ 5 / 8                                       │
│ ┌─────────────────────────────────────────┐ │
│ │ Best of SNES                    5/8     │ │  ← collection name + counter
│ │ ████████████████████████████░░░░░░░░░░░ │ │
│ └─────────────────────────────────────────┘ │
│                                             │
│ ~8s remaining                 1m04s elapsed │
│ [ Cancel Sync ]                             │
└─────────────────────────────────────────────┘
```

### State 4: Step 4 — Applying Changes (with platform card + game title)

```
┌─────────────────────────────────────────────┐
│ Applying changes                Step 4/4    │
│ ██████████████████████░░░░░░░░░░░░░░░░░░░░ │  ← overall bar (847/1620)
│ 847 / 1,620                                 │
│ ┌─────────────────────────────────────────┐ │
│ │ Nintendo 64                     5/14    │ │  ← which platform, how many done
│ │ ████████████████░░░░░░░░░░░░░░░░░░░░░░ │ │
│ └─────────────────────────────────────────┘ │
│ Super Mario 64 (USA)                        │  ← italic dim, current game title
│ ~3m44s remaining              2m18s elapsed │
│ [ Cancel Sync ]                             │
└─────────────────────────────────────────────┘
```

### State 5: Cleaning Up (no platform card)

```
┌─────────────────────────────────────────────┐
│ Cleaning up                     Step 4/4    │
│ ██████████████████████████████████████████░ │
│ 1,618 / 1,620                               │
│                                             │
│                                             │
│ ~2s remaining                 6m01s elapsed │
│ [ Cancel Sync ]                             │
└─────────────────────────────────────────────┘
```

### State 6: Finalizing (indeterminate, no card)

```
┌─────────────────────────────────────────────┐
│ Finalizing                                  │
│ ░░░░░░░░░░░░░▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░ │
│                                             │
│                                 6m12s elapsed│
│ [ Cancel Sync ]                             │
└─────────────────────────────────────────────┘
```

---

## Data Model

### New `SyncProgress` Fields

| Field | Type | Set By | Purpose |
|---|---|---|---|
| `stepLabel` | `string?` | Backend (steps 1–3), Frontend (step 4) | Human-readable step description |
| `platformCurrent` | `number?` | Backend + Frontend | Current-unit progress numerator |
| `platformTotal` | `number?` | Backend + Frontend | Current-unit progress denominator |
| `platformLabel` | `string?` | Backend + Frontend | Current-unit label (platform or collection name) |

All fields are **optional** — old progress messages still work. Existing fields
(`current`, `total`, `message`, `step`, `totalSteps`, `subMessage`, etc.) are
unchanged and still populated for backward compatibility.

### Step Label Mapping

| Step | Phase | `stepLabel` | Overall Bar | Platform Card |
|---|---|---|---|---|
| 1 | `platforms` | **Connecting** | Indeterminate | Hidden |
| 2 | `roms` | **Fetching ROMs** | ROMs fetched / estimated total | Platform name + platforms done/total |
| 3 | `collections` | **Fetching collections** | Collections done / total | Collection name + count |
| 4 | `applying` | **Applying changes** | Shortcuts across all platforms | Platform name + platform index/total |
| 4 | `applying` (removals) | **Cleaning up** | Removal progress | Hidden |
| — | `done` | **Finalizing** | Indeterminate | Hidden |

### Who Sets Each Field

**Backend (`library.py` → `_emit_progress`)** populates during steps 1–3:

- `step_label` → `"Connecting"` / `"Fetching ROMs"` / `"Fetching collections"`
- `platform_current` → platforms completed (step 2) or collections completed (step 3)
- `platform_total` → total platforms (step 2) or total collections (step 3)
- `platform_label` → current platform name (step 2) or collection name (step 3)

**Frontend (`syncManager.ts` → `updateSyncProgress`)** populates during step 4:

- `stepLabel` → `"Applying changes"` / `"Cleaning up"` / `"Finalizing"`
- `platformCurrent` → `platform_index` from backend event
- `platformTotal` → `total_platforms` from backend event
- `platformLabel` → platform name

---

## Rendering Rules

1. **Step label** — always visible (top-left, bold white 13px). Falls back to `"Syncing…"`.
2. **Step counter** — visible when `step` + `totalSteps` set (top-right, dim 11px uppercase).
3. **Overall bar** — always visible. Indeterminate when `total` is 0 or undefined.
4. **Numeric context** — below overall bar. Shows `current / total` or message text.
5. **Platform card** — appears only when `platformTotal > 0`. Subtle background
   (`rgba(255,255,255,0.04)`), rounded corners. Contains label + counter + its own
   progress bar.
6. **Sub-message** — below the card (or below the context line if no card). Italic,
   dim. Shows the individual game title being processed.
7. **ETA row** — bottom, smallest text (11px, 0.4 opacity). Left: remaining. Right: elapsed.

---

## Files Changed

| File | What Changed |
|---|---|
| `src/types/index.ts` | Added `stepLabel`, `platformCurrent`, `platformTotal`, `platformLabel` to `SyncProgress` interface |
| `py_modules/services/library.py` | Extended `_emit_progress()` with 4 new keyword params; populated in all step 1–3 call sites |
| `src/utils/syncManager.ts` | All `updateSyncProgress()` calls now include `stepLabel` + `platformCurrent` / `platformTotal` / `platformLabel` |
| `src/components/MainPage.tsx` | Complete redesign of syncing progress section — dual bars, step labels, platform card, cleaner layout |

---

## Design Constraints

- **Decky QAM panel width**: ~310px. Two stacked bars + labels fit comfortably.
- **Backward compatible**: All new fields are optional (`?`). Old progress events
  without the new fields render gracefully (step label falls back to "Syncing…",
  platform card hidden when `platformTotal` is 0).
- **No protocol changes**: `sync_apply_platform`, `sync_apply_removals`, and
  `sync_apply_done` events are unchanged. The new fields are added alongside
  existing ones in `sync_progress` events only.
