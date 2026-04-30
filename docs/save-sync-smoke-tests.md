# Save Sync Rewrite — Hardware Smoke Tests

Hardware test plan for the `refactor/save-sync-rewrite` branch. Goal is to exercise every row of the [Decision Matrix](../../decky-romm-sync.wiki/Save-File-Sync-Architecture.md#decision-matrix) plus the rollback flow, conflict modal, per-rom lock, and state migration on a real Steam Deck against a real RomM server.

This file is the source of truth for progress. Tick the status box at the bottom of each test as you go and write notes on anything unexpected — we will iterate on bugs from this list.

## How to use this document

1. Pick the next un-ticked test in order. Order matters because some tests build on the state left by the previous one.
2. Read the **Setup** block carefully and put the system in the exact state described.
3. Run **Steps** and observe **Expected**.
4. Write **Pass / Fail / Skip** at the bottom of the test. Add notes on anything off.
5. If you fail a test or get blocked, stop and let me know — we'll triage and decide whether to keep going against the broken state or reset.

You can stop and resume any time. The next session reads this file to know exactly where you left off.

## Conventions

### Plugin reload / rebuild

- **Reload plugin only**: Decky → decky-romm-sync → toggle off, wait 2 seconds, toggle on. Use this when you change `save_sync_state.json` on disk and want the plugin to re-read it. **Important: only modify `save_sync_state.json` while the plugin is unloaded.** The plugin writes the file from memory on shutdown and on every state mutation; concurrent edits will be lost.
- **Rebuild + reload (`mise run dev`)**: run from `/home/deck/Repos/decky-romm-sync/.worktrees/refactor/save-sync-rewrite/` when you have changed source code (Python or TypeScript). I will flag with **REBUILD** any test that requires a code change. Tests below mostly do not — they only need a plugin reload at most.

### Paths

- **Plugin runtime**: `/home/deck/homebrew/plugins/decky-romm-sync/`
- **State file**: `/home/deck/homebrew/data/decky-romm-sync/save_sync_state.json`
- **Plugin logs**: `/home/deck/homebrew/data/decky-romm-sync/logs/` plus Decky's Loader log at `/tmp/plugin_loader_stdout.log`
- **RetroDECK saves (internal SSD)**: `/home/deck/retrodeck/saves/<system>/<rom_name>.srm`
- **RetroDECK saves (SD card)**: `/run/media/deck/Emulation/retrodeck/saves/<system>/<rom_name>.srm`
- **RetroDECK ROMs**: same root as saves but `roms/<system>/<rom_name>.<ext>`

### Test ROM

Pick **one** ROM you can play through quickly and stick with it for all tests. Suggested: a GBA game, because save files are tiny and `mGBA` boots fast. Record below:

- ROM name: `___________________________________`
- System: `___________________________________`
- rom_id (RomM database id; visible in the RomM web UI URL or the plugin debug log): `_________`
- save filename (e.g. `Mario Golf.srm`): `___________________________________`
- save path on disk: `___________________________________`
- emulator tag (look it up after the first sync — appears in the server save metadata, e.g. `retroarch-mgba`): `___________________________________`

### Server-side manipulations

You'll need to add, modify, or delete saves on the RomM server during tests. Two options:

**Option A — RomM web UI**: open `https://<your-romm>/library/<rom_id>` and use the Save Files panel. Easy for upload/delete; you cannot directly bump `updated_at`.

**Option B — `curl`**: install `jq` if you don't have it. Set:

```bash
export ROMM_URL="https://your-romm-server"
export ROMM_USER="your-username"
# Read once into the shell with: read -s ROMM_PASS && export ROMM_PASS
```

Helpful one-liners (run from a Konsole on the Deck):

```bash
# Login → cookie jar
curl -s -c /tmp/romm-cookies.txt -X POST "$ROMM_URL/api/login" \
  -d "username=$ROMM_USER&password=$ROMM_PASS"

# List saves for a rom
curl -s -b /tmp/romm-cookies.txt "$ROMM_URL/api/saves?rom_id=<ID>" | jq .

# PUT (re-upload) — bumps updated_at
curl -s -b /tmp/romm-cookies.txt -X PUT \
  "$ROMM_URL/api/saves/<save_id>" -F "saveFile=@/path/to/local.srm"

# Delete (bulk)
curl -s -b /tmp/romm-cookies.txt -X POST \
  "$ROMM_URL/api/saves/delete" -H "Content-Type: application/json" \
  -d '{"saves":[<save_id>]}'

# Get the device_syncs/is_current state for a save
curl -s -b /tmp/romm-cookies.txt \
  "$ROMM_URL/api/saves?rom_id=<ID>&device_id=<my_device_id>" \
  | jq '.[].device_syncs'
```

### Reset to clean state

When a test calls for a fresh slate:

1. Toggle plugin off.
2. Stop the plugin completely (`systemctl --user stop plugin_loader || true` is overkill; just toggling off is enough).
3. Delete or rewrite the relevant `saves.<rom_id>` block in `save_sync_state.json`. To reset just one rom: `jq 'del(.saves["<rom_id>"])' save_sync_state.json | sponge save_sync_state.json` (or hand-edit).
4. Delete or restore the local `.srm` file.
5. On the RomM server, delete or restore server saves for the rom.
6. Toggle plugin back on.

A copy of a known-good state file lives at `/tmp/save_sync_state.backup.json` if you make one before starting.

---

## Tests

### T1 — Matrix row 1 (no local, no server)

Goal: prove `compute_sync_action` returns `Skip(nothing_to_sync)` and the sync flow does no I/O.

**Setup**:
- Plugin state: no `saves.<rom_id>` entry, OR `saves.<rom_id>.files` empty. Reset rom state if needed.
- Local: no `.srm` file at the expected path. `rm -f /home/deck/retrodeck/saves/<system>/<rom_name>.srm`.
- Server: no saves for this rom. Delete via RomM UI if needed.

**Steps**:
1. Open the game-detail page in Decky.
2. Tap Play.

**Expected**:
- Game launches normally.
- No upload, no download.
- Toast (if any) says nothing was synced.
- `save_sync_state.json` after the sync still has no file entries for this rom.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T2 — Matrix row 2 (local exists, no server)

Goal: first POST upload from a fresh local file.

**Setup**:
- Plugin state: same as T1 (no entry).
- Local: a real `.srm` exists. The simplest way: launch the game once, save in-game, exit — but to keep this test purely about the upload path, we want to test pre-launch sync, so create the file directly: `dd if=/dev/urandom of=/home/deck/retrodeck/saves/<system>/<rom_name>.srm bs=1024 count=32`. (32 KB random content; valid `.srm` content is not required for this test, only its existence and a stable hash.)
- Server: no saves for this rom.

**Steps**:
1. Open game-detail page.
2. Tap Play.

**Expected**:
- Pre-launch sync runs `Upload(POST)`.
- Server now has one save in slot `default` (or whatever your `default_slot` setting is) with a timestamp-tagged filename like `<rom_name> [yyyy-MM-dd_HH-mm-ss].srm`.
- `save_sync_state.json` has `saves.<rom_id>.files["<rom_name>.srm"]` populated with `tracked_save_id`, `last_sync_hash` matching the local MD5, and `last_sync_*` fields.
- `device_syncs[me].is_current=true` on the new save (verify via `curl`).

**Pass criteria**:
- ✓ MD5 of local file equals `last_sync_hash` in state.
- ✓ Server save id equals `tracked_save_id` and `last_sync_server_save_id` in state.
- ✓ `is_current=true` for our device on the new save.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T3 — Matrix row 7 (steady-state skip)

Goal: prove a sync after T2 with no changes is a no-op.

**Setup**:
- Carry over state from T2 (do **not** reset).
- Local: untouched since T2.
- Server: untouched since T2.

**Steps**:
1. Open game-detail page.
2. Tap Play (or use Sync All Saves Now in QAM).

**Expected**:
- `compute_sync_action` returns `Skip(synced)`.
- No upload, no download. Toast shows 0 synced.
- `last_sync_check_at` in state advances; nothing else changes.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T4 — Matrix row 9 (offline edit, server still trusts us → PUT)

Goal: post-play offline edit propagates as a PUT.

**Setup**:
- State carries over from T3.
- Local: simulate a play session — `dd if=/dev/urandom of=/home/deck/retrodeck/saves/<system>/<rom_name>.srm bs=1024 count=32 conv=notrunc` (overwrites with new random content; size unchanged is fine).
- Server: untouched.

**Steps**:
1. Open game-detail page.
2. Tap Play.

**Expected**:
- `compute_sync_action` → `Upload(PUT to <tracked_save_id>)`.
- Server save id is unchanged (same id), `updated_at` advances to now.
- `last_sync_hash` in state updated to the new local MD5.
- `device_syncs[me].is_current=true` (because `confirm_download` runs after the PUT).

**Pass criteria**:
- ✓ `tracked_save_id` unchanged.
- ✓ Server `updated_at` newer than before.
- ✓ `is_current=true`.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T5 — Matrix row 10 (server moved, local untouched → silent Download)

Goal: cross-device upload from another client gets pulled silently.

**Setup**:
- State carries over from T4.
- Local: untouched.
- Server: simulate another device pushing — easiest is a `curl` PUT with new content:
  ```bash
  dd if=/dev/urandom of=/tmp/foreign.srm bs=1024 count=32
  curl -s -b /tmp/romm-cookies.txt -X PUT \
    "$ROMM_URL/api/saves/<tracked_save_id>" -F "saveFile=@/tmp/foreign.srm"
  ```
  This bumps `updated_at` and leaves our `device_syncs[me]` row stale → `is_current=false` for us.

**Steps**:
1. Open game-detail page.
2. Tap Play.

**Expected**:
- `compute_sync_action` → `Download(picked)`.
- Local file content now equals `/tmp/foreign.srm` content.
- `last_sync_hash` updated to MD5 of `/tmp/foreign.srm`.
- `device_syncs[me].is_current=true` again (auto-upserted by `GET /content?optimistic=true`).

**Pass criteria**:
- ✓ `md5sum /home/deck/retrodeck/saves/<system>/<rom_name>.srm` equals `md5sum /tmp/foreign.srm`.
- ✓ `is_current=true`.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T6 — Matrix row 12 (true conflict — Keep Local)

Goal: both sides changed → modal appears → Keep Local PUTs and resolves.

**Setup**:
- State carries over from T5.
- **Both** sides need to diverge from the new baseline:
  - Local: `dd if=/dev/urandom of=/home/deck/retrodeck/saves/<system>/<rom_name>.srm bs=1024 count=32 conv=notrunc` (simulates an offline play).
  - Server: another `curl` PUT with **different** content from another temp file (`dd if=/dev/urandom of=/tmp/foreign2.srm ...; curl -X PUT ...`).
- After both: capture the local MD5 (`md5sum /home/deck/retrodeck/saves/<system>/<rom_name>.srm`) — call it `LOCAL_HASH`.

**Steps**:
1. Open game-detail page.
2. Tap Play.

**Expected**:
- Sync conflict modal appears with both rows shown.
- Tap **Keep Local**.
- Modal closes, sync continues, game launches.
- Server save content now equals local content (server MD5 == `LOCAL_HASH`).
- `last_sync_hash` == `LOCAL_HASH`.
- `is_current=true` for our device.

**Pass criteria**:
- ✓ Modal showed both sides.
- ✓ Server file matches what local was at modal time.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T7 — Matrix row 12 (true conflict — Use Server)

Goal: same conflict trigger as T6, opposite resolution.

**Setup**:
- Carry over from T6, then re-create the conflict the same way:
  - Local: overwrite with new random content (`dd ...`).
  - Server: PUT a different new content via `curl` (`dd ...; curl -X PUT ...`).
- Capture the **server**-side MD5 by downloading: `curl -s -b ... "$ROMM_URL/api/saves/<tracked_save_id>/content" -o /tmp/server_check.srm; md5sum /tmp/server_check.srm`. Call it `SERVER_HASH`.

**Steps**:
1. Open game-detail page.
2. Tap Play.

**Expected**:
- Sync conflict modal appears.
- Tap **Use Server**.
- Local file content now equals server content (local MD5 == `SERVER_HASH`).
- `last_sync_hash` == `SERVER_HASH`.

**Pass criteria**:
- ✓ Local file overwritten with server content.
- ✓ State reflects new hash.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T8 — Matrix row 12 (true conflict — Cancel re-fires next time)

Goal: Cancel does no I/O, no state change; conflict re-fires until user picks a side.

**Setup**:
- Re-create the same conflict as T6/T7 (overwrite local + curl PUT to server with different content).
- Capture both `LOCAL_HASH` and `SERVER_HASH`.

**Steps**:
1. Open game-detail page.
2. Tap Play.
3. Modal appears. Tap **Cancel**.
4. Game launches (or sync skipped, depending on flow). Exit the game without saving.
5. Open game-detail page again.
6. Tap Play.

**Expected** after step 3:
- Modal closes immediately. No upload, no download.
- Local file unchanged (`md5sum` still == `LOCAL_HASH`).
- Server file unchanged (`SERVER_HASH`).
- `last_sync_hash` in state is **still the old baseline** (the value from before T6 — NOT `LOCAL_HASH`, NOT `SERVER_HASH`).

**Expected** after step 6:
- Modal appears again with the same two rows. (If another device intervened between steps 3 and 6, this may resolve to Skip / Download instead — that's OK and worth noting.)

**Pass criteria**:
- ✓ Cancel mutates nothing.
- ✓ Conflict re-detected on the next sync.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T9 — Matrix row 4 (recovery: tracked exists, local missing)

Goal: deleted local file is restored from the tracked server save.

**Setup**:
- Pick whichever of T6 / T7 left the cleanest state. For simplicity, run T6 (Keep Local) again first if needed so we end with `is_current=true`, baseline matches local, server matches local.
- Local: delete the file. `rm /home/deck/retrodeck/saves/<system>/<rom_name>.srm`.
- Server: untouched.
- Plugin state: untouched (`tracked_save_id`, `last_sync_hash` still point at the now-missing local).

**Steps**:
1. Open game-detail page.
2. Tap Play.

**Expected**:
- `compute_sync_action` → `Download(picked)` (matrix row 4).
- Local file recreated; content equals server content; MD5 equals `last_sync_hash` (unchanged from before).

**Pass criteria**:
- ✓ Local file restored.
- ✓ `last_sync_hash` is unchanged (because content matches the baseline).

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T10 — Matrix row 6a (POST when no entry + local newer)

Goal: a foreign-only server slot with our local file ahead by mtime → POST a new save side-by-side.

**Setup**:
- **Reset rom state**. Hand-edit `save_sync_state.json` to remove `saves.<rom_id>` (or use `jq` as in the Reset section).
- Local: `dd if=/dev/urandom of=/home/deck/retrodeck/saves/<system>/<rom_name>.srm ...` and **`touch -d "now" ...`** to make sure mtime is fresh.
- Server: from the previous tests there is at least one save (from another "device" via curl). Confirm via web UI or API that at least one save in the slot exists and that **our `device_id` is not in any of their `device_syncs` arrays** (because we wiped state, our recorded device_id is gone — but the server still has us by `server_device_id`. To get a true "no entry" condition, either delete and recreate the save via curl as a different user, OR delete our `device_syncs` row server-side via the SQL/admin tools if you have them).

If you can't easily clear the server-side `device_syncs` row, mark this test SKIP and note it — `compute_sync_action` will fall into row 7/9/10 instead of row 6a in that case, which is the same output as previous tests.

**Steps**:
1. Toggle plugin back on.
2. Open game-detail page.
3. Tap Play.

**Expected**:
- `compute_sync_action` → `Upload(POST)` because no entry on the picked save and local mtime ≥ server `updated_at`.
- A second save now exists in the slot. Original foreign save is untouched.
- New save id stored as `tracked_save_id` in state.
- New save is newest (`updated_at`).

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T11 — Matrix row 6b (no entry + server newer → Download)

Goal: same as T10 but with local mtime older than server.

**Setup**:
- Reset rom state.
- Local: `touch -d "1 hour ago" /home/deck/retrodeck/saves/<system>/<rom_name>.srm` so mtime is in the past. Content can be whatever.
- Server: at least one save with `updated_at` recent. Either run T5's `curl PUT` again to bump it, or create a fresh save via the RomM UI.

**Steps**:
1. Open game-detail page.
2. Tap Play.

**Expected**:
- `compute_sync_action` → `Download(picked)`.
- Local file overwritten with server content.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T12 — Matrix row 8 (adopt baseline)

Goal: server reports `is_current=true` for our device but we have no `last_sync_hash` yet → state silently records the baseline.

**Setup**:
- Hand-edit `save_sync_state.json` to remove only the `last_sync_hash` field for this file (keep `tracked_save_id` and other fields):
  ```bash
  jq '.saves["<rom_id>"].files["<file>"] |= del(.last_sync_hash)' state.json | sponge state.json
  ```
- Local: any content — but capture its MD5 first (`LOCAL_HASH`).
- Server: leave alone. Verify our `device_syncs[me].is_current=true` via `curl`.

**Steps**:
1. Toggle plugin on.
2. Open game-detail page (this triggers `_get_save_status_io`, which is enough to record the baseline — no Play required).

**Expected**:
- `compute_sync_action` → `Skip(synced, adopt_baseline=True)`.
- `save_sync_state.json` now has `last_sync_hash == LOCAL_HASH`.
- No upload, no download.

**Pass criteria**:
- ✓ Baseline written without any I/O.
- ✓ Subsequent Play taps run T3-style steady-state Skip.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T13 — Cross-device round trip

Goal: classic A → B → A scenario silently propagates.

This is two-device. If you only have one Deck, simulate device B with `curl` against the same RomM server using a different `device_id`. Easier path: keep running everything from the Deck and use `curl` as the second device.

**Setup**:
- Reset state, local, and server for the rom.
- Local: a `.srm` exists. POST it via the plugin (i.e. run T2). After this, our state knows the save and `is_current=true`.

**Steps**:
1. Simulate device B uploading newer content:
   ```bash
   dd if=/dev/urandom of=/tmp/B.srm bs=1024 count=32
   curl -s -b /tmp/romm-cookies.txt -X PUT \
     "$ROMM_URL/api/saves/<tracked_save_id>" -F "saveFile=@/tmp/B.srm"
   ```
2. Optionally also POST `confirm_download` as device B if you want B's `is_current=true`. For this test it's enough that **our** `is_current` flips to false (which the PUT alone does).
3. On the Deck: open game-detail, tap Play.

**Expected**:
- Silent download. Local now matches `/tmp/B.srm`.
- Modal does NOT appear (we did not edit local; only one side changed → matrix row 10, not row 12).

**Pass criteria**:
- ✓ No modal.
- ✓ `md5sum local` == `md5sum /tmp/B.srm`.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T14 — Rollback flow

Goal: roll the local + server back to an older save id and confirm cross-device propagation.

**Setup**:
- Need at least 2 server saves in the slot for this rom so there's a "previous version" to roll to. Run T2 (POST) followed by T4 (PUT) followed by another local edit + sync — that gives you a couple of stacked versions, but PUT updates the same id, so you only get one save id with multiple `updated_at` bumps.
- For real version history you need **multiple POSTed save ids**. Easiest: change `default_slot` setting between syncs OR manually create extra saves via curl POST using a unique slot, then move them. Or simply recognise that a single-save rollback doesn't really roll back content — you need history.
- If `autocleanup_limit` is high enough and slots already keep history, RomM stores prior versions — check what the version-history UI in the SAVES tab shows for this rom. If it's empty, manually create older versions:
  ```bash
  for i in 1 2 3; do
    dd if=/dev/urandom of=/tmp/v${i}.srm bs=1024 count=32
    curl -s -b /tmp/romm-cookies.txt -X POST \
      "$ROMM_URL/api/saves?rom_id=<ROM_ID>&emulator=<EMU>&slot=default" \
      -F "saveFile=@/tmp/v${i}.srm"
    sleep 2
  done
  ```
  (Three increasing-timestamp saves in the same slot. Adjust `<EMU>` to your tag, e.g. `retroarch-mgba`.)

**Steps**:
1. Open game-detail → SAVES tab.
2. Pick a non-newest version and tap **Rollback to this version**.
3. Confirm any modal.
4. Verify server: that picked save's `updated_at` is now NEWEST in the slot.
5. Open game-detail again, tap Play.
6. Open another machine's view (or `curl` from a fake "device B"): they should see the rolled-back save as newest and download it.

**Expected**:
- Step 2 returns `{"status": "ok"}` in plugin logs.
- Server: picked save's `updated_at` ≥ all others in slot.
- Local file content equals picked save's content.
- `last_sync_hash` in state matches local content.
- `device_syncs[me].is_current=true` for the picked save (because `confirm_download` ran).
- After step 5: matrix row 7 `Skip(synced)` — no further sync needed.
- Step 6 (other device): `Download(picked)` — they pull our rolled-back content silently.

**Pass criteria**:
- ✓ Rollback ok.
- ✓ Cross-device propagation works (newest is the rolled-back save).
- ✓ Our device flagged current for that save.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T15 — Per-rom asyncio.Lock

Goal: simultaneous pre_launch_sync + sync_all serialise without races.

**Setup**: any rom in a steady state.

**Steps**:
1. Open the QAM panel and the game-detail panel side by side (the QAM side panel can stay open while the game-detail page is foregrounded).
2. From game-detail, tap Play.
3. Within ~200ms (best effort), tap **Sync All Saves Now** in the QAM.

**Expected**:
- Both sync attempts complete.
- No race-y log lines (look for two interleaved `_sync_rom_saves(<rom_id>)` log blocks for the same rom).
- State consistent — no mismatched fields after both finish.

This is best-effort given the manual timing. If you can reproduce a race, capture the log lines and we'll fix it.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

### T16 — State migration (legacy `dismissed_newer_save_id`)

Goal: the plugin loads state from before the rewrite without errors and silently drops the legacy field.

**Setup**:
- Toggle plugin off.
- Hand-edit `save_sync_state.json` to inject the legacy field on at least one file:
  ```bash
  jq '.saves["<rom_id>"].files["<file>"].dismissed_newer_save_id = 999' state.json | sponge state.json
  ```
  Verify with `jq '.saves["<rom_id>"].files'` that the field is present.
- Toggle plugin on.

**Steps**:
1. Watch logs at startup — no errors related to state load.
2. Trigger any sync (open game-detail page is enough to write state again).
3. Re-read `save_sync_state.json`.

**Expected**:
- Field `dismissed_newer_save_id` is **gone** from the file after the next state write.
- No errors logged.
- All other fields preserved.

**Pass criteria**:
- ✓ Field removed.
- ✓ Plugin functions normally afterwards.

**Status**: [ ] Pass / [ ] Fail / [ ] Skip
**Notes**:

---

## After all tests

If everything passes:
- Note any cosmetic issues (toast wording, modal layout) for follow-ups.
- Push the branch and open a PR back to `main`.
- We can close out tasks #10 and #11 in the project tracker.

If anything fails:
- Don't keep going against a broken state — let me know which test failed and we'll triage. Most failures are reproducible from the setup blocks here.

## Quick reference — which test triggers which matrix row

| Matrix row | Test |
|---|---|
| 1 | T1 |
| 2 | T2 |
| 3 | (covered implicitly by T11 reset path; explicit test deferred) |
| 4 | T9 |
| 5 | (covered implicitly when state is reset and server has saves; deferred) |
| 6a | T10 |
| 6b | T11 |
| 7 | T3 |
| 8 | T12 |
| 9 | T4 |
| 10 | T5, T13 |
| 11 | (deferred — needs no-baseline + is_current=false setup; rare) |
| 12 (Conflict — Keep Local) | T6 |
| 12 (Conflict — Use Server) | T7 |
| 12 (Conflict — Cancel) | T8 |

Plus T14 (rollback), T15 (lock), T16 (migration).
