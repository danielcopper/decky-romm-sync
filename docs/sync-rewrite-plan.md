# Save Sync Rewrite — Argosy Model

**Branch**: `refactor/save-sync-argosy-model`
**Base**: `main` @ `2428f66`
**Estimated effort**: 4-5 days focused

## Goal

Replace the current "no assumptions, user decides on every ambiguity" sync model with the pragmatic Argosy/Grout model used by the official RomM clients. Reduce code surface by ~1500 LOC, simplify maintenance, and align with the wider RomM ecosystem.

**What we adopt from Argosy:**
- Newest-server-save-in-slot wins by default
- `device_syncs[me].is_current` is the primary discriminator
- `last_sync_hash` (= Argosy's `lastUploadedHash`) is the only safety net against silent overwrite of locally-edited content
- No foreign-save surfacing, no newer-in-slot modal, no per-save dismiss state

**What we add over Argosy:**
- A **Cancel/Defer** button on the conflict modal — user can defer the decision; conflict re-fires on next sync unless server state changes. Argosy forces an immediate decision; we don't.

**What stays untouched (orthogonal):**
- Save-restore-from-history feature (version timeline UI)
- Slot management UI in game-detail panel
- Device management
- BIOS sync, settings, etc.

## Algorithm

```python
# domain/save_sync.py — pure function, no I/O

def compute_sync_action(
    local_file: LocalFile | None,
    server_saves_in_slot: list[ServerSave],   # already slot-filtered by caller
    files_state: dict,                         # files_state[filename]
    device_id: str,
    local_hash: str | None,                    # pre-computed by service
) -> SyncAction:

    # 1. No server saves in slot
    if not server_saves_in_slot:
        if local_file:
            return Upload(target=None)         # POST as new
        return Skip(reason="nothing_to_sync")

    # 2. Pick newest server save (max updated_at)
    server = max(server_saves_in_slot, key=lambda s: s.updated_at)

    # 3. Defer-state check
    deferred = files_state.get("deferred")
    if deferred and deferred["server_save_id"] == server.id and deferred["server_updated_at"] == server.updated_at:
        return Skip(reason="deferred_unchanged")

    # 4. Find our device's entry on this save
    our_entry = next((d for d in server.device_syncs if d.device_id == device_id), None)

    # 5a. is_current=true: skip unless local diverged
    if our_entry and our_entry.is_current:
        if local_file and local_hash != files_state.get("last_sync_hash"):
            return Conflict(server)            # local edited offline, surfaced post-launch in Argosy
        return Skip(reason="synced")

    # 5b. is_current=false: server timeline moved
    if our_entry and not our_entry.is_current:
        if local_file and local_hash != files_state.get("last_sync_hash"):
            return Conflict(server)            # both changed
        return Download(server)                # silent server-newer adoption

    # 5c. No entry for our device: fall back to timestamps
    if not local_file:
        return Download(server)
    if local_file.mtime >= server.updated_at:
        return Skip(reason="local_newer_no_entry")
    return Download(server)
```

## SyncAction variants (reduced from previous 5)

| Variant | Side effects | Meaning |
|---|---|---|
| `Skip(reason)` | none | Nothing to do (synced, deferred, local-newer) |
| `Upload(target=None or save_id)` | POST or PUT + state update | Push local to server |
| `Download(server_save)` | download + state update | Adopt server save |
| `Conflict(server_save)` | emit conflict | User must decide |

`Unrecoverable` dropped from previous design — Argosy doesn't model it; defensive programming via input validation in service layer is enough. Edge cases (duplicate local files, etc.) are pre-conditions enforced by `_find_save_files`, not first-class decision outputs.

## Resolve callable

```python
@callable
async def resolve_sync_conflict(
    rom_id: int,
    filename: str,
    action: Literal["keep_local", "use_server", "defer"],
) -> dict:
    ...
```

Three actions:
- `keep_local` → upload local to server (PUT to current server save), update state
- `use_server` → download server save, overwrite local, update state
- `defer` → persist `deferred: {server_save_id, server_updated_at, deferred_at}` in state; no I/O. Modal closes; game launches without sync.

Defer state auto-clears on:
- Server state change (different `server_save_id` or different `updated_at`) → conflict re-fires fresh
- User explicitly resolves (keep_local/use_server) → defer state replaced

## State migration (lazy, no big-bang)

Existing v0.15.0 state on disk:
```json
{
  "tracked_save_id": 123,
  "last_sync_hash": "abc",
  "last_sync_server_updated_at": "...",
  "last_sync_local_mtime": 1234.5,
  "last_sync_local_size": 8192,
  "last_sync_at": "...",
  "dismissed_saves_state": {"99": "..."}     // OBSOLETE
}
```

New schema:
```json
{
  "tracked_save_id": 123,                    // KEPT (semantic identical)
  "last_sync_hash": "abc",                   // KEPT
  "last_sync_server_updated_at": "...",      // KEPT
  "last_sync_local_mtime": 1234.5,           // KEPT
  "last_sync_local_size": 8192,              // KEPT
  "last_sync_at": "...",                     // KEPT
  "deferred": null                            // NEW (optional)
}
```

**Migration approach**: read tolerantly, write strictly. On state load, ignore `dismissed_saves_state` field. On any state mutation, write without it. No version bump needed; eventually all entries get re-written naturally.

**Risk for existing users**: minimal. A user who had foreign saves dismissed will see the modal one more time on the first sync after upgrade (because dismiss state is dropped). They resolve once via keep_local/use_server/defer and it sticks per the new mechanic.

## Per-rom asyncio.Lock

The 197-branch had a useful commit (`5e926f6 feat(saves): per-rom asyncio.Lock serializes concurrent sync operations`) that prevents race conditions when `pre_launch_sync`, `post_exit_sync`, and manual sync run concurrently on the same rom_id. **Re-implement fresh in Phase 1** rather than cherry-picking (the cherry-pick would conflict with code we're about to delete anyway). Pattern is ~20 LOC: `dict[int, asyncio.Lock]` with acquire-around-sync-operations.

## Phase plan

### Phase 1 — Domain rewrite (1 day)

**Output:**
- `py_modules/domain/save_sync.py` rewritten with `compute_sync_action(...)` pure function + new typed dataclasses (`LocalFile`, `ServerSave`, `DeviceSync`, `SyncAction` union variants)
- Per-rom asyncio.Lock infrastructure in `SaveService`
- Layer-1 pure-function tests covering ~6-8 matrix rows (see Smoke matrix below)

**Old code untouched.** Service still calls the old detection paths. Plugin functional throughout.

### Phase 2 — Service rewrite + Frontend modal + E2E (2 days)

**Output:**
- `_sync_rom_saves` rewritten to use `compute_sync_action`
- `_get_save_status_io` rewritten similarly
- New `resolve_sync_conflict` callable in `main.py`
- New `SyncConflictModal.tsx` (Argosy-style: Keep Local | Use Server | Defer/Cancel)
- TypeScript types simplified (one conflict type, three actions)
- Layer-2 dispatch tests + Layer-3 E2E tests
- **Hardware smoke test** before merge

**Plugin transitions to new model.** Old callables deleted. Old detection helpers still in tree but unused (they get deleted in Phase 3).

### Phase 3 — Cleanup + docs (1-2 days)

**Output:**
- Dead code purge (~1500 LOC, see checklist below)
- Wiki rewrite of `Save-File-Sync-Architecture.md`
- Release notes draft

## Cleanup checklist (Phase 3)

### Backend (Python)

Delete:
- `_check_newer_in_slot`, `_build_newer_in_slot_conflict` (saves.py)
- `_find_newer_in_slot`, `foreign_saves_in_slot` (save_sync.py)
- `_collect_server_only_saves`, `_find_slot_fallback`, `_mark_older_versions_in_slot` (save_sync.py)
- `_match_single_local_file` (replaced by inline simple lookup)
- `MatchedSave`, `MatchResult` dataclasses (replaced by direct iteration)
- `_find_local_hash_match` (no hash-match-adopt under Argosy)
- `_migrate_dismiss_state` (legacy migration)
- `resolve_newer_in_slot`, `_resolve_newer_in_slot_locked` (replaced)
- All Strategy-A demotion code in `_sync_rom_saves`
- `dismissed_saves_state` reads/writes everywhere

### Frontend (TS/React)

Delete:
- `src/components/NewerInSlotModal.tsx`
- `src/components/ConflictModal.tsx` (old)
- `NewerInSlotConflict`, `NewerSaveEntry`, `DeviceSyncInfo` types in `src/types/index.ts`
- `resolveNewerInSlot`, `resolveConflict` callable wrappers in `src/api/backend.ts`

### Tests

Delete:
- `TestNewerInSlotConflict`, `TestDetectNewerInSlotDismiss`, `TestResolveNewerInSlotV2`
- `TestS4bKeepCurrentPostsWhenNoOwnOnServer`, `TestS4bKeepCurrentPostsWhenForeignShareOurFilename`
- `TestSyncReuploadsAndFlagsForeignWhenTrackedDeletedWithForeign`
- `TestFirstSetupHashMatch`, `TestFindLocalHashMatchUnit`
- `TestFreshSlotAutoSelect`, `TestFirstSyncMetadataPersistence`
- `TestDismissStateMigration`
- `TestPerRomLock` (re-implemented for new code)

### Docs

Delete or archive (these live on the abandoned `feature/197-version-picker` branch):
- `docs/block3-handoff.md`
- `docs/sync-behavior-matrix.md`
- `docs/sync-decision-matrix.md`
- `docs/manual-test-plan.md` (S1-S9b states obsolete; replaced by smoke list below)

## Smoke test checklist (run on hardware, Phase 2 + Phase 3)

| # | Scenario | Expected | Verified |
|---|----------|----------|----------|
| 1 | First sync, no local, server has 1 own | Silent download | [ ] |
| 2 | First sync, local exists, no server | POST upload | [ ] |
| 3 | Steady-state, nothing changed | Skip | [ ] |
| 4 | Local edited offline, server unchanged | Silent PUT (post-launch) or Conflict (Argosy treats this as `is_current=true + hash divergence` → it's a Conflict) | [ ] |
| 5 | Server changed (other device synced), local unchanged | Silent download | [ ] |
| 6 | Conflict (both changed), pick Keep Local | PUT local, modal closes, state updated | [ ] |
| 7 | Conflict (both changed), pick Use Server | Download server, overwrite local, state updated | [ ] |
| 8 | Conflict (both changed), pick Defer | Modal closes, game launches, no I/O. Re-launch without server change → Skip (deferred). | [ ] |
| 9 | Defer + server-side change → re-launch | Conflict re-fires fresh | [ ] |
| 10 | Recovery: tracked dead, local exists | POST as new save | [ ] |
| 11 | Cross-device: A syncs → B downloads + uploads → A re-launches | Silent download of B's save (no prompt) | [ ] |
| 12 | Per-rom lock: trigger pre_launch + manual sync simultaneously | Operations serialize, no race | [ ] |
| 13 | State migration: load v0.15.0 state file with `dismissed_saves_state` | No error, field ignored, next state-write drops it | [ ] |

## Wiki update items (Phase 3 — separate from code work)

- **`Save-File-Sync-Architecture.md`** — full rewrite with new algorithm, updated diagrams
- **README** — update sync features section to mention "follows official RomM client model (Argosy/Grout)"
- **CHANGELOG / release notes** — draft entry: "Sync model simplified to match Argosy/Grout. Newer-in-slot prompts removed. Cross-device uploads are silently adopted unless local edits diverge. New Cancel/Defer button on conflict modal."

## Open items / risks

1. **UX regression for existing users**: anyone who relied on the newer-in-slot modal to be aware of cross-device activity loses that signal. Mitigation: improved Save-Status indicator in game-detail panel showing last-synced device + timestamp. Out of scope for this rewrite, but worth a follow-up issue.
2. **Argosy's hash-divergence-on-current-device model**: row 4 in smoke list. Argosy's pre-launch resolver returns `LocalIsNewer` (skip) when `is_current=true`, then post-session detects hash divergence and surfaces a conflict. Our plugin runs sync at pre-launch AND post-exit; we may want to treat row 4 as a Conflict at either point, not just post. **Decision needed in Phase 1**: where do we detect this divergence?
3. **No hash-match-auto-adopt**: existing first-sync code adopts via hash match silently (e.g. `B4.2`/`F12` in old matrix). Argosy doesn't do this. We lose a minor UX optimization (silent state-write when local accidentally matches server). Acceptable per design discussion.
4. **Multi-slot first-setup**: Argosy doesn't prompt at launch; user picks slot in setup-wizard. We already have a setup wizard — confirm it covers slot selection adequately. If not, follow-up issue.

## Status

- [ ] Phase 1 — Domain rewrite + tests + per-rom lock
- [ ] Phase 2 — Service + frontend + E2E + hardware smoke
- [ ] Phase 3 — Cleanup + wiki + release notes
- [ ] Final smoke matrix (13 items)
- [ ] PR + merge to main
- [ ] Old `feature/197-version-picker` branch closed/deleted on origin
