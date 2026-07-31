---
name: save-conflict-test-path
description: How to produce a save conflict for testing. The newest-wins matrix in domain/sync_action.py has exactly one path that yields Conflict — _decide_when_not_current. Conflict needs both sides diverged AND our device holding a (now-stale) sync entry on the newest server save. Also covers the device_syncs-empty gotcha (RomM 4.8.1 requires device_id query param), how a device gets its sync row (POST upload / download / confirm_download upsert; PUT does NOT on 4.8.1 — confirm_download fires after PUT as workaround, tracked in #748), and the no-device-authorship-column constraint (#276 records PUT uploads to own_upload_ids; display-only, doesn't affect matrix).
type: project
---

# Producing a save conflict for testing — the one conflict path

The newest-wins matrix (`domain/sync_action.py`) has exactly **one** path that yields a `Conflict`:
`_decide_when_not_current`. Every other branch is download/upload/skip ("newer wins"). Conflict fires iff ALL of:

1. The **newest** server save in the slot has OUR device's `device_syncs` entry with `is_current=False` (server moved
   past us).
2. `local_file` exists.
3. `last_sync_hash` exists (we have a baseline).
4. `local_hash != last_sync_hash` (local also diverged).

So a conflict needs **both sides diverged** AND our device to already hold a (now-stale) sync entry on the newest save.

**`device_syncs` requires the `device_id` query param — NOT a regression (re-verified 2026-05-20, RomM 4.8.1):** RomM
only computes `device_syncs` _relative to a queried device_. `GET /api/saves?rom_id=` WITHOUT `device_id` returns
`device_syncs=[]` for every save; WITH `&device_id=<our id>` it returns our device's single sync row (`is_current`,
`last_synced_at`). The server returns ONLY the queried device's row, never other devices' — same in 4.8.1 and master. So
the earlier "device_syncs always empty / conflict path dead" reading was an inspection artifact (the probe omitted
`device_id`), not a regression. All 11 `list_saves` call sites in the plugin pass `device_id`, so the matrix receives
populated rows and branches 4/5 ARE reachable. Conflict detection is functional.

How a save gets our sync row (so `is_current` can be true): POST upload with `device_id` upserts it;
`download_save_content(..., optimistic=True)` upserts it (merely downloading marks us current — `optimistic` defaults
true); `confirm_download` (POST `/saves/{id}/downloaded`) upserts it. On 4.8.1 **PUT does NOT upsert** — so
`do_upload_save` calls `confirm_download` after every upload to mark us current (load-bearing for the PUT path on 4.8.1;
redundant-but-harmless on master, where PUT upserts). Tracking issue to drop the confirm once min RomM ≥ 4.9.x: #748.

The RomM `Save` model has NO device-authorship column (only `user_id`) — neither 4.8.1 nor master records who _uploaded_
a save, and a sync row can't distinguish upload from download. So "did THIS device upload this version" is local-only:
`own_upload_ids`. #276 fixes it to record PUT uploads too (was POST-only) — display attribution only, does NOT affect
the matrix.

Device registration verified working: `steamdeck` (our `server_device_id`) is registered, `sync_enabled`, `last_seen`
current; the `ensure_device_registered` → `update_device` heartbeat bumps `last_seen` on each sync.

Transport note: RomM auth is Basic (`auth_header()` in `adapters/romm/http.py`); a save update is a multipart
`PUT /api/saves/{id}` with form field `saveFile`. Never log secret values.

Related: #276 (PUT uploads don't update `own_upload_ids` → wrong "(this device)" attribution) is in the same save-sync
device-tracking area.
