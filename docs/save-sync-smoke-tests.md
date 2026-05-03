# Save Sync Rewrite — Hardware Smoke Tests

Hardware test plan for the `refactor/save-sync-rewrite` branch. Goal is to exercise every row of the [Decision Matrix](../../decky-romm-sync.wiki/Save-File-Sync-Architecture.md#decision-matrix) plus the rollback flow, conflict modal, per-rom lock, and state migration on a real Steam Deck against a real RomM server.

This file is the source of truth for progress. Tick the status box at the bottom of each test as you go and write notes on anything unexpected. The session can be paused and resumed at any time — the next session reads this file to know exactly where to pick up.

## How we work through this

Each test has three blocks:

- **Setup (Claude)** — the state-prep work I do: curl against RomM, edit `save_sync_state.json`, write/remove fake `.srm` content. I run these without prompting.
- **Action (you)** — what you do on the Deck UI: tap Play, observe the modal, etc. I'll wait for you to report what happened.
- **Verify (Claude)** — what I check after: re-read state, query server, compare hashes. I report back ✓ or ✗.

You don't run any curl. I have your RomM credentials (read from the plugin settings) and the device ids.

When I need a plugin reload I'll ask you to **toggle the plugin off in Decky, wait two seconds, toggle it on**. I'll never tell you to run `mise run dev` for these smoke tests — none of them require a code change unless I push a fix mid-session, in which case I'll explicitly say "run `mise run dev` from the worktree before continuing".

### Trigger sync without launching the game

For most matrix-row tests we want to observe `compute_sync_action`'s decision in isolation. Launching the game brings mGBA into the loop — it loads our mocked `.srm`, decides the bytes aren't valid SRAM, and writes its empty default back on close, masking what we actually planted.

So unless a test specifically exercises post-exit sync, drive the sync by tapping **"Sync All Saves Now"** in the QAM Save Sync settings. That goes through the same `_sync_rom_saves` code path, exercises the same `compute_sync_action` decisions, but never starts the emulator.

The "Action (you)" block in each test below specifies which trigger to use.

## Test ROM and environment

Locked in:

- **ROM name**: Mario Golf - Advance Tour (USA)
- **System**: gba
- **rom_id**: `4409`
- **Save filename**: `Mario Golf - Advance Tour (USA).srm`
- **Save path**: `/run/media/deck/Emulation/retrodeck/saves/gba/Mario Golf - Advance Tour (USA).srm`
- **State path** (RetroArch save state, not relevant to sync — sync only touches `.srm`): `/run/media/deck/Emulation/retrodeck/states/gba/`
- **Active core (per ES-DE)**: mGBA — emulator tag will be `retroarch-mgba` after the first sync that resolves the core
- **RomM server**: `http://192.168.178.83:8085` (v4.8.1)
- **Our device**: `steamdeck` — id `81445610-e5a1-46b5-9389-9d159f99c21c`
- **Simulated "other device"**: `htpc-livingroom` — id `0151fb4c-04ef-42e7-9e22-6e7499e4a94e` (also registered on this RomM, used to simulate B in cross-device flows)

Plugin runtime files I edit on your behalf:

- State: `/home/deck/homebrew/data/decky-romm-sync/save_sync_state.json`
- Settings (read-only for me): `/home/deck/homebrew/settings/decky-romm-sync/settings.json`

Initial baseline (verified just now):

- Server saves for rom 4409: **none** (cleaned up)
- Plugin state for rom 4409: **empty** (`saves["4409"] = {}`)
- Local save: `/run/media/deck/Emulation/retrodeck/saves/gba/Mario Golf - Advance Tour (USA).srm` does **not** exist; only a `.srm.bak` from a previous session is present (will leave it alone — sync ignores `.bak` files)

## Reset between tests

When a test calls for a clean slate (state, local, server), I'll execute:

1. Toggle plugin off (you).
2. `jq 'del(.saves["4409"])' state.json | sponge state.json` (me).
3. `rm -f /run/media/deck/Emulation/retrodeck/saves/gba/Mario\ Golf\ -\ Advance\ Tour\ \(USA\).srm` (me).
4. `curl -X POST .../api/saves/delete -d '{"saves":[<all current ids>]}'` (me).
5. Toggle plugin on (you).

I'll always announce the reset before doing it, so you can confirm.

---

## Tests

> **T1, T2, T3, T4 already passed** (one game session, with mGBA opportunistically writing the empty-default `.srm`). Logs and state confirm matrix rows 1, 2, 7, and 9 each fired correctly. The toast wording bug found during T4 was fixed in d25d790; follow-up issue #250 tracks the per-direction toast breakdown. From T5 onward we drive sync via "Sync All Saves Now" to keep mGBA out of the loop.

### T1 — Matrix row 1 (no local, no server)

Goal: prove `compute_sync_action` returns `Skip(nothing_to_sync)` and the sync flow does no I/O.

**Setup (Claude)**:
- Confirm `saves["4409"]` in state is empty or has no `files`.
- Confirm no `.srm` at the save path.
- Confirm `GET /api/saves?rom_id=4409` returns `[]`.

**Action (you)**:
1. Open Decky → **Save Sync** is enabled (toggle in QAM if not).
2. Open the Mario Golf game-detail page.
3. Tap Play.
4. Tell me what happened (toast text? game launched?).

**Verify (Claude)**:
- `save_sync_state.json` `saves["4409"].last_sync_check_at` advanced; no `files` entries created.
- No new server saves.
- No `.srm` created on disk.

**Status**: [x] Pass — pre-launch sync at 21:26:18 returned `Skip`, synced=0, no I/O.
**Notes**: Ran via Play, fell through to game launch.

---

### T2 — Matrix row 2 (local exists, no server → POST)

Goal: first POST upload from a fresh local file.

**Setup (Claude)**:
- Carry over from T1.
- Write 32 KB random content to the save path. Capture MD5 as `T2_LOCAL_HASH`.

**Action (you)**:
1. Open game-detail page.
2. Tap Play.

**Verify (Claude)**:
- ✓ Server now has one save in slot `default` with our timestamp-tagged filename.
- ✓ State has `saves["4409"].files["Mario Golf - Advance Tour (USA).srm"]` with `tracked_save_id`, `last_sync_hash == T2_LOCAL_HASH`.
- ✓ `device_syncs[me].is_current=true`.
- ✓ `emulator` field on the new save reads `retroarch-mgba` (or whatever ES-DE returns; if it's just `retroarch` we'll note it).

**Status**: [x] Pass — post-exit at 21:26:35 ran `Upload(POST)`, save id 42 created, baseline hash `14338baf...`, emulator tag `retroarch-mgba`, `is_current=true`.
**Notes**: mGBA wrote a 32 KB empty SRAM during the brief play session — that's what was POSTed.

---

### T3 — Matrix row 7 (steady-state Skip)

Goal: a sync after T2 with no changes is a no-op.

**Setup (Claude)**: nothing — carry T2 state.

**Action (you)**:
1. Open game-detail page.
2. Tap Play (or "Sync All Saves Now").

**Verify (Claude)**:
- ✓ No upload, no download.
- ✓ Server save id and `updated_at` unchanged.
- ✓ `last_sync_hash` in state unchanged.
- ✓ `last_sync_check_at` advanced.

**Status**: [x] Pass — both pre-launch and post-exit at 21:38 returned `Skip(synced)`, no I/O. mGBA didn't even touch the `.srm` during the short session.
**Notes**: No toast on Skip is by design.

---

### T4 — Matrix row 9 (offline edit → PUT)

Goal: post-play offline edit propagates as a PUT.

**Setup (Claude)**:
- Overwrite the local file with new 32 KB random content (size unchanged).
- Capture new MD5 as `T4_LOCAL_HASH`.
- Note current server `updated_at` for the tracked save.

**Action (you)**:
1. Open game-detail page.
2. Tap Play.

**Verify (Claude)**:
- ✓ Server save id unchanged (PUT, not POST — same `tracked_save_id`).
- ✓ Server `updated_at` advanced.
- ✓ `last_sync_hash` in state == `T4_LOCAL_HASH`.
- ✓ `device_syncs[me].is_current=true` (re-confirmed by `confirm_download` after PUT).

**Status**: [x] Pass — pre-launch at 21:44:21 ran `Upload(PUT to 42)` with `T4_LOCAL_HASH=3897f6af...`. Post-exit re-uploaded mGBA's empty default (`14338baf...`); both PUTs hit row 9 correctly.
**Notes**: Surfaced a frontend toast bug (pre-launch said "Saves downloaded" instead of "synced") — fixed in d25d790. Filed #250 for the per-direction breakdown.

---

### T5 — Matrix row 10 (server moved, local untouched → silent Download)

Goal: cross-device upload from another client gets pulled silently.

**Setup (Claude)**:
- Generate 32 KB content for a "B" file. Save its MD5 as `T5_FOREIGN_HASH`.
- PUT it onto our `tracked_save_id` using the htpc device_id (`device_id=0151fb4c-...`):
  ```
  curl -u daniel:... -X PUT \
    "$ROMM_URL/api/saves/<tracked_save_id>?device_id=0151fb4c-...&optimistic=true" \
    -F "saveFile=@/tmp/B.srm"
  ```
- That bumps `updated_at` and leaves our (`steamdeck`) `device_syncs` row stale → `is_current=false` for us.
- Confirm via `GET /api/saves?rom_id=4409&device_id=<our>`.

**Action (you)**:
1. Open the QAM Save Sync settings.
2. Tap **Sync All Saves Now**.
3. Tell me when it finishes (toast or button-state change).

**Verify (Claude)**:
- ✓ Toast text was "Saves synced with RomM" (or none, depending on count semantics).
- ✓ `md5sum local` == `T5_FOREIGN_HASH`.
- ✓ `last_sync_hash` in state == `T5_FOREIGN_HASH`.
- ✓ `device_syncs[me].is_current=true` again (auto-upserted by `GET /content?optimistic=true`).

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T6 — Matrix row 12 (true conflict — Keep Local)

Goal: both sides changed → modal appears → Keep Local PUTs and resolves.

**Setup (Claude)**:
- Local: overwrite with new 32 KB random content (`T6_LOCAL_HASH`).
- Server: PUT a different new content via htpc device_id (`T6_SERVER_HASH`). Now both diverge from baseline.
- Note: `last_sync_hash` in state still equals `T5_FOREIGN_HASH` from the previous test.

**Action (you)**:
1. Open the Mario Golf game-detail page.
2. Tap Play. (Conflict tests need the modal flow, which is wired into the Play button.)
3. **Sync conflict modal should appear**. Tell me what the local and server rows show (sizes/timestamps).
4. Tap **Keep Local**.
5. As soon as the modal closes, tell me — I want to capture server state before mGBA can touch the `.srm`. Press the Steam button to back out of the launch as soon as possible.

**Verify (Claude)**:
- ✓ Server save content == `T6_LOCAL_HASH` (captured before mGBA writes anything).
- ✓ `last_sync_hash` in state == `T6_LOCAL_HASH`.
- ✓ `is_current=true` for us.

If mGBA gets a session in before we capture server state, the post-exit upload will overwrite `T6_LOCAL_HASH` with mGBA's default. We'll still see Upload(PUT) in logs and a fresh `last_sync_hash`, just not `T6_LOCAL_HASH` itself. That's still a pass.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T7 — Matrix row 12 (true conflict — Use Server)

Goal: same trigger as T6, opposite resolution.

**Setup (Claude)**: re-create the conflict the same way.
- Local overwrite (`T7_LOCAL_HASH`).
- Server PUT via htpc (`T7_SERVER_HASH`).

**Action (you)**:
1. Open game-detail page.
2. Tap Play.
3. Modal appears. Tap **Use Server**.
4. As soon as the modal closes, back out via the Steam button so mGBA doesn't get a chance to overwrite the freshly-downloaded `.srm`.

**Verify (Claude)**:
- ✓ `md5sum local` == `T7_SERVER_HASH`.
- ✓ `last_sync_hash` in state == `T7_SERVER_HASH`.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T8 — Matrix row 12 (true conflict — Cancel re-fires)

Goal: Cancel does no I/O, no state change; conflict re-fires until user picks a side.

**Setup (Claude)**: re-create the conflict.
- Local overwrite (`T8_LOCAL_HASH`).
- Server PUT via htpc (`T8_SERVER_HASH`).
- Capture pre-test `last_sync_hash` for comparison.

**Action (you)**:
1. Open game-detail page.
2. Tap Play.
3. Modal appears. Tap **Cancel**.
4. Tell me whether the game launched anyway.
5. Exit the game without saving (or just press the Steam button to back out).
6. Open game-detail page again.
7. Tap Play.
8. Tell me whether the modal appeared again.

**Verify (Claude)** after step 3:
- ✓ Local file unchanged (`md5sum` == `T8_LOCAL_HASH`).
- ✓ Server file unchanged (`T8_SERVER_HASH`).
- ✓ `last_sync_hash` in state == pre-test value (NOT T8_LOCAL_HASH, NOT T8_SERVER_HASH).

**Verify (Claude)** after step 7: modal expected to re-fire (matrix still produces row 12).

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T9 — Matrix row 4 (recovery: tracked exists, local missing)

Goal: deleted local file is restored from the tracked server save.

**Setup (Claude)**:
- Resolve the lingering conflict from T8 first by either re-running T6 (Keep Local) setup or running T7 setup (Use Server). Let's go with Keep Local for predictability — that gets us back into matrix row 7 (synced) territory after the resolve.
- Then: delete the local file. Note the server save and `last_sync_hash` from state.

**Action (you)**:
1. (After T8 was a Cancel, run a quick "Keep Local" first to clean state — I'll tell you when.)
2. Open the QAM Save Sync settings.
3. Tap **Sync All Saves Now**.

**Verify (Claude)**:
- ✓ Local file recreated.
- ✓ MD5 of recreated local == server content MD5 == `last_sync_hash` in state.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T10 — Matrix row 6a (no entry + local newer → POST as new save)

Goal: a server slot we never touched + our local file ahead by mtime → POST a new save side-by-side.

**Setup (Claude)**:
- Reset rom state (delete `saves["4409"]` from state file). Plugin off, edit, plugin on.
- Local: write new 32 KB random content. `touch -d "now"` it for fresh mtime.
- Server: keep the existing save from T9 — but we need our `device_syncs` row gone for "no entry". Easiest path: **delete and re-create the server save as device htpc only**, with no steamdeck `device_syncs` row. I'll do that via:
  1. Capture content via download.
  2. Delete existing save.
  3. POST new save with htpc device_id only.
- After this the slot has 1 save with no entry for us.

**Action (you)**:
1. Toggle plugin on.
2. Open the QAM Save Sync settings.
3. Tap **Sync All Saves Now**.

**Verify (Claude)**:
- ✓ Server slot now has **2** saves (the htpc save untouched + our newly POSTed save).
- ✓ Our new save id stored as `tracked_save_id`. New save is newest (`updated_at`).
- ✓ Original htpc save unchanged.

**Status**: [N/A] — see Findings & Follow-ups below. Branch 6 of `compute_sync_action` (no `device_syncs` entry for our device) is unreachable in real plugin operation because RomM's `GET /api/saves?rom_id=X&device_id=Y` always upserts a row for the queried device. Logic is verified by unit tests in `tests/domain/test_sync_action.py`.
**Notes**: Worth re-evaluating once we've decided whether to adjust the adapter's `optimistic` parameter usage (see #1 in Findings).

---

### T11 — Matrix row 6b (no entry + server newer → Download)

Goal: same as T10 but with local mtime older than server.

**Setup (Claude)**:
- Reset rom state.
- Local: write 32 KB random content. `touch -d "1 hour ago"` so mtime is in the past.
- Server: 1 save authored by htpc only (no `device_syncs[me]`), `updated_at` recent.

**Action (you)**:
1. Open the QAM Save Sync settings.
2. Tap **Sync All Saves Now**.

**Verify (Claude)**:
- ✓ Local file content == server content.
- ✓ State updated with `last_sync_hash` == server-content MD5.

**Status**: [N/A] — same reason as T10. The "no entry for our device" precondition is unreachable from the UI side because every plugin-side `list_saves` GET upserts a row for our device.
**Notes**: Logic verified by unit tests.

---

### T12 — Matrix row 8 (adopt baseline)

Goal: server reports `is_current=true` for our device but we have no `last_sync_hash` yet → state silently records the baseline.

**Setup (Claude)**:
- Bring rom 4409 to a synced state (run T2 if needed).
- Plugin off. Strip `last_sync_hash` from `state.saves["4409"].files["Mario Golf - Advance Tour (USA).srm"]`.
- Capture local MD5 as `T12_LOCAL_HASH`.
- Confirm via curl that `device_syncs[me].is_current=true` for the tracked save.
- Plugin on.

**Action (you)**:
1. Open game-detail page (`getSaveStatus` triggers; no Play tap needed).
2. Tell me when the page is open and you see the SAVES tab.

**Verify (Claude)**:
- ✓ State now has `last_sync_hash == T12_LOCAL_HASH`.
- ✓ No upload, no download (tracked save id and `updated_at` unchanged).

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T13 — Cross-device round trip

Goal: classic A → B → A scenario silently propagates.

**Setup (Claude)**:
- Reset rom state, local, server.
- Local: write 32 KB random.
- (Phase 1 of test: us POSTing.)

**Action (you) — phase 1**:
1. Tap **Sync All Saves Now** in QAM. Tell me when done.

**Setup (Claude) — phase 2**: simulate device B (htpc) updating the save.
- Generate `/tmp/B.srm` (`T13_B_HASH`).
- PUT to our `tracked_save_id` with `device_id=<htpc>` and `optimistic=true`.
- Optionally call `confirm_download` as htpc to mark htpc current. (Not strictly required for this test.)

**Action (you) — phase 2**:
2. Tap **Sync All Saves Now** again. Tell me whether a modal appeared.

**Verify (Claude)**:
- ✓ No modal in phase 2.
- ✓ Local content == `T13_B_HASH`.
- ✓ State `last_sync_hash` == `T13_B_HASH`.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T14 — Rollback flow

Goal: roll the local + server back to an older save and confirm cross-device propagation works.

**Setup (Claude)**:
- Reset rom state, local, server.
- Create three POSTed saves in slot `default` with increasing timestamps:
  - v1 (`T14_V1_HASH`) — POSTed as our device, then immediately `confirm_download`.
  - v2 (`T14_V2_HASH`) — POSTed as htpc.
  - v3 (`T14_V3_HASH`) — POSTed as htpc, newest.
- Plugin should pick v3 on next sync. Sync the plugin once so our local matches v3 (Action: tap Play). After that, our state has v3 as tracked.

**Action (you) — phase 1**: tap **Sync All Saves Now** once so our local catches up to v3. Confirm via me.

**Setup (Claude) — phase 2 marker**: capture all three save ids and their `updated_at` values for verification.

**Action (you) — phase 2**:
1. Open game-detail → SAVES tab.
2. Find the version-history list (or version picker — wherever rollback lives in the current UI).
3. Pick **v1** (the oldest).
4. Tap **Rollback to this version** (or whatever the button is named — tell me what you see).
5. Confirm any "this will discard local changes" warning.

**Verify (Claude)**:
- ✓ Server: v1's `updated_at` is now NEWEST in the slot (post-PUT bump).
- ✓ Local: `md5sum` == `T14_V1_HASH`.
- ✓ State: `last_sync_hash == T14_V1_HASH`, `tracked_save_id == v1.id`.
- ✓ `device_syncs[me].is_current=true` on v1.

**Cross-device verification (Claude)**: simulate htpc opening the game (via `GET /api/saves?rom_id=4409&device_id=<htpc>`). htpc's `is_current` for v1 should be `false` → if we ran `compute_sync_action` for htpc, it'd return `Download(v1)`. (We don't actually run htpc — just confirm the server state is consistent.)

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T15 — Per-rom asyncio.Lock

Goal: simultaneous pre_launch_sync + sync_all serialise without races.

This is best-effort given manual timing.

**Setup (Claude)**: any rom in a steady state (carry over T14).

**Action (you)**:
1. Open the QAM panel and the Mario Golf game-detail page side by side (QAM stays open while game-detail is foregrounded).
2. From game-detail, tap Play.
3. Within ~200ms (best effort), tap **Sync All Saves Now** in the QAM.
4. Tell me when both finish.

**Verify (Claude)**:
- Plugin log under `~/homebrew/data/decky-romm-sync/logs/` (or `/tmp/plugin_loader_stdout.log`) shows two `_sync_rom_saves(4409)` log blocks for the same rom — but they should NOT interleave (one finishes before the other starts).
- State file consistent (no half-written fields).

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T16 — State migration (legacy `dismissed_newer_save_id`)

Goal: the plugin loads state from before the rewrite without errors and silently drops the legacy field.

**Setup (Claude)**:
- Plugin off.
- Inject `dismissed_newer_save_id: 999` into one file entry under `saves["4409"].files["Mario Golf - Advance Tour (USA).srm"]`.
- Verify field is present in the JSON file.
- Plugin on.

**Action (you)**:
1. Open game-detail page (triggers a state read + write).
2. Tell me when the page is loaded.

**Verify (Claude)**:
- ✓ Field `dismissed_newer_save_id` is gone from the state file after the next state write.
- ✓ Plugin logs show no errors related to state load.
- ✓ All other fields preserved.

**Status**: [x] Pass — first run surfaced a real migration-shim bug; fix shipped in `cc1cff2` and re-verified on hardware.
**Notes**: Initial T16 attempt revealed the `dismissed_newer_save_id` field stayed on disk after page-open. Root cause: `init_state()` runs *before* `load_state()` in main.py, so the migration loops in init_state iterated over an empty `saves` dict. `load_state` then overwrote in-memory state with the disk content (legacy field included), and nothing migrated it after. Fixed by moving both migrations (`active_core` → `last_synced_core` and the `dismissed_newer_save_id` strip) into a new `_migrate_loaded_state()` called from `load_state` after the disk read. Added end-to-end test `test_load_state_drops_legacy_dismissed_newer_save_id_persists_to_disk` that asserts the field is gone from the file on disk after init→load→save. Re-tested on hardware: state file at 07:31:28 had `dismissed_newer_save_id` gone, all other fields preserved.

---

## Session Resume Notes (2026-05-01)

The smoke run got as far as T14 (rollback) passing cleanly. T15 (per-rom lock) ran best-effort and is marked N/A — manual timing on hardware couldn't trigger overlapping calls; the lock's correctness is covered by unit tests. T16 (state migration) is paused mid-test because two unrelated issues surfaced during setup that need to be addressed before continuing.

### What's confirmed working through T14
- All decision-matrix paths reachable from the UI (rows 1, 2, 4, 5, 7, 8, 9, 10, 12 in three modes).
- Conflict modal: Keep Local, Use Server, Cancel — all behave correctly, modal is gated behind the Play button (Sync button + page open never surface the modal).
- Rollback flow: download → PUT bumps server `updated_at` → `confirm_download` registers our device current → other devices pick it up on next sync.
- State migration shim runs at plugin load (legacy `dismissed_newer_save_id` is stripped in memory; persistence happens on the next state-write).

### Two issues blocking T16 completion

#### (1) `installed_roms` registry got wiped
At some point before T16 setup, `state.json.installed_roms` ended up empty (`{}`), even though the ROM file is still on disk at `/run/media/deck/Emulation/retrodeck/roms/gba/Mario Golf - Advance Tour (USA).zip` and `state.json.shortcut_registry["4409"]` still has the metadata. Likely cause: `_prune_stale_installed_roms` removed an entry whose `file_path` pointed at an old internal-SSD location after a RetroDECK SSD→SD-card migration; nothing in the codebase repopulates the entry from disk afterward.

**There is no self-heal path in the codebase.** The only writes to `installed_roms` are `downloads.py:307,324` (when a ROM is downloaded). The only deletes are explicit (`rom_removal.py`) or the prune at startup (`main.py:65 _prune_stale_installed_roms`). On disk-state divergence, the only way to repopulate today is to re-download via the plugin's Download button.

User's expectation was that the registry self-heals. It doesn't. Adding self-heal is a small additive feature (~30 LOC): on plugin load, for each `shortcut_registry[id]` whose canonical ROM path (`<roms_path>/<platform_slug>/<fs_name>` or the extracted directory) exists on disk, populate `installed_roms[id]` with `file_path`, `file_name`, `system`, `platform_slug`, `installed_at`, and (for zip ROMs) `rom_dir`. Done at startup before `_prune_stale_installed_roms` runs.

#### (2) Saves tab UI architecture is wrong for the slot model
`_get_save_status_io` produces an array `SaveStatus.files[]` rendered by `SavesTab.tsx` as one "card" per entry (header row + Previous Versions dropdown). The function does two passes: per-local-file (Pass 1) and per-server-only-target-filename (Pass 2). Pass 2 was originally for legacy multi-file-per-slot scenarios — it should not exist in the current model. With `installed_roms` empty, `_get_rom_save_info` returns None, `rom_name` is None, `_local_save_target` falls back to per-server-`file_name_no_tags` targets, and Pass 2 emits a separate top-level entry per server save (the symptom user saw: three top-level entries with cross-pollinated dropdowns).

### Agreed model for the rewrite

**Per slot**:
- Exactly **one** active tracked save → one top-level entry in the saves tab.
- Header row shows: local filename, last synced (with attribution: "this device" / device name), last updated, server save id + emulator + server filename, local path.
- Below that: a `Previous Versions` dropdown.

**Previous Versions dropdown**:
- Lazy-fetched on first expand (already implemented).
- Lists **every** server save in the active slot **except** the currently-tracked one. **No filter on `file_name_no_tags`.** Saves uploaded by other clients (Argosy, RomM web UI) with different naming schemes are first-class entries, not hidden.
- Ordered by `updated_at` descending (newest first).
- Each row shows: `#id`, server filename (with timestamp tag), file size, last-updated timestamp, attribution device.

**Switching to a previous version (the "rollback" / "restore" flow)**:
1. Verify the currently-tracked save is on the server (Gates D + F: refuse switch if local has unsynced changes or tracked save is missing server-side, unless user confirms).
2. Delete the local file.
3. Download the chosen older version → write to the canonical local filename `<rom_basename>.<server.file_extension>`. (Server's stored filename is irrelevant — RetroArch identifies SRAM by ROM-basename, not server-stored name.)
4. Update state: `tracked_save_id = chosen.id`, `last_sync_hash` = chosen content's MD5, `last_sync_server_*` fields from the server response.
5. Call `confirm_download(chosen.id, our_device_id)` → marks our device as current on that save server-side. The PUT step in `_rollback_to_version_io` additionally bumps `updated_at` so other devices on their next sync see the chosen save as newest and adopt it. This is how cross-device propagation works.

After the switch, the previously-tracked save remains on the server (still in the slot, accessible via the dropdown). It just isn't the active one anymore.

### Refactor plan (post-compact)

1. **Add `installed_roms` self-heal at plugin startup** (~30 LOC in `main.py`, runs before `_prune_stale_installed_roms`). For each `shortcut_registry` entry, derive the expected ROM path from `retrodeck_home_path + roms/<platform_slug>/<fs_name>` (and the extracted-dir variant for zips), check existence, populate `installed_roms[id]` if missing. Idempotent.

2. **Collapse `_get_save_status_io` to one entry per slot** in `py_modules/services/saves.py`:
   - Drop the "Pass 1 / Pass 2" structure entirely.
   - One function: if local file exists in the saves dir, build an entry from it; else if any server save is in the slot, build an entry from the newest server save (status="download"); else no entry.
   - The result list has 0 or 1 entry per slot (multi-file-per-slot scenarios deferred to Phase 7 if/when they arrive).

3. **Drop the `file_name_no_tags` filter from `list_file_versions`** in `py_modules/services/saves.py`. Show every save in the slot except `tracked_save_id`. Ordering: `updated_at` descending. Update the existing `TestListFileVersions` cases — saves with mismatched basenames now appear in the result (they're first-class versions).

4. **Wiki update** (`Save-File-Sync-Architecture.md`): rewrite the "Local Save File Naming", "Decision Matrix" + Pass 2 references, and Rollback Flow sections to match the slot model. Drop Pass 1/Pass 2 terminology — the function is just "build the active save status".

5. **Smoke test resume**: with the new code in place, T16 (state migration) becomes trivial — open the page, verify `dismissed_newer_save_id` is gone after the next state write. Then we're done.

### Outstanding follow-up issues (file when smoke run completes)

- **#250** (already filed): per-direction toast counts (uploaded vs. downloaded vs. mixed).
- **NEW**: `installed_roms` self-heal at startup. Even after we add it as part of this rewrite, there's a deeper question: should the prune step also be more conservative (e.g. skip pruning when the canonical ROM path exists on disk even if the recorded `file_path` doesn't)? Out of scope here.
- **NEW**: Stale Play-button state in Game Mode after Desktop→Game switch (seen at T7). Likely a frontend cache that doesn't refresh on plugin loader restart. Independent of this rewrite.
- **NEW**: Investigate the `optimistic` query parameter on `GET /api/saves` (recorded in Findings section below). Open question whether the flag changes upsert semantics on the LIST endpoint or is ignored.

### How to resume

After compact, the next session should:
1. Read this doc top-to-bottom.
2. Read `py_modules/services/saves.py` `_get_save_status_io` and `list_file_versions`, plus `main.py` `_prune_stale_installed_roms`, to refresh on the code shape.
3. Implement the four refactor items above as separate commits.
4. Run `mise run test`, lint, push.
5. Ask the user to `mise run dev` on the Deck.
6. Resume T16 (state migration) — should pass cleanly with the new code.
7. Mark the smoke run complete, file the new follow-up issues, open the PR.

---

## Quick reference — matrix coverage

| Matrix row | Test |
|---|---|
| 1 | T1 |
| 2 | T2 |
| 3 | covered implicitly during T11 reset path |
| 4 | T9 |
| 5 | covered implicitly during T13 reset paths |
| 6a | T10 |
| 6b | T11 |
| 7 | T3 |
| 8 | T12 |
| 9 | T4 |
| 10 | T5, T13 |
| 11 | deferred (rare; needs no-baseline + is_current=false setup, hard to reach naturally) |
| 12 (Conflict — Keep Local) | T6 |
| 12 (Conflict — Use Server) | T7 |
| 12 (Conflict — Cancel) | T8 |

Plus T14 (rollback), T15 (lock), T16 (migration).

## When all tests pass

- Note any cosmetic issues (toast wording, modal layout) for follow-ups.
- Open a PR from `refactor/save-sync-rewrite` → `main`.
- Mark project-tracker tasks #10 and #11 done.

## Findings & Follow-ups (discovered during the smoke run)

### 1. RomM `GET /api/saves` upserts `device_syncs` regardless of `optimistic` flag

Empirically verified during T10 setup on RomM 4.8.1:

- `GET /api/saves?rom_id=X&device_id=Y` (with or without `&optimistic=true`/`false`) creates a `device_save_sync` row for device Y on every save returned that did not already have one.
- The `optimistic` flag does NOT prevent the upsert.
- Effect on `compute_sync_action`: branch 6 ("no entry for our device") is dead code in real plugin operation — `our_entry` is always set by the time the algorithm sees the data, because `services/saves.py:_sync_rom_saves` calls `list_saves` first, which fires the GET, which upserts.
- T10 and T11 are therefore not hardware-reachable. Logic is covered by unit tests.

**Open question — can we use this to our advantage?**

Two ideas worth investigating:

- **(a) Use `optimistic=false` in `list_saves`** to keep the upserted row's `last_synced_at` not equal to `save.updated_at` — possibly making the "first observation" semantically distinguishable from "synced". *Needs experimentation: does `optimistic=false` change the upsert payload, or is the flag ignored on the list endpoint?* My T10 setup couldn't tell because both flags resulted in `last_synced_at = save.updated_at` and `is_current=False`.
- **(b) Skip the upsert entirely** by querying without `device_id` and matching device_syncs client-side. We'd lose RomM's per-device filtering of `device_syncs` array (currently the response only contains the queried device's row). Bigger refactor.

Recommended next step: experiment with `optimistic=false` against a fresh save (after this smoke run completes) and document what semantic difference, if any, the flag has on the LIST endpoint. If there is no difference, file a RomM upstream issue clarifying the spec.

### 2. `is_current` formula is strict `>`, not `>=`

The wiki currently says `is_current = sync.last_synced_at >= save.updated_at`. Empirical observation during T10: when `last_synced_at == save.updated_at` exactly (same ISO string), `is_current=False`. So the formula is strict greater-than: `is_current = sync.last_synced_at > save.updated_at`.

Already fixed in the wiki commit that follows this doc update.

### 3. Pre-launch toast text was hard-coded for download direction

Filed as #250. Interim fix in d25d790 changed both pre-launch and post-exit toasts to direction-neutral "Saves synced with RomM". Per-direction breakdown is the proper fix.

### 4. Stale Play button state in Game Mode after Desktop→Game switch (T7)

When the user resolved a conflict in Desktop Mode and then switched to Game Mode, the play button initially showed standard "Spielen" instead of the conflict-aware version. Restarting the Deck fixed it. Probably stale `CustomPlayButton` state cache; first page render didn't re-poll `getSaveStatus`. Worth a follow-up issue if reproducible.
