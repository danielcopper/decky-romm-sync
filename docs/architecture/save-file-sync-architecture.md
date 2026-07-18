# Save File Sync Architecture

## Overview

decky-romm-sync provides bidirectional save file synchronization between RetroDECK and a self-hosted RomM server. Saves
are uploaded after play sessions and downloaded before game launch, enabling seamless multi-device play.

The scope is **per-game RetroArch save files** across all systems that use RetroArch cores via RetroDECK (NES, SNES, GB,
GBC, GBA, Genesis, N64, PSX via RetroArch cores, Saturn, Dreamcast, PC Engine, and more). Each system's full save-file
**set** is discovered and synced — the default `.srm` / `.rtc` / `.sav` plus system-specific extensions (e.g. Saturn
`.bkr` / `.bcr` / `.smpc`, NDS `.dsv`, Sega CD `.brm`); the extension list lives in `domain/save_extensions.py`. Every
file syncs **independently against the server save sharing its own canonical target**, so a multi-file set never
cross-mixes extensions. Standalone emulator saves (PCSX2, DuckStation, Dolphin, PPSSPP, melonDS, etc.) are deferred to
Phase 7.

## RomM Save API

Requires RomM >= 4.9.0 (release or higher core). Pre-releases at the exact floor (`4.9.0-beta`, `4.9.0-alpha.1`) rank
below `4.9.0` and are rejected; a higher core with a suffix (`4.9.1-beta`) passes. The plugin rejects servers below the
floor with `reason: "version_error"`.

| Endpoint                                                 | Method | Notes                                                                                                                                                                                                                                                                                                  |
| -------------------------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/api/saves?rom_id={id}`                                 | GET    | Returns array. Each item now includes `slot`, `file_name_no_tags`, `file_extension`, `content_hash`, and `device_syncs` array.                                                                                                                                                                         |
| `/api/saves/{id}`                                        | GET    | Single save metadata with v4.7 fields                                                                                                                                                                                                                                                                  |
| `/api/saves?rom_id={id}&emulator={emulator}&slot={slot}` | POST   | Creates a new save entry. Slot-aware: `slot=default` causes RomM to append a timestamp to the filename (e.g. `Game.srm` becomes `Game [2026-03-24_15-18-50].srm`). Same filename + same slot = upsert. Different slot = new entry.                                                                     |
| `/api/saves/{id}`                                        | PUT    | Updates file content only. No metadata changes, no new entry created.                                                                                                                                                                                                                                  |
| `/api/saves/{id}/content`                                | GET    | Binary download by save ID (new in v4.7)                                                                                                                                                                                                                                                               |
| `/api/devices`                                           | GET    | List all registered devices for the authenticated user. Returns array of `{id, name, platform, client, client_version, last_seen, created_at, ...}`.                                                                                                                                                   |
| `/api/devices`                                           | POST   | Register a device. Accepts hostname, platform, client info. Returns `device_id` (UUID).                                                                                                                                                                                                                |
| `/api/devices/{id}`                                      | DELETE | Remove a device registration. Returns 204 No Content. PATCH (rename) is not supported (405).                                                                                                                                                                                                           |
| `/api/saves/delete`                                      | POST   | Bulk delete saves by ID. Body: `{"saves": [id1, id2, ...]}`. Returns result dict.                                                                                                                                                                                                                      |
| `/api/sync/negotiate`                                    | POST   | Open a 4.9 sync session. Body: `{device_id, saves: ClientSaveState[]}`. Returns `{session_id, operations[], total_*}` — the server's `upload`/`download`/`conflict`/`no_op` verdicts (+ a `reason`). Detects but never resolves; opening cancels this device's prior sessions.                         |
| `/api/sync/sessions/{id}/complete`                       | POST   | Close a negotiated session. Body: `{operations_completed, operations_failed}`. Returns `{session}`.                                                                                                                                                                                                    |
| `/api/play-sessions`                                     | POST   | Ingest a batch (max 100) of `{rom_id, start_time, end_time, duration_ms}` under a top-level `device_id`. Additive per-device union; dedup on `(user_id, device_id, rom_id, start_time)`. Playtime, decoupled from save-sync ([ADR-0018](../adr/0018-native-play-session-tracking-additive-ingest.md)). |
| `/api/play-sessions?rom_id={id}`                         | GET    | List a ROM's stored play sessions (needs the `roms.user.read` scope). Summed by `duration_ms` for the reconcile `max()`; degrades to local-only without the scope.                                                                                                                                     |

**New parameters on POST:**

- `slot` — slot name (e.g. `"default"`). If omitted, save has `slot=null` (legacy behavior).
- `autocleanup` — whether RomM prunes old stacked versions. Defaults to **false**.
- `autocleanup_limit` — max save versions retained per slot (default: 10). Inert unless `autocleanup=true` is sent
  alongside it.

The `negotiate` / `complete` endpoints are wired into the adapter (`RommApiAdapter.negotiate_sync` /
`complete_sync_session`), typed by the `models/sync.py` schemas (`ClientSaveState`, `SyncOperation`,
`SyncNegotiateResponse`, …). As of [#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276) /
[ADR-0017](../adr/0017-client-baseline-detection-authoritative-negotiate-is-transport.md) they are the sync
**transport**, not the sync **brain**: for a confirmed non-legacy ROM the run opens a negotiate session (per-device
serialization) but the server's returned `operations` are **discarded**. The client's `compute_sync_action` decides
every action for every ROM, from `list_saves` — including legacy `slot:null` saves, which RomM cannot address through
the negotiate inventory param. See
[Negotiate as transport; one decision kernel](#negotiate-as-transport-one-decision-kernel).

- `device_id` — server-registered device UUID. Used to populate `device_syncs` per save.

**New fields on save metadata:**

- `slot` — the slot this save belongs to (string or null)
- `file_name_no_tags` — base filename without timestamp tags (e.g. `Game` from `Game [2026-03-24_15-18-50].srm`)
- `file_extension` — file extension (e.g. `srm`)
- `content_hash` — RomM's content hash of the save: a plain MD5 for a single file, or a per-entry combined MD5 for a
  zipped multi-file save (eliminates the download-and-hash slow path)
- `device_syncs` — array of per-device sync records: `device_id`, `device_name`, `is_current`, `last_synced_at`

The plugin reproduces this `content_hash` byte-for-byte so a save's local and server hashes agree and sync converges:
single-file MD5 via `SaveFileStore.checksum_md5`, and the zip per-entry scheme via `SaveFileStore.content_hash` →
`domain.save_hash.combine_zip_entry_hashes` (sorted `name:md5(entry)` lines joined by `\n`, then MD5'd; dispatch is by
`zipfile.is_zipfile`, a content sniff, not the extension). A file that `is_zipfile` accepts but that cannot actually be
read as a zip — a corrupt or truncated archive, an encrypted entry, or a compression method this Python runtime lacks —
falls back to the plain `checksum_md5` rather than raising, so one poison save can never abort the whole sweep (#1470);
RomM's server degrades the same file to `content_hash=None`, so the kernel's truthiness guards reject a `None`-side
identity match and the fallback can never manufacture a false byte-identical match — it only keeps the local drift
baseline working. This hash reproduction is what the sync decision needs
([ADR-0016](../adr/0016-save-sync-hands-detection-to-romm-negotiate.md) /
[ADR-0017](../adr/0017-client-baseline-detection-authoritative-negotiate-is-transport.md) / #1234): the client's
`compute_sync_action` decides `upload` / `download` / `conflict` / `no_op` without a download-and-rehash, for every ROM
— including legacy `slot:null` saves, which RomM cannot address through the negotiate inventory param.

**Server-hash baseline; parity as the no-history fallback (#1468).** The identity question — "is my local file
byte-identical to that server save?" — is answered by `domain.sync_action._local_matches_server`, a two-route
disjunction used at matrix rows 6d / 11a and the `resolve_upload_conflict` 409 backstop:

- **Provenance (primary).** At each sync boundary the plugin stores the server's own `content_hash` alongside the local
  baseline, in `FileSyncState.last_sync_server_hash` (the `rom_save_files.last_sync_server_hash` column, added by
  migration 017). While the local file is unchanged since that baseline (`local_hash == last_sync_hash`), identity is
  proven by `last_sync_server_hash == server.content_hash` — two hashes RomM itself produced, so this route holds even
  if the plugin's local hashing ever drifts from the server's. This is the robustness the issue buys: identity no longer
  depends on the plugin's reimplementation staying byte-for-byte identical to RomM's scheme.
- **Parity (fallback).** A file with **no sync history on this device** (fresh reinstall, copied SD card, second device)
  has no stored server hash, so identity falls back to the direct `local_hash == server.content_hash` comparison — kept
  correct by the hash reproduction above (#1457). Branch 5's no-baseline slice and true fresh installs can only ever use
  this route, by design. Watch item: RomM's zip scheme is undocumented internal behavior and its author has publicly
  sketched a metadata-based variant that does not match the shipped code — re-verify the zip branch of this fallback
  against the RomM source on server version bumps. If it ever drifts, the fallback degrades to spurious conflict prompts
  (never data loss); the stored-server-hash primary route is unaffected.

The baseline pair is written together at the recorded-baseline writer sites (`update_file_sync_state` on upload/download
and the keep_local adopt-without-upload path) with honest provenance per flow — the upload response's `content_hash`
(RomM hashed the bytes it received), the adopted server save's `content_hash` on download — recording `None` when a
response/save carries none rather than fabricating one from a local recomputation. The hash-only skip-adopt path
(`update_baseline_hash`) keeps the stored server hash only while the local hash is unchanged and clears it when the hash
changes, so a stored server hash always truthfully pairs with its `last_sync_hash`. The negotiate inventory
(`build_save_inventory`) still sends the locally-computed `content_hash`: echoing the stored hash buys nothing while the
computed hash already is RomM's scheme (#1457), and it stays the client's own statement of local content.

Every local hash that reaches this comparison — the matrix's `local_hash`, the post-op baseline (`last_sync_hash`), the
drift check, the slot-switch pending-changes check, and the keep_local adopt-without-upload probe — is computed with
`SaveFileStore.content_hash`, never the whole-archive `checksum_md5` (#1457). For a single-file save the two are
bit-identical, so this is a no-op there; for a zip container the whole-archive MD5 could never equal
`server.content_hash`, so feeding it would silently degrade the byte-identical dedup and adoption to spurious conflicts.
Within one sync run the same file is hashed by several passes (negotiate inventory, the matrix, the baseline write);
`SaveFileStore.hash_memo_scope` memoizes the digest keyed by `(path, mtime_ns, size)` so it is read once, bounded to
that run and discarded on exit.

### Negotiate inventory builder (`SaveService.build_save_inventory`)

The negotiate POST sends this device's local save inventory; `SaveService.build_save_inventory()` assembles it as a
`list[ClientSaveState]`. It walks the `rom_save_states` aggregate and keeps only ROMs **in scope** — `slot_confirmed` is
true **and** `active_slot` is a real (truthy) slot, which excludes both the unset `None` and the legacy `""` slot. For
each in-scope ROM it enumerates the local save files via `RomInfoService.find_save_files` and emits **one
`ClientSaveState` per file** (per-file granularity): a confirmed ROM with no local files contributes nothing, and a ROM
with several local files yields one entry each. Per entry, `content_hash` is always set via `SaveFileStore.content_hash`
— the zip-aware RomM-parity hash above, never the single-file `checksum_md5` — and `updated_at` is the local file's
mtime rendered as a UTC ISO-8601 string (`domain.iso_time.epoch_to_iso`, the round-trip inverse of
`parse_iso_to_epoch`).

`build_save_inventory(rom_id=None)` builds the whole-device inventory (the bulk `sync_all_saves` pre-negotiate); a
concrete `rom_id` scopes it to that one ROM (the single-ROM negotiate trigger). The in-scope predicate is identical
either way. The builder feeds the negotiate transport below (#1234 / ADR-0017). It is single-file-first; the
multi-file-per-slot collision case (several local files mapping to one slot) is a Phase 4 concern tracked in #1235.

### Negotiate as transport; one decision kernel

`SyncEngine._run_rom_sync` runs the **same** path for every ROM — confirmed non-legacy and legacy `slot:null` alike
([ADR-0017](../adr/0017-client-baseline-detection-authoritative-negotiate-is-transport.md), superseding the
detection-handoff of [ADR-0016](../adr/0016-save-sync-hands-detection-to-romm-negotiate.md)):

1. Load the ROM's `RomSaveState`.
2. Fetch the slot's server saves with `list_saves`.
3. Run `compute_sync_action` per file (the decision matrix below).
4. Dispatch each outcome through `_dispatch_sync_action` — POST / PUT / GET plus the `.romm-backup` quarantine, the
   #1062 shrink guard, and the per-file baseline writes (`update_file_sync_state`).

There is **no** op→action fork any more: the client's kernel is the sole authority, and `StatusService` (the read-only
SAVES-tab path) runs the identical kernel, so the sync run and the game-detail status can never disagree. The wizard
gate still holds — an unconfirmed ROM never syncs automatically — so the "user decides on ambiguity" invariant is
unchanged.

**`negotiate` is the transport, not the brain.** When a ROM has a confirmed non-legacy slot, `_run_rom_sync` opens a
negotiate session around the kernel run and closes it in a `finally`. The session buys two things and only two:

- **Per-device serialization** — `negotiate` cancels this device's prior in-flight sessions server-side, reinforcing the
  in-process single-owner gate (#1202).

Playtime does **not** ride this session: it must be recorded for every ROM on every exit (including save-sync-off and
unconfirmed-slot ROMs), so it ingests through the standalone `/api/play-sessions` endpoint instead
([ADR-0018](../adr/0018-native-play-session-tracking-additive-ingest.md), which narrows the "ingest rides `negotiate`"
aside of ADR-0016/0017). The server's returned `operations` are **discarded** — detection is the client's
`compute_sync_action`. Opening the session is best-effort: a `negotiate` failure is non-fatal, the run simply proceeds
**without** a session (the kernel still syncs). Legacy `slot:null` ROMs open no session at all (RomM cannot address
`slot:null` through the negotiate inventory param) but take the identical kernel path. The `complete` close is likewise
**non-fatal** — a session the server never hears closed times out and is cancelled by the next `negotiate`, so a failed
`complete` never fails the run.

**Cross-device pulls come from `list_saves`, not an op.** A confirmed ROM with a save on the server but **no local file
here** is picked up by the matrix directly: `list_saves` returns the server save, it is grouped by its canonical local
target with no matching local file, and `compute_sync_action` returns `Download` (matrix rows 3 / 4 / 5). No negotiate
`download` op is needed or consulted. The save-sort migration guard still suppresses a server-only download while a
migration is pending (#238).

**Upload safety is the 409 backstop, not a server verdict.** Every upload the kernel dispatches on the automatic path is
a POST with `overwrite=false` — see [Upload-time conflicts (the 409 backstop)](#upload-time-conflicts-the-409-backstop)
below. The old in-place PUT is no longer on the automatic path.

On the automatic path there is no PUT-in-place to bump, so the #748 "drop the PUT-bump" work is **moot** for it.
`add_save` (POST) already upserts this device's `device_save_sync` row (`last_synced_at = updated_at`) and serializes it
into the response's `device_syncs`, so `is_current` is true for us the moment the POST returns — the follow-up
`confirm_download` ack is redundant and is **skipped** when the response proves us current (`_confirm_upload_sync`,
#1458). The one path that still needs the ack is `add_save`'s content-dedup early-return: a byte-identical
`overwrite=false` POST returns the matching save **before** the upsert with `is_current=false`, so the ack is the only
writer of our sync row there. The version-switch flow (`versions.py`) routes through the same `_confirm_upload_sync`
after its PUT — the PUT upserts too, so the ack is at most an idempotent re-ack (and is skipped if the PUT response
already proves us current) — and is out of scope here — see [Version Switch Flow](#version-switch-flow-rollback).

## Save Slots

RomM v4.7 introduces **save slots** — named containers for save files. This enables multi-save workflows (e.g.,
different save states per device).

### How slots work

- Each save on RomM belongs to a slot (or has `slot=null` for legacy pre-slot saves)
- Save identity on RomM: `(user_id, rom_id, filename)` **within a slot**
- `POST /api/saves` with `slot=default` causes RomM to append a timestamp to the filename: `Game.srm` becomes
  `Game [2026-03-24_15-18-50].srm`
- Same filename + same slot = overwrites (upsert). Different slot = new save entry.
- `PUT /api/saves/{id}` updates file content only, no metadata changes. No new entry created.
- `autocleanup_limit` parameter controls how many stacked versions are retained per slot

### Our default behavior

- Every game gets a `default` slot (configurable in QAM settings as "Default Save Slot")
- First upload = POST (creates save entry with timestamp filename, server assigns ID)
- Automatic uploads carry `autocleanup=true` together with the user-configured `autocleanup_limit` (QAM "Auto-cleanup
  limit"), so RomM prunes each slot back to the cap as versions stack.
- All subsequent syncs = POST a new version (`overwrite=false`, 409-backstopped), not an in-place PUT (#1276 /
  [ADR-0017](../adr/0017-client-baseline-detection-authoritative-negotiate-is-transport.md)). RomM stacks the versions
  and prunes them to `autocleanup_limit`.
- Single-device flow: one current head per game per slot, with older versions retained up to `autocleanup_limit`.
- Multi-device: newest-wins across devices; each device tracks the current head via `tracked_save_id`.

### Switching slots

`switch_slot` makes the active slot, the local saves directory, and per-file tracking coherent with the chosen slot in
one locked critical section (the per-rom `asyncio.Lock` — see the "Per-rom asyncio.Lock" section). After the pre-checks
pass (sync enabled, ROM installed, not a content-dir layout, no un-uploaded local changes on tracked files, server
reachable):

1. The active slot is flipped in memory.
2. Every local save file the target slot does **not** provide is quarantined into `.romm-backup` (never deleted
   outright) and dropped from tracking — so no stale extension (e.g. a `.rtc` left behind when the new slot holds only
   `.srm`) lingers to upload into the new slot, and a never-synced local save is always recoverable (#965, #1058).
3. For each canonical local target the new slot **does** provide, the **newest** server save by `updated_at` is
   downloaded. Two server saves mapping to one target collapse to the newest, so the on-disk result and
   `tracked_save_id` are deterministic, not server-list-order dependent (#1058). The download backs up the file it
   overwrites through the same `.romm-backup` quarantine.
4. The flipped slot + tracking are persisted once, **regardless of partial download failure**: a failed leg still
   persists this coherent state and returns `reason="switch_incomplete"` so the caller can retry — the completed targets
   are already correct, and a failed target re-resolves as `Download` on the next sync. Saves are never carried between
   slots; the switch only downloads or quarantines, never uploads.

An empty target slot is just the case where step 3 is a no-op: every local file is quarantined, tracking is cleared, and
the slot starts fresh — with every prior save recoverable under `.romm-backup`.

#### Inside `.romm-backup` — naming and retention

Backups live in `<saves_dir>/.romm-backup` and are named `<name>_<ts>[_<n>]<ext>`, where `<ts>` is the `YYYYMMDD_HHMMSS`
quarantine time. When several files of one slot are backed up within the same second (so the base `<name>_<ts><ext>`
name would collide), a `_<n>` counter (`_1`, `_2`, …) is appended so an earlier backup is never overwritten by a later
one. The folder is capped at the **newest 10** backups per save file: each quarantine prunes the older copies of that
same file beyond the cap, bounding disk use on the Deck while keeping a deep-enough recovery net. The cap is per save
file — backups of a different save file in the same folder are never pruned by another file's quarantine. One deliberate
exception: the backup a quarantine just wrote is never pruned in that same call, so under sustained same-second churn
the folder may briefly hold one extra copy (11) — honouring the cap by deleting the just-saved file would defeat the
backup.

### The `none` slot (legacy) — a migration source, no longer a target

- Saves uploaded before v2 (or without the slot parameter) have `slot=null`.
- These are separate entries from `slot="default"` — different slot = different save.
- **Legacy `slot:null` is retired as a confirmable target**
  ([#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276) /
  [ADR-0017](../adr/0017-client-baseline-detection-authoritative-negotiate-is-transport.md)): a ROM can no longer be
  confirmed onto the legacy slot. Every confirmed slot is now a real, addressable name. The Slot Setup Wizard detects
  legacy saves and offers to **migrate them into a named slot**, never to "track legacy in place."
- **Migration `005`** (`005_unconfirm_legacy_slot_confirmations.sql`) un-confirms any ROM previously confirmed in legacy
  mode — `UPDATE rom_save_states SET slot_confirmed=0 WHERE active_slot IS NULL AND slot_confirmed=1`. No save data is
  touched; the wizard simply reappears for that ROM and the user re-picks a named slot (optionally migrating the legacy
  saves in). `resolve_default_slot` never returns `None` — a blank/unset default coerces to `"default"`.
- Legacy `slot:null` survives **only** as a migration **source**. `domain/save_slot.py` (`normalize_slot`) still defines
  the equivalence class `slot ∈ {null, ""}` (state stores `active_slot=None`, the persisted slots map keys it `""`, and
  the server returns `slot: null`) so those saves can be read and deleted on the wire (below), but they are never the
  active slot of a confirmed ROM.

#### Addressing legacy saves on the wire (#1061)

RomM filters the `slot` query param by **exact string match**, and legacy saves are stored as `slot: null` — which **no
param value can address** (`&slot=` matches only `slot==""`; `&slot=null` matches the literal string `"null"`; both
return `[]`). The **only** way to retrieve legacy saves is to **omit the `slot` param** (the server then returns every
save for the ROM) and **filter client-side** for `slot ∈ {null, ""}`.

This is the core invariant for every per-slot server read/delete (`get_slot_saves`, `get_slot_delete_info`,
`delete_slot`): legacy → `slot_query_param(...) == None` (param omitted) + `save_in_slot(...)` client filter; a named
slot → `&slot=<name>` (server filters) **and** the same client re-filter (defence in depth). A legacy `delete_slot`
therefore deletes **only** the `slot: null` saves and never touches named slots. Sending `&slot=` (empty) for a legacy
op was the bug: the server returned `[]`, the local tracking was cleared, and the slot resurrected on the next merge
(zombie slot).

The **active-slot matching filter** applies the same funnel from the other direction. The matrix sync run,
`get_save_status`, and rollback narrow the fetched saves through `MatrixExecutor.filter_server_saves_to_slot`, and
`switch_slot` client-filters its fetch the same way — every one keeps only `save_in_slot(save, active_slot)`. So a
legacy `slot:null` save belongs **only** to the legacy slot and is **isolated from every named slot, including
`"default"`** ([#877](https://github.com/danielcopper/decky-romm-sync/issues/877)): it never enters a named slot's
`compute_sync_action` inputs (no spurious download or conflict from a newer legacy head), its status counts, or its slot
listing, and a brand-new named slot is genuinely empty. The legacy saves stay readable through the legacy bucket
(`get_slot_saves(rom, "")` and the Setup Wizard's server-slot grouping) — the null bucket is deliberately separate from
`"default"`, never merged onto or aliased with it. The original bug matched `slot == active or slot is None` under every
named slot (fixed by #1061); routing the switch fetch through the shared `save_in_slot` funnel closes the last
hand-rolled site.

The **upload** side honours the same equivalence (`MatrixExecutor._resolve_upload_slot`): a sync on the legacy slot
(`active_slot=None` with a populated `slots` map) uploads with the `slot` param **omitted**, so the server stores a
`slot: null` save. Only a brand-new ROM (no `slots` yet) seeds the configured default slot for its first sync. Since
#1276 retired legacy as a confirmable target, this `active_slot=None`-confirmed state is no longer newly created — it
lingers only until migration `005` un-confirms the ROM — but the equivalence stays defined so any residual legacy
tracking still reads and deletes correctly. Returning `"default"` for `active_slot=None` was a sibling of the original
#1061 bug — a save played on the legacy slot was misfiled into the default slot, so switching back to legacy found
nothing on the server.

### Confirming a slot (`confirm_slot_choice`)

The wizard confirms a slot through `confirm_slot_choice(rom_id, chosen_slot, migrate, migrate_from_slot)`:

- `chosen_slot` **must be a real slot name.** A `null`, empty, or whitespace-only value is rejected up front with
  `{success: false, reason: "invalid_slot_name", …}` before the aggregate is touched — legacy confirm is retired, so
  there is no longer a "confirm legacy mode" branch
  ([#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276)). `confirm_slot` on the aggregate mirrors this:
  it raises on an empty/`None` name.
- `migrate` is an explicit boolean — the default (`false`) never migrates. When `true`, saves are migrated from
  `migrate_from_slot` (`null` = the legacy `slot:null` **source**) into the named `chosen_slot`, and a server save is
  deleted from the old slot **only if it was successfully re-uploaded** into the new one; non-matching saves are left in
  place and reported (so a save uploaded under a different ROM filename by another device is never destroyed). This is
  how legacy saves reach a named slot now that legacy is no longer a confirmable target.

### Not yet implemented

- Manually selecting a specific save if multiple exist in one slot

## Device Registration

Each machine running the plugin registers as a device with the RomM server. This allows RomM to track which device
uploaded each save.

1. On first use with save sync enabled, the plugin calls `POST /api/devices` with the friendly device label (`name`),
   platform, client info, and the contents of `/etc/machine-id` as the `hostname` fingerprint
2. Server returns a `device_id` (UUID). Registration writes it to `kv_config["device_id"]` **first** — the `device_id`
   is the authoritative "registered" signal — then writes the device label to `settings.json` as a **best-effort** step
   (the two live in separate stores per [ADR-0003](../adr/0003-json-sqlite-persistence-boundary.md), so the two writes
   can't be one atomic op). A failed label write leaves a fully registered, usable device (valid `device_id`,
   prior/default label) instead of a broken half-state, and logs at debug; the in-memory settings dict is rolled back so
   an unsaved label never lingers
3. This ID is passed to `list_saves` (populates `device_syncs` per save) and `upload_save` / `download_save_content`
   (tracks sync status)
4. `device_syncs` array on each save shows per-device sync status: `device_id`, `device_name`, `is_current`,
   `last_synced_at`
5. `is_current = false` means another device uploaded since our last sync
6. Server returns HTTP 409 on POST when device has stale sync record (additional safety net)

### Why `/etc/machine-id` is the fingerprint

RomM dedupes devices by fingerprint — `mac_address`, OR `hostname` + `platform` — and returns the existing device
instead of minting a duplicate (`allow_existing` defaults true). The `name` field is **not** fingerprinted, so without a
stable fingerprint every local-state wipe (the SQLite reinstall path) would create a fresh duplicate device on each
reinstall.

The plugin sends `/etc/machine-id` as the RomM `hostname`: it is machine-derived (survives a reinstall), unique per
device (two Steam Decks stay distinct), and stable. The real OS hostname is deliberately **not** sent — two stock Steam
Decks both report `steamdeck`, so a `hostname` + `platform` fingerprint built from the OS hostname would collide them
into one server device. The friendly OS hostname remains the display-only `name`. When `/etc/machine-id` is unreadable
the `hostname` field is omitted entirely, degrading to no-fingerprint behaviour rather than sending a colliding value.

### RomM account requirement

Save games in RomM are tied to the authenticated user account. Users must use their own RomM account (not a
shared/generic one) so saves are correctly attributed per user, per device.

## Emulator Tags

The `emulator` parameter on RomM save uploads determines the server-side folder path:
`saves/{system}/{rom_id}/{emulator}/`

**Format:** `retroarch-{core}` where core is the libretro core name without `_libretro` suffix, lowercased.

- Examples: `retroarch-mgba`, `retroarch-snes9x`, `retroarch-swanstation`
- Fallback: `retroarch` if core resolution fails (e.g., ES-DE config parse error)

**Important:** Emulator tag is **immutable** on RomM — set on creation, cannot be changed later. This means saves
created before v2 have `emulator=retroarch` and will keep that tag. New saves created with slots get the correct
`retroarch-{core}` tag.

For future standalone emulator support (Phase 9): just the emulator name, e.g. `duckstation`.

## Sync Decision Algorithm

Each sync run picks one action per save file: `Skip`, `Upload`, `Download`, or `Conflict`. The decision is computed by a
pure function — no I/O, no service or adapter imports — so behaviour is fully driven by inputs and is exhaustively
unit-tested.

### Inputs

- **`local_file`** — `{filename, path, size, mtime}` for a file on disk, or `None` if no local file exists for this
  filename.
- **`server_saves_in_slot`** — RomM save dicts already filtered to the active slot.
- **`files_state`** — the per-filename baseline from the ROM's save state — the `FileSyncState` value object on the
  `RomSaveState` aggregate (persisted in the `rom_save_files` table), may be empty for a never-synced file. Carries
  `tracked_save_id`, `last_sync_hash` (our own hash of the local file at the last sync — the drift baseline),
  `last_sync_server_hash` (the server's own `content_hash` for that same sync — the provenance anchor for the identity
  check, `None` before this field existed or for a hash-only skip-adopt), `last_sync_server_updated_at`,
  `last_sync_local_mtime`, etc.
- **`device_id`** — this device's RomM-server ID (used to find our entry in `server_save.device_syncs`).
- **`local_hash`** — pre-computed RomM-parity `content_hash` of `local_file` (zip-aware: a plain MD5 for a single-file
  save, the per-entry combined hash for a zipped multi-file save — `SaveFileStore.content_hash`, never the whole-archive
  `checksum_md5`), or `None`. It **must** be the same scheme the server stamps so the parity-route byte-identity checks
  against `server.content_hash` (rows 6d / 11a) can match for zip saves too (#1457); the provenance route instead
  compares the stored `last_sync_server_hash` against `server.content_hash` and needs no such agreement (#1468).

### Pick rule and discriminators

Within `server_saves_in_slot`, the algorithm picks the **newest by `updated_at`** as the canonical save and decides
against that one. Other saves in the slot are ignored — no foreign-save surfacing, no per-save dismiss state.

Three discriminators drive the branch:

1. **Our device's entry on the picked save**: `server.device_syncs[me]` may be missing (we never touched this save),
   `is_current=true` (server claims our last write/read is current), or `is_current=false` (someone else has moved this
   save forward since we last touched it).
2. **Hash divergence vs. baseline**: `local_hash != files_state["last_sync_hash"]` means the local file has been edited
   since the last successful sync. Without a baseline (`last_sync_hash` is missing) we cannot claim divergence.
3. **Size plausibility (upload guard only)**: in the branch that uploads a diverged edit (our device `is_current=true` +
   local diverged, row 9), `local_file.size` is checked against the recorded `last_sync_local_size` baseline via
   `domain/save_size.is_implausibly_shrunken`. A 0-byte or implausibly-shrunk local is a crash artifact, not an edit,
   and diverts that branch to `Conflict` (row 9b) so a corrupt-looking local is never pushed as the new newest save
   (#1062).

`is_current` is **computed server-side**, not stored — see [RomM Save Sync API Behaviour](#romm-save-sync-api-behaviour)
below.

### Outcomes

| Variant                       | Service behaviour                                                                                                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Skip(reason)`                | No I/O. Optional `adopt_baseline=True` flag: dispatcher writes `last_sync_hash := local_hash` (state mutation only, no network).                                    |
| `Upload(target_save_id=None)` | POST a new save to the slot (`overwrite=false`, 409-backstopped). Server assigns an ID; we record it in state.                                                      |
| `Upload(target_save_id=int)`  | POST a new version too (`overwrite=false`, 409-backstopped). The id marks the save we supersede, for the status echo — it no longer drives an in-place PUT (#1276). |
| `Download(server_save)`       | GET save content, overwrite local file, update sync state.                                                                                                          |
| `Conflict(server_save)`       | Surface a `SyncConflict` to the frontend. The user resolves via `resolve_sync_conflict(rom_id, filename, server_save_id, action)`.                                  |

`Skip(adopt_baseline=True)` is recorded both from the mutating sync path (`SyncEngine.do_sync_rom_saves`) and the
read-only status path (`StatusService._get_save_status_io`). The alternative — only writing the baseline from the
mutating path — would leave state incomplete forever for users who only ever open the game-detail panel.

### Implementation

The algorithm is `compute_sync_action` in `py_modules/domain/sync_action.py`. The `SaveService` aggregate
(`py_modules/services/saves/`) calls it from two sub-services:

- `SyncEngine.do_sync_rom_saves` (`services/saves/sync_engine/`) iterates local files and server-only-in-slot groups,
  dispatching each action via the matrix executor's `_dispatch_sync_action` (POST/GET + state update; the in-place PUT
  is retired on this path, #1276).
- `StatusService._get_save_status_io` (`services/saves/status/`) runs the same decisions read-only and folds them into
  the `SaveStatus.files[*].status` strings the frontend renders. The only allowed mutation is recording an adopted
  baseline hash — pure state hygiene with no network traffic.

Server-only saves (no matching local file) are grouped by their target local filename (`rom_name.<ext>`) before being
passed to `compute_sync_action`. The algorithm picks the newest in the group, so older stacked versions in the same slot
are not separately surfaced. The same grouping applies to local files: each local file is matrix-evaluated only against
the server saves sharing its canonical target, so a multi-file save set (e.g. `Game.srm` + `Game.rtc`) never
cross-contaminates extensions — `Game.srm` is never resolved against a newer `Game.rtc` server record.

## Decision Matrix

The matrix below enumerates every input combination `compute_sync_action` handles. Rows are derived from the algorithm
and exhaustively cover the cross-product of dimensions. Tests in `tests/domain/test_sync_action.py` map 1:1 to these
rows.

Dimensions:

- **Local file** — does a `.srm` exist on disk?
- **Server saves in slot** — none, or at least one (algorithm picks newest).
- **Our device entry on picked save** — _never touched_ (no `device_syncs` entry for our id), _current=true_, or
  _current=false_.
- **Local vs `last_sync_hash`** — _unchanged_, _changed_, or _no baseline_ (key missing in state).
- **Local mtime vs server `updated_at`** — only consulted in the `never touched` branch where the algorithm has no other
  ordering signal.
- **Content identity** — in the `never touched` branch, byte-identity between the local file and the server head is
  checked first (`_local_matches_server`: the stored `last_sync_server_hash` vs `server.content_hash` while local is
  unchanged, else parity `local_hash == server.content_hash`); a match short-circuits to row 6d before mtime/baseline
  are consulted (#1468).

| #   | local file | server in slot | our entry     | local vs baseline | mtime vs server      | decision                            | reason                                                                                                                                                                  |
| --- | ---------- | -------------- | ------------- | ----------------- | -------------------- | ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | no         | none           | n/a           | n/a               | n/a                  | `Skip(nothing_to_sync)`             | nothing local, nothing server                                                                                                                                           |
| 2   | yes        | none           | n/a           | n/a               | n/a                  | `Upload(POST)`                      | first push for this save (or recovery after server-side wipe)                                                                                                           |
| 3   | no         | ≥1             | never touched | n/a               | n/a                  | `Download(picked)`                  | no relation, pull newest                                                                                                                                                |
| 4   | no         | ≥1             | current=true  | n/a               | n/a                  | `Download(picked)`                  | recovery — server still tracks our last version, local is gone                                                                                                          |
| 5   | no         | ≥1             | current=false | n/a               | n/a                  | `Download(picked)`                  | server moved forward, nothing local to protect                                                                                                                          |
| 6a  | yes        | ≥1             | never touched | no baseline       | local mtime ≥ server | `Upload(POST)`                      | post our local as a new save in the slot — no overwrite risk                                                                                                            |
| 6b  | yes        | ≥1             | never touched | no baseline       | local mtime < server | `Download(picked)`                  | server is newer than our untracked local                                                                                                                                |
| 6c  | yes        | ≥1             | never touched | changed           | n/a                  | **`Conflict(picked)`**              | baseline held from a prior sync but the picked head is a save we never synced — both sides moved (#1059)                                                                |
| 6d  | yes        | ≥1             | never touched | any               | any                  | `Skip(synced, adopt_baseline=true)` | byte-identical to an existing server save (stored server hash — provenance, or `content_hash == local_hash` — parity); adopt it, never POST a duplicate (#1013 / #1468) |
| 7   | yes        | ≥1             | current=true  | unchanged         | n/a                  | `Skip(synced)`                      | steady state                                                                                                                                                            |
| 8   | yes        | ≥1             | current=true  | no baseline       | n/a                  | `Skip(synced, adopt_baseline=true)` | trust server's `is_current=true`, write `last_sync_hash := local_hash` so future drift can be detected                                                                  |
| 9   | yes        | ≥1             | current=true  | changed           | n/a                  | `Upload(supersede picked.id)`       | offline edit (plausible size) — POST our changes as a new version that supersedes the save the server still considers ours                                              |
| 9b  | yes        | ≥1             | current=true  | changed           | n/a                  | **`Conflict(picked)`**              | diverged local is 0-byte or shrunk past the baseline (crash / full disk) — refuse the in-place PUT, let the user decide (#1062)                                         |
| 10  | yes        | ≥1             | current=false | unchanged         | n/a                  | `Download(picked)`                  | another device synced; we did nothing — adopt their version                                                                                                             |
| 11a | yes        | ≥1             | current=false | no baseline       | n/a                  | `Download(picked)`                  | no baseline (so parity only, #1468), but `server.content_hash == local_hash` — byte-identical, adopt the server head safely                                             |
| 11b | yes        | ≥1             | current=false | no baseline       | n/a                  | **`Conflict(picked)`**              | no baseline and content differs from the server head — cannot prove which side is newer; refuse the silent overwrite (#1276)                                            |
| 12  | yes        | ≥1             | current=false | changed           | n/a                  | **`Conflict(picked)`**              | both sides changed independently — only true conflict                                                                                                                   |

Conflict happens in four rows — #12 (we already hold an entry on the picked save and our local diverged from the
baseline), #6c (we hold a baseline from a prior sync but no entry on the picked head), #11b (we hold an
`is_current=false` entry, have no baseline, and our local content differs from the server head), and #9b (we own the
picked save and our local diverged, but the local file is 0-byte / implausibly shrunk). #12, #6c, and #11b are all "both
sides hold content we never reconciled" situations; #9b is a different hazard — protecting the server's only good copy
from being overwritten in place by a corrupt-looking local file (#1062). Every other row resolves silently to a Skip,
Upload, or Download.

### Why row 6d adopts instead of posting

Row 6d fires when a local file exists, a server save also exists in the slot, our device has never touched the picked
save, **and** the picked save's RomM-provided `content_hash` equals the local content hash — the bytes on disk are
identical to bytes the server already holds. This is the copied-SD-card / restored-backup / fresh-reinstall case: the
device looks brand-new to the server (no `device_syncs` entry) but the content is the same save. POSTing here would
create a duplicate server save of identical bytes, inflating the slot and churning autocleanup. Instead we adopt the
existing save as the baseline (`Skip(adopt_baseline=true)`, writing `last_sync_hash := local_hash`), so future drift is
detected without ever duplicating. The check is pure — no download, no re-hash — because RomM stamps `content_hash` on
every save. **Known fallback gap:** older / migrated server saves may lack `content_hash`; when it is absent the dedup
check is skipped and the row 6a/6b mtime path applies (which can still POST a byte-identical duplicate). No slow-path
content fetch is attempted (#1013).

### Why row 6a posts instead of overwriting

Row 6a fires when a local file exists, a server save also exists in the slot, our device has never touched the picked
server save, the content hashes do **not** match (or `content_hash` is absent — see row 6d), and our local mtime is
at-or-after the server's `updated_at`. There is no baseline (`last_sync_hash`), so we cannot prove drift either way; we
also have no claim on the picked save (no `device_syncs` entry for our id). POSTing a brand-new save preserves both
files: the original picked save stays intact, and our local content lands as a separate entry that becomes the new
newest. Subsequent syncs pick our save naturally.

### Why row 6c conflicts instead of downloading

Row 6c is the never-touched sibling of row 12. We hold a baseline (`last_sync_hash`) from a prior sync, but the picked
head is a save we have no `device_syncs` entry for — another timeline became newest while our local diverged from the
baseline (`local_hash != last_sync_hash`). That is the same "both sides moved independently" situation as row 12, so it
takes the same exit: a `Conflict` the user resolves, never a silent `Download` that would discard the diverged local
progress (whose only surviving copy would be the `.romm-backup`). When there is no baseline, or local still matches it,
we cannot claim divergence — rows 6a/6b apply and the mtime heuristic breaks the tie.

### Why row 9b conflicts instead of uploading

Row 9 is the steady offline-edit path: we own the picked save (`is_current=true`), our local diverged from the baseline,
so we POST the local content as a new version that supersedes the tracked save. Row 9b is the same branch with one extra
guard. A crashed emulator or a full disk can leave a **0-byte or truncated** save on disk — still a valid regular file,
with a valid-but-wrong content hash, so it reads as a "diverged" edit and would take the row 9 upload. Pushing that
corrupt file would make it the newest save in the slot, so newest-wins would then propagate the garbage to every other
device — and once `autocleanup_limit` prunes the older versions, the good copy could age out of recovery. This is the
upload mirror of the [#965](https://github.com/danielcopper/decky-romm-sync/issues/965) backup-or-confirm invariant: the
download-overwrite path already quarantines the local file into `.romm-backup` first, but a blind upload of a
corrupt-looking local had no equivalent guard.

The plausibility check is pure (`domain/save_size.is_implausibly_shrunken`, fed `local_file.size` and the recorded
`last_sync_local_size` baseline): a new size of **0** fires unconditionally, and a non-empty new size below **50%** of
the recorded baseline fires as a shrink. The threshold is a hard-coded conservative default — not a setting. When the
guard fires, the kernel returns `Conflict(picked)` instead of `Upload`, routing through the existing `SyncConflictModal`
so the user decides: **Use Server** downloads the good server copy (quarantining the bad local first), **Keep Local**
re-uploads the corrupt file only after an explicit choice. A plausible-size divergent edit (or a save that grew) is
unaffected and still uploads (row 9).

### Why row 11 splits into download (11a) vs conflict (11b)

Row 11 looks superficially symmetrical to row 6a — local file exists, mtime is whatever, no baseline. The difference is
that our device **does** have an entry on the picked save (we touched it before) and the entry says `is_current=false`:
some other device has moved that save forward since our last interaction, so its content is foreign to us. Without a
baseline we cannot prove our local has edits that postdate the foreign write, and mtime is unreliable (filesystem
touches, migrations, clock skew).

The content hash breaks the tie:

- **11a — `server.content_hash == local_hash`.** The bytes on disk are already identical to the foreign head, so there
  is nothing to lose: adopt the server save (`Download`). This is the copied-card / restored-backup case landing on the
  foreign timeline.
- **11b — the content differs.** We genuinely hold different local bytes than the server head, with no baseline to say
  which is newer. The earlier design silently `Download`ed here, quarantining the local bytes into `.romm-backup` on the
  bet that another device's work mattered more — a silent overwrite of possibly-newer local progress. Under
  [#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276) this is a **`Conflict`** instead: the user
  decides via Keep Local / Use Server, the same exit rows 6c and 12 take. When the server save carries no `content_hash`
  (older / migrated saves) the equality fails closed to this conflict — the safe default. mtime is never trusted to
  break this tie.

### Why is there no foreign-save modal anymore

Earlier versions surfaced every server save in the slot the user had not authored as a "newer-in-slot" prompt. The
pragmatic newest-wins model used by the official RomM clients (Argosy, Grout) treats the slot as a single timeline:
whichever save has the highest `updated_at` wins, regardless of which device PUT it. We adopted that model in v0.16
because it eliminates ~1500 lines of foreign-tracking code and aligns with the wider RomM ecosystem. Cross-device
uploads are silently adopted unless local edits diverge from baseline (rows 12 / 11b). This is documented behaviour, not
a regression.

### Upload-time conflicts (the 409 backstop)

The client kernel is not the only conflict detector — RomM's `POST /api/saves` self-guards. Every upload the automatic
sync path dispatches is a POST with `overwrite=false`, and RomM rejects it with **HTTP 409** when the device is not
current on the slot's newest save (see [The `add_save` POST 409-gate](#the-add_save-post-409-gate) below for the exact
predicate). The adapter maps that 409 to `RommConflictError`, which is non-retryable and propagates on the first
attempt.

On a 409 the executor re-fetches the slot, picks the newest save in the canonical group, and resolves through the pure
`resolve_upload_conflict(local_hash, last_sync_hash, server_content_hash)`:

| Condition                           | Result       | Why                                                                                 |
| ----------------------------------- | ------------ | ----------------------------------------------------------------------------------- |
| `local_hash == last_sync_hash`      | `"download"` | local is unchanged since our last sync — the server moved on, adopt it              |
| `local_hash == server_content_hash` | `"download"` | already byte-identical to the server head — adopt it, nothing to upload             |
| otherwise (incl. any `None` input)  | `"conflict"` | genuinely divergent — surface the same `SyncConflictModal` a matrix `Conflict` uses |

A `"download"` result downgrades the upload to a `do_download_save` (the server save was newer and our local had no
un-synced edits); a `"conflict"` result appends a `SyncConflict` and returns without writing, so the user resolves it
via Keep Local / Use Server exactly as a matrix-row conflict. This is the upload mirror of the matrix's download-side
safety: an automatic upload never blindly overwrites a save the device isn't current on.

**`overwrite=true` is reserved for the explicit `keep_local` resolution.** The only caller that sets it is
`_resolve_conflict_keep_local` — when the user has chosen to overwrite the server head, the re-POST carries
`overwrite=true` to bypass the 409 gate deliberately
([#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276) /
[ADR-0017](../adr/0017-client-baseline-detection-authoritative-negotiate-is-transport.md)).

## Slot Setup Wizard

Before save sync can operate for a game, the user must choose which slot to track. This is managed by the
`slot_confirmed` flag in per-game state.

### Scenarios on first use

| Scenario | Local     | Server                    | Behavior                                       |
| -------- | --------- | ------------------------- | ---------------------------------------------- |
| A        | No saves  | Has saves                 | Wizard: choose which server slot to track      |
| B        | Has saves | No saves                  | Auto-configure with default slot (no prompt)   |
| C        | Has saves | Has saves (other slots)   | Wizard: upload to default or track server slot |
| D        | --        | --                        | Manual slot switch in game detail              |
| E        | Has saves | Has saves in default slot | Wizard: track default or use different slot    |

### Where the check happens

- **Game detail page (SAVES tab):** shows wizard instead of save list when `slot_confirmed=false`
- **Play button:** checks before launch. If not configured and server has saves, redirects to SAVES tab. If no server
  saves, auto-configures with default.

## Save File Discovery

Save files are located using a predictable path pattern based on the system slug and ROM filename.

### Save base path

The save base directory is read at runtime from RetroDECK's configuration file:

```text
~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json -> paths.saves_path
```

This path varies depending on where RetroDECK was installed:

- **Internal SSD**: `/home/deck/retrodeck/saves/`
- **SD card**: `/run/media/deck/Emulation/retrodeck/saves/`

The backend reads `retrodeck.json` → `paths.saves_path` as the source of truth
(`py_modules/adapters/retrodeck_paths.py`). When that file is unreadable — e.g. a fresh install with no RetroDECK
configured yet — it falls back to the hardcoded RetroDECK default `~/retrodeck/saves`.

The plugin deliberately does **not** read `savefile_directory` from `retroarch.cfg`; it takes the saves root from
`retrodeck.json` → `paths.saves_path`. RetroDECK re-pins `savefile_directory = saves_path` only at **first-run
install**, **config reset** (explicit / factory / component update / multi-user switch), and **data-move** (`postmove`)
— **not** on every game launch or routine boot. Between those events `retroarch.cfg` is user-owned and edits persist, so
the plugin reads the live cfg for save **sorting** to stay correct. The one key it does not yet read —
`savefiles_in_content_dir` — is therefore a **persistent** blind spot until the user toggles it back or a reset/install
re-copies the default cfg ([#239](https://github.com/danielcopper/decky-romm-sync/issues/239)).

_Verified against RetroDECK source on 2026-06-09: `RetroDECK/components` `retroarch/component_prepare.sh` sets the key
only in its `reset` and `postmove` branches, and every `prepare_component` call in `RetroDECK/RetroDECK` uses action
`reset` / `postmove` / `factory-reset` — none from a launch (`run_game`) path._

### RetroArch .srm pattern

All RetroArch cores save in a consistent location:

```text
<saves_path>/{system}/{rom_name}.srm
```

Where:

- `<saves_path>` is the base path from `retrodeck.json` → `paths.saves_path`
- `{system}` is the RetroDECK ROM directory name (e.g. `gba`, `snes`, `n64`, `psx`) — this matches the ROM folder under
  `roms/`
- `{rom_name}` is the ROM filename without extension

**Sort by content directory**: RetroDECK's default RetroArch config sets `sort_savefiles_by_content_enable = true`. This
means save subdirectories match the ROM's parent folder name (the platform slug like `gba`), **not** the RetroArch core
name (like `mGBA`). The separate `sort_savefiles_enable` setting (sort by core name) is `false` by default.

**Sort by core name (optional)**: When a user enables `sort_savefiles_enable`, RetroArch organizes saves by the core's
canonical name instead — e.g. `<saves_path>/Snes9x/game.srm` rather than `<saves_path>/snes/game.srm`. The canonical
core name comes from the `corename` field of RetroArch's `.info` file for the active core, which is **not** the same as
the ES-DE display label for that core (e.g. ES-DE labels the core `Snes9x - Current` while RetroArch calls it `Snes9x`).
The plugin resolves this by asking two different parsers — ES-DE for "which core is active", then the RetroArch `.info`
parser for "what does RetroArch call that core". The rationale and architecture are documented on the
[Config Source Parsers](config-source-parsers.md) page.

The backend resolves save paths by looking up the ROM's system slug in the platform config and constructing the expected
`.srm` path. The file is checked for existence and its hash/mtime are read for comparison.

### Unsupported: `savefiles_in_content_dir` (Write Saves to Content Directory)

RetroArch has a third save-related layout setting that **the plugin does not support**:

- `savefiles_in_content_dir` — RetroArch UI label: **"Write Saves to Content Directory"**

When this setting is **enabled** (RetroDECK default is `false`), RetroArch writes save files into the **same directory
as the ROM file** (e.g. `roms/gba/Game/Game.srm`) instead of the configured `savefile_directory`. The two
`sort_savefiles_*` settings discussed above become irrelevant in that case because saves no longer live in the savefile
directory at all.

**The plugin detects this configuration and disables save sync for it — it does not silently miss saves
([#239](https://github.com/danielcopper/decky-romm-sync/issues/239)).** `adapters/retroarch_config.py` reads all three
layout keys and models them as a `SaveLayout` value object (`domain/save_layout.py`): `ContentDir` when
`savefiles_in_content_dir=true`, otherwise `InSaveDir(sort_by_content, sort_by_core)`. When the layout is `ContentDir`,
the four save-sync entry points (`pre_launch_sync`, `post_exit_sync`, `sync_rom_saves`, `sync_all_saves`) return a
benign skip (`{success: false, reason: "savefiles_in_content_dir", …}` — the game still launches, no error), and
`get_save_status` carries an additive `savefiles_in_content_dir: true` flag so the game-detail play section shows a
banner asking the user to turn the setting back off. Actually _syncing_ ROM-adjacent saves stays unsupported (the
deferred multi-emulator work).

This blind spot is **persistent**: RetroDECK only restores the `false` default on a full config reset or first-run
install — never on a normal launch (verified 2026-06-09) — so the plugin reads the layout live on every sync rather than
assume RetroDECK keeps it off.

**Why this is easy to confuse**: the RetroArch UI labels are deliberately similar. "Write Saves to **Content
Directory**" controls the **destination** (next to the ROM vs the saves directory), while "Sort Saves **Into Folders by
Content Directory**" controls the **layout within** the saves directory. They sound nearly identical but mean very
different things.

| RetroArch UI label                           | cfg key                            | What it controls                                                                                                           |
| -------------------------------------------- | ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Write Saves to Content Directory             | `savefiles_in_content_dir`         | **Destination** — next to ROM (true) vs `savefile_directory` (false). Plugin **detects `true`, warns, skips sync** (#239). |
| Sort Saves Into Folders by Content Directory | `sort_savefiles_by_content_enable` | **Layout inside `savefile_directory`** — group by ROM parent folder name. Plugin handles both values.                      |
| Sort Saves Into Folders by Core Name         | `sort_savefiles_enable`            | **Layout inside `savefile_directory`** — further group by RetroArch core name. Plugin handles both values.                 |

**Status**: detect-and-warn is **implemented** ([#239](https://github.com/danielcopper/decky-romm-sync/issues/239)) —
the layout is read into `SaveLayout`, `ContentDir` hard-gates the four sync entry points, and the play-section banner
surfaces it. Full support — resolving save paths relative to the ROM's actual on-disk location — remains deferred to the
multi-emulator save work ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129) /
[#255](https://github.com/danielcopper/decky-romm-sync/issues/255)).

## Save-Sort Migration: Automatic Detection and Conflict Resolution

### Why detection needs to happen mid-session

RetroArch save sorting is controlled by two keys in `retroarch.cfg`:

- `sort_savefiles_by_content_enable` — group saves under the ROM's platform folder (e.g. `gba/`)
- `sort_savefiles_enable` — group saves under the core's canonical name folder (e.g. `mGBA/`)

When a user changes either setting — most commonly via the RetroArch Quick Menu **while a game is running** — RetroArch
does not migrate existing `.srm` files. It silently begins writing future saves to the new layout. The result is a split
state: older saves sit at the old path, newer in-session saves go to the new path, with no signal from RetroArch that
anything changed.

The plugin must detect this layout change and offer a one-click migration to consolidate files under the new path.
Because the most common trigger is mid-game configuration (Quick Menu → Settings → Directory), detection cannot be
deferred to plugin startup alone. It must also run at the points that bracket gameplay.

### Detection trigger points

All five trigger points call the `refresh_migration_state` callable and share the same idempotent backend methods.
Running on every trigger is cheap: `detect_retrodeck_path_change()` and `detect_save_sort_change()` both have
early-return guards that exit immediately when no config change has occurred since the last call.

| When             | Where (code location)                                              | Why                                                                                                  |
| ---------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Plugin load      | `main.py` Phase 6 in `_main()`                                     | Catches changes that occurred between plugin sessions                                                |
| QAM open         | `MainPage.tsx` mount `useEffect`                                   | User navigating via QAM sees current state when Settings is one tap away                             |
| Game-detail open | `RomMGameInfoPanel.tsx` `useEffect([appId])`                       | Per-game navigation refreshes state when the user browses without launching                          |
| Pre-game-launch  | `launchInterceptor.ts`                                             | Catches setting changes made by external tooling between sessions                                    |
| Post-game-exit   | `SessionLifecycleService.finalize` (backend, after post-exit sync) | **Primary trigger for the main real-world scenario** — user changed settings via Quick Menu mid-game |

### Post-game ordering and the detect-first invariant (#238)

In `SessionLifecycleService.finalize` (backend), the post-exit save sync runs first, then the migration refresh runs
unconditionally; `sessionManager.ts` `handleGameStop` now makes a single `finalizeGameSession` call and feeds the
returned payloads into the migration stores. However, the ordering is **not load-bearing** — save-sync is
order-independent with respect to detect triggers because of three structural guards introduced in #238:

**The race problem (pre-#238):** When the user changes RetroArch sort settings mid-game, `refreshMigrationState` from
`RomMGameInfoPanel` remount could update state to the new layout before `postExitSync` read it. Save-sync would then
look in the wrong directory, download stale server content, and the newest-wins resolver would pick the fresh-but-stale
download over the real user progress.

**Three structural guards:**

1. **Rule 1 — Read previous layout during pending migration.** `RomInfoService.get_rom_save_info`
   (`services/saves/rom_info.py`) reads `save_sort_settings_previous` (the layout RetroArch was writing to during the
   session) in preference to `save_sort_settings` (the new layout). This ensures save-sync always looks where RetroArch
   actually wrote.

2. **Rule 2 — Upload-only mode during pending migration.** `SyncEngine.do_sync_rom_saves` skips `server_only` matches
   (no downloads) when a save-sort migration is pending. This prevents stale server content from being written to disk
   with `mtime=now`, which the mtime-naive migration resolver could then mispick.

3. **Detect-first invariant.** `pre_launch_sync`, `post_exit_sync`, `sync_rom_saves`, and `sync_all_saves` all call
   `detect_save_sort_change` (via an injected callback from `MigrationService`) before reading state. This closes the
   race where `post_exit_sync` reaches the backend before any frontend detect trigger fires — ensuring
   `save_sort_settings_previous` is always set before save-sync reads it.

Combined, these three guards close all four race sub-scenarios (mid-session change with detect winning or post_exit
winning the race, and NEW-from-start with detect winning or post_exit winning).

Migration refresh still runs unconditionally regardless of connectivity because `refresh_migration_state` only reads
config files and local state — it does not touch user save files. The actual migration runs only when the user
explicitly clicks the migrate button in Settings.

### Newest-wins conflict resolution

Implemented in `_resolve_save_sort_conflict` in `py_modules/services/migration.py`.

**The scenario**: the user enables `sort_savefiles_enable` mid-game and saves in-game. RetroArch writes fresh progress
to the new layout — e.g. `saves/gba/mGBA/Mario Golf.srm`. The old file at the original layout — e.g.
`saves/gba/Mario Golf.srm` — still exists with pre-change content. When migration runs, both files are present and the
migration logic treats this as a conflict.

**Resolution rule**: the file with the newer `mtime` wins.

| Case                        | Condition                                                                                  | Action                                                                                     |
| --------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| Destination newer (typical) | In-game save wrote to the new layout during the session                                    | Remove the orphan at the old path via `os.remove`, keep the destination, count as migrated |
| Source newer (rare)         | Source `mtime` exceeds destination; possible if the user reverted settings without playing | Atomically overwrite the destination via `os.replace`, count as migrated                   |
| Tie (equal mtime)           | `mtime` values identical at filesystem granularity                                         | Bias toward destination (no-op keep)                                                       |

On any `OSError` during `mtime` reads or file operations, the error is appended to the errors list and processing
continues with the next item. The migration never leaves state partially inconsistent — each file is either fully
resolved or skipped with an error recorded.

### Why newest-wins is safe

- If the user played game G during the setting change, the in-game save at the new path contains all progress up to that
  save point. The old file at the old path contains only pre-change progress — a strict subset. Deleting the old file
  loses nothing.
- If the user did **not** play game G during the setting change, only the old file exists (no destination file, no
  conflict) and migration is a simple move to the new path.
- Save-sync has already uploaded the new-path version to RomM before migration runs (see post-game ordering above). Even
  a catastrophic local migration failure leaves the latest version on the server.

**Mtime-naive limitation:** The resolver compares pure `os.path.getmtime` timestamps. A freshly-downloaded file has
`mtime=now` regardless of how old its content actually is. This is structurally prevented by #238 Rule 2 (upload-only
mode during pending migration prevents downloads that would create stale files with misleading mtimes). If Rule 2 is
ever removed, the resolver would need to be made hash-aware.

### Relationship to `retrodeck_path_migration`

The RetroDECK **path** migration — `_migrate_retrodeck_files_io` in `migration.py`, triggered when the RetroDECK home
directory moves between the internal SSD and an SD card — uses a different conflict-resolution approach: a user-driven
bulk strategy modal (overwrite / skip / cancel). That is intentional. ROMs and BIOS files are not progress files, and
`mtime`-based resolution is not semantically meaningful for them. See
[RetroDECK Path Migration](../user-guide/retrodeck-path-migration.md) for the user-facing side.

### Supported systems

All paths below are relative to `<saves_path>` from `retrodeck.json`.

| System                    | Save Path Example             | Extension |
| ------------------------- | ----------------------------- | --------- |
| NES                       | `saves/nes/game.srm`          | `.srm`    |
| SNES                      | `saves/snes/game.srm`         | `.srm`    |
| Game Boy                  | `saves/gb/game.srm`           | `.srm`    |
| Game Boy Color            | `saves/gbc/game.srm`          | `.srm`    |
| Game Boy Advance          | `saves/gba/game.srm`          | `.srm`    |
| Genesis / Mega Drive      | `saves/genesis/game.srm`      | `.srm`    |
| Master System             | `saves/mastersystem/game.srm` | `.srm`    |
| Nintendo 64               | `saves/n64/game.srm`          | `.srm`    |
| PlayStation (RetroArch)   | `saves/psx/game.srm`          | `.srm`    |
| Saturn                    | `saves/saturn/game.srm`       | `.srm`    |
| Dreamcast                 | `saves/dreamcast/game.srm`    | `.srm`    |
| PC Engine / TurboGrafx-16 | `saves/pcengine/game.srm`     | `.srm`    |
| Neo Geo Pocket            | `saves/ngp/game.srm`          | `.srm`    |
| WonderSwan                | `saves/wonderswan/game.srm`   | `.srm`    |
| Atari Lynx                | `saves/atarilynx/game.srm`    | `.srm`    |

## Slot Deletion

Users can delete save slots from the game detail SAVES tab. Deletion removes the slot from local state and bulk-deletes
all server saves in the slot.

### How it works

1. **Get delete info**: `get_slot_delete_info(rom_id, slot)` returns metadata for the confirmation modal — server save
   count, tracked file count, slot source (server/local), and whether the slot is active.
2. **Confirmation modal**: Always shown (both local-only and server-backed slots). Shows exact save count and whether
   saves will be deleted from the server.
3. **Perform deletion**: `delete_slot(rom_id, slot)` bulk-deletes server saves via `POST /api/saves/delete`, removes the
   slot from the `slots` dict, and cleans up `files` entries whose `tracked_save_id` matches a deleted save.

### Safety invariants

- **Active slot cannot be deleted.** The user must switch to a different slot first. This implicitly prevents deleting
  the last remaining slot — the last slot is always active (there's nothing to switch to), so it can never be deleted.
- **Server errors leave state intact.** If `delete_server_saves` fails (network error), the slot is NOT removed from
  local state. The user can retry.
- **Local-only slots** (`source: "local"`) skip server calls entirely — always deletable.

### Frontend

The delete button appears in inactive slot bodies alongside "Activate Slot". It is hidden on the active slot. Gamepad
navigation between the buttons uses `Focusable` with `flow-children="right"` for proper DPad left/right traversal.

## Server Capabilities

The capabilities system (`get_server_capabilities` callable) has been removed. Since the plugin now requires RomM >=
4.9.0, all features (device sync, version history, slot deletion, device management) are unconditionally available. The
frontend no longer fetches or checks capability flags.

## Conflict Resolution

A `Conflict` outcome from `compute_sync_action` (matrix rows 12, 6c, 11b, and 9b) is the only surface that shows a
modal. The common case fires when the local file has diverged from the recorded baseline
(`local_hash != last_sync_hash`) while the server moved to content we never synced — either on a save we already have an
entry for (`device_syncs[me].is_current=false`, row 12) or on a new head we have no entry for (row 6c). Row 11b is the
no-baseline sibling: we hold an `is_current=false` entry, have no baseline, and our local content differs from the
server head — the same both-sides-unreconciled hazard (#1276). In all three the two sides have unsynced changes that
cannot be silently merged. Row 9b is the corrupt-local guard: we own the picked save and our local diverged, but the
local file is 0-byte or implausibly shrunk, so the in-place PUT is refused and the user resolves it instead of the only
good server copy being overwritten (#1062).

### The modal

`SyncConflictModal` (`src/components/SyncConflictModal.tsx`) shows the local-save row and the picked server-save row
side by side, each with size and timestamp. Three actions:

- **Keep Local** → `resolveSyncConflict(rom_id, filename, "keep_local")` → backend POSTs local content as a new server
  version with `overwrite=true` (the old server save is retained, not overwritten in place).
- **Use Server** → `resolveSyncConflict(rom_id, filename, "use_server")` → backend downloads the picked server save and
  overwrites local.
- **Cancel** → pure UI close, no callable, no state mutation. The conflict re-fires on the next sync as long as the
  underlying state still produces matrix row 12, 6c, 11b, or 9b.

On a successful resolution the modal closes and surfaces a branch-specific confirmation toast — **Keep Local** confirms
the local save was uploaded to the server, **Use Server** confirms the server save is now in use and the prior local
save was backed up to `.romm-backup`. Failure and stale branches stay inline in the modal instead (no toast).

The modal is shown by `CustomPlayButton` during pre-launch sync, and by `VersionHistoryPanel.handleRestore` (in
`SavesTab`) when a version-restore pre-flight returns `conflict_blocked`. Both call `showSyncConflictModal(conflict)`
which returns a Promise resolving to `"keep_local" | "use_server" | "cancel"`. After post-exit sync, `sessionManager`
only fires a toast — the conflict re-surfaces in the modal at the next pre-launch.

### resolve_sync_conflict callable

`SaveService.resolve_sync_conflict(rom_id, filename, server_save_id, action)` — the async callable wired in `main.py`.
The façade delegates to `SyncEngine.resolve_sync_conflict`, whose rollback sub-module
(`services/saves/sync_engine/rollback.py`) runs the resolution:

1. Acquires the per-rom asyncio.Lock so no other sync operation for this rom can race.
2. Fetches a fresh server-saves list and re-picks the newest in the active slot.
3. **Round-trips `server_save_id`**: the caller passes the id the user was shown in the modal. If the freshly-picked
   head's id doesn't match, a third device has uploaded a newer save into the slot between the modal opening and the
   click. The backend returns `{success: False, reason: "stale_conflict", message: ...}` instead of dispatching —
   silently PUTting local content over the third device's work would be a write-loss. The frontend surfaces an error and
   the user cancels + retries; the next sync re-evaluates the matrix with the fresh head.
4. Dispatches:
   - `keep_local` → `_resolve_conflict_keep_local` reads the server save's content hash. If it matches local (rare, but
     possible — both devices ended up at the same content via different paths), the server's id is adopted into state
     without re-uploading. Otherwise the local file is POSTed as a new version with `overwrite=true` — the user's
     deliberate overwrite bypasses the 409 gate (#1276). The POST's own `device_save_sync` upsert leaves us
     `is_current=true`, so the redundant `confirm_download` ack is skipped (#1458).
   - `use_server` → `_resolve_conflict_use_server` downloads the picked save and writes it to the local path.

The modal only accepts `keep_local` or `use_server`; `cancel` never reaches the backend. A wrong action string is
rejected before the lock is acquired.

### Why no defer state

Earlier drafts persisted a `deferred` field in per-file state to suppress the modal on subsequent syncs until the server
state changed. This was removed before merge: the conflict is already surface-on-demand (only shown during a
user-initiated launch), and re-firing on the next launch is the desired behaviour — the user has just reopened the game
and is in a position to decide. Self-healing is automatic: if another device pushes in the meantime, the picked server
save changes and the matrix may produce Skip or Download instead of Conflict, dissolving the conflict without user
input.

### Per-rom asyncio.Lock

`SyncEngine._rom_sync_locks: dict[int, asyncio.Lock]` (`services/saves/sync_engine/engine.py`) serializes
`pre_launch_sync`, `post_exit_sync`, `sync_rom_saves`, `sync_all_saves`, and `resolve_sync_conflict` for the same
`rom_id`. `StatusService.get_save_status` also takes the lock — not for the read, but for its one write: the executor
body adopts a baseline hash (`Skip(adopt_baseline=True)`) and persists it through a `rom_save_states` read-modify-write,
which would otherwise race a concurrent sync and clobber that sync's update. The four **slot mutations** —
`SlotSwitcher.switch_slot` / `set_active_slot`, `SetupWizard.confirm_slot_choice`, and `SlotDeleter.delete_slot` — take
the lock too: each loads the `RomSaveState` aggregate, mutates it (active-slot flip, slot-confirm, slot/file tracking
teardown, switch downloads/deletes), and persists, so without the lock a slot op racing an in-flight sync on the same
ROM loses updates or PUTs the wrong slot's content into the tracked server save (#1057). The lock-free server-saves
network fetch stays outside the lock; only the local RMW is the critical section. Different rom_ids have independent
locks, so cross-game concurrency (e.g. Sync All Saves running concurrently with a resolve on one specific rom) is
unaffected. The lock is created lazily on first access (`SyncEngine.rom_lock(rom_id)`).

The lock is **not reentrant** (plain `asyncio.Lock`), so a critical section must never call a peer that re-acquires the
same lock. `switch_slot` is the live instance: its tail `get_save_status` re-takes `rom_lock(rom_id)`, so the lock is
released at the end of the read-mutate-write block and the status read runs **outside** it — nesting them would
self-deadlock. The peer calls a slot mutation makes while holding the lock (`content_dir_blocked`,
`_migrate_slot_saves`, `_delete_server_slot_saves`, the matrix download/upload workers) are all lock-free by design, so
holding the lock across their server/file I/O is safe and is the intended serialization point.

The realistic race the lock prevents: user clicks Keep Local → executor runs the POST (`overwrite=true`) + state
mutation → in parallel, `post_exit_sync` for a game that just stopped runs and mutates the same per-file state →
last-writer-wins on the `rom_save_states` aggregate, dropping one set of fields. The same lost-update window applies to
`get_save_status`'s baseline-adopt write versus a concurrent pre-launch / post-exit / manual sync. The lock makes each
read-modify-write-and-persist sequence atomic relative to the others.

## Local Save File Naming

Every download path — pre-launch / post-exit / manual sync, conflict-resolve "Use Server", rollback / version switch,
slot switch — writes content to a path of the form:

```text
<saves_dir>/<rom_basename>.<server_save.file_extension>
```

`<rom_basename>` is the ROM file's name without extension (e.g. `Mario Golf - Advance Tour (USA)` from
`Mario Golf - Advance Tour (USA).gba`); `<server_save.file_extension>` is the `file_extension` field on the chosen RomM
save (e.g. `srm`).

This is the **only** path used for local writes. The server's stored `file_name` (which may carry a timestamp tag like
`[2026-03-24_15-18-50]` or come from a different client with an unrelated naming convention) and the server's
`file_name_no_tags` are **not** consulted. RetroArch identifies SRAM purely by `<rom_basename>.<ext>` filename match —
content is opaque bytes — so writing to anything else would leave the save invisible to the emulator.

The shared helper is `_local_save_target(server_save, rom_name)` in `py_modules/services/saves/_helpers.py` (wrapping
`domain.save_path.compute_local_save_target`). It requires a non-None `rom_name`; there is no fallback to server-derived
names. If a ROM is not installed (`RomInfoService.get_rom_save_info` returns `None`) the saves tab shows no entry for it
and sync is a no-op for that ROM — by design, rather than guessing a path that may or may not match what RetroArch uses.

This matches the convention used by the official RomM clients [Argosy](https://github.com/rommapp/argosy-launcher) and
[Grout](https://github.com/rommapp/grout).

The version-history UI (`list_file_versions`) reflects the same principle: it returns every save in the active slot
except the currently-tracked one, with no filename filter. A user can switch to any save in the slot — even ones
uploaded by another client with a different name — and the destructive switch lands the content at the canonical local
path.

## Version Switch Flow (rollback)

Users can switch the active save to a chosen older version via the Previous Versions dropdown in the SAVES tab. The flow
is more involved than a simple download because it must:

1. Capture any local changes server-side first (otherwise the destructive overwrite would lose them).
2. Make the chosen save authoritative cross-device — other devices that already have the latest save tracked must end up
   downloading the chosen version on their next sync.

### Multi-file saves: version history suppressed (interim #908 guard)

Some systems store one game state across **several files with distinct extensions** — e.g. a Sega Saturn cartridge save
is `<rom>.bkr` + `<rom>.bcr` + `<rom>.smpc` (three files = one state). RomM stores each filename as an **independent
save record with its own version stack**, so the slot's "current save" is really an N-file _set_, not a single file with
a version history. Per-file rollback would revert one component and leave the siblings on their current version — an
incoherent save.

Until grouped save-states with atomic set rollback land
([#908](https://github.com/danielcopper/decky-romm-sync/issues/908)), the plugin **detects multi-file slots and
suppresses version history + rollback** for them:

- `get_save_status` carries `multi_file: bool`, `component_files: list[str]` (the N filenames, sorted), and
  `rollback_supported: bool`. Detection counts the distinct canonical target filenames the active slot resolves to
  (across the matrix outcomes); more than one ⇒ multi-file.
- The SAVES tab replaces the Previous-Versions dropdown for a multi-file slot with a read-only **"Files in this save
  (N)"** component list plus a short note that per-version rollback isn't available yet.
- `list_file_versions` short-circuits to `{"status": "multi_file_unsupported", "versions": []}` and
  `rollback_to_version` refuses with `{"status": "unsupported"}` before any preflight or destructive I/O. Both backstops
  detect multi-file from the **local** save files on disk (a rollback target is always installed), so they add no extra
  network round-trip.

Single-file slots — including a single file with genuine prior versions — are unaffected and keep the full
version-history + rollback flow described below.

### Why a switch cannot be a download-only

A pure download to local would only update _our_ device. On the next sync from any other device, RomM's
newest-by-`updated_at` rule would still pick the original (newer) save and propagate it back to us. The switch would
silently undo itself.

To make the switch authoritative cross-device, the chosen older save's `updated_at` must become NOW so it beats every
other save in the slot.

### Matrix pre-flight

Before the destructive switch starts, `rollback_to_version` runs a full `compute_sync_action` pre-flight on the
currently-tracked save (via `do_sync_rom_saves`):

| Pre-flight outcome                           | What happens                                                                                                                                                                                                |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Skip(synced)` / `Skip(adopt_baseline=True)` | No I/O. Switch proceeds.                                                                                                                                                                                    |
| `Upload`                                     | Local changes are silently pushed to the server first. Switch proceeds.                                                                                                                                     |
| `Download(server)`                           | The newer server save is silently adopted. Switch then proceeds (the user's chosen target is still in the slot).                                                                                            |
| `Conflict(...)`                              | Switch aborts with `{"status": "conflict_blocked", "conflicts": [...]}`. The frontend opens the standard `SyncConflictModal`; the user must resolve via Keep Local / Use Server before retrying the switch. |
| Non-conflict error                           | Switch aborts with `{"status": "preflight_failed", "errors": [...]}`.                                                                                                                                       |

The pre-flight replaces the dedicated "unsynced local changes" / "tracked save missing" warnings the previous design
used — those were workarounds for not running the matrix. With the matrix in front of every switch, local data is always
captured (or the user is forced to resolve a real conflict) before the local file is overwritten.

### The four-step destructive switch

After the pre-flight clears, `VersionsService._rollback_to_version_io` (`services/saves/versions.py`) runs the switch —
the actual file/server writes go through `SyncEngine`:

1. **Download target**: GET the chosen older save's content and overwrite local. `_do_download_save` updates
   `tracked_save_id` and `last_sync_hash` to the target version, so even if step 2 fails the local view is consistent
   with the target.
2. **PUT to bump `updated_at`**: re-upload local content via `_do_upload_save(server_save=target_save)`. This issues a
   PUT against the target save id with byte-identical content. RomM fires the SQLAlchemy `onupdate=utc_now` hook on
   every PUT regardless of whether the content changed, so `save.updated_at` becomes NOW; the same request also upserts
   our `device_save_sync` row (`last_synced_at = updated_at`). The target save is now newest in the slot and
   `is_current` is already `true` for us.
3. **Confirm download (redundant)**: `_do_upload_save` still runs `_confirm_upload_sync`. On every supported RomM
   version the PUT already upserted our sync row (step 2), so this ack is at most a redundant idempotent re-write of
   `last_synced_at = save.updated_at`, not the load-bearing step it once was (#1458).
4. **Update local state**: `_do_upload_save` records `tracked_save_id`, `last_sync_hash`, `last_sync_server_updated_at`,
   and friends from the post-PUT response, leaving local state consistent with the now-newest server save.

After this, the next `compute_sync_action` for our device picks `target_save` (now newest), our `is_current=true`, hash
matches the baseline → `Skip(synced)`. Other devices on their next sync see `target_save` as newest with their own
`is_current=false` → matrix row 5 (`Download`) → adopt our switch. Cross-device propagation works without a dedicated
rollback API.

### Failure handling

- **Pre-flight `Conflict`**: switch never runs. Status: `conflict_blocked`. Local file untouched.
- **Pre-flight non-conflict error**: switch never runs. Status: `preflight_failed`. Local file untouched.
- **Step 1 (download) fails**: state is not mutated. Status: `not_found` (or surfaced error). Local file unchanged.
- **Step 1 (download) succeeds, step 2 (PUT) fails**: state mutation from the download is persisted. Status:
  `put_failed`. Local file and local state both point at the target. Cross-device propagation is incomplete — other
  devices still see the original newest save. Calling `rollback_to_version` again is safe and idempotent: step 1 is
  already done, step 2 retries the PUT.

## RomM Save Sync API Behaviour

The plugin depends on several RomM v4.8.1 behaviours that are not obvious from the OpenAPI schema and were discovered
while implementing the rewrite. They drive design decisions throughout the sync layer.

### `is_current` is computed, not stored

RomM's `device_save_sync` table stores `last_synced_at` and `is_untracked` per device per save. The `is_current` field
surfaced on each `device_syncs[]` entry of a `GET /api/saves` response is **derived at read time** as
`sync.last_synced_at >= save.updated_at` (greater-or-**equal** — equality counts as current). There is no column to set;
you can only push the components.

### `GET /api/saves` upserts `device_syncs` for the queried device

Hardware-verified on RomM 4.8.1: `GET /api/saves?rom_id=X&device_id=Y` upserts a `device_save_sync` row for device Y on
every save returned that did not already have one. The `optimistic` query flag does not appear to prevent the upsert.
The upserted row has `last_synced_at = save.updated_at`, which under the `>=` formula evaluates to `is_current = true` —
the freshly created row is already "current" (equality counts as current). Only a later foreign write, which advances
`save.updated_at` past our stored `last_synced_at`, flips us to `is_current = false`.

This has a concrete consequence for the sync algorithm: the "no entry for our device on the picked save" branch of
`compute_sync_action` (matrix rows 6a/6b) is unreachable in real plugin operation, because
`SyncEngine.do_sync_rom_saves` always calls `list_saves` (which triggers the upsert) before passing the data to the
algorithm. By the time the algorithm runs, our device entry exists on every server save. The branch is retained as
defensive code and is exercised by unit tests in `tests/domain/test_sync_action.py`.

### The `add_save` POST 409-gate

`POST /api/saves` does not blindly accept a new version. When the request carries a `device_id` and a `slot` and does
**not** set `overwrite`, RomM's `add_save` handler guards against a stale device clobbering the slot:

```text
if device_id and slot and not overwrite:
    latest = max(saves_in_slot, key=lambda s: s.updated_at)   # newest save in the slot
    sync   = get_device_sync(device_id, latest.id)            # our sync row for it, if any
    if sync is None or sync.last_synced_at < latest.updated_at:
        raise HTTP 409                                        # not current (or never synced) → refuse
```

Two branches raise: a device whose `last_synced_at` is behind the slot's newest `updated_at` (stale), **and** a device
that has **never** synced the slot (`sync is None`). The second branch is easy to miss — a brand-new device POSTing into
a slot that already has saves gets a 409, not a silent second version. The plugin relies on this as the server-side
backstop for its automatic-upload conflict path (see
[Upload-time conflicts (the 409 backstop)](#upload-time-conflicts-the-409-backstop)); an explicit `keep_local` sets
`overwrite=true`, which skips the gate entirely.

### POST and PUT both upsert the calling device's sync row

Re-verified against RomM 4.9.0 / 4.9.1 / 4.9.2 / 5.0.0: both `POST /api/saves` (`add_save`) and `PUT /api/saves/{id}`
(`update_save`) bump `save.updated_at` to the server's NOW **and** upsert the calling device's
`device_save_sync.last_synced_at = updated_at`. The POST additionally builds its response **after** that upsert, so the
body's `device_syncs` shows us `is_current = true` (equality counts) the moment it returns. Whether the PUT response
body carries `device_syncs` the same way is **unverified** — the skip logic is fail-open either way: a response that
proves us current skips the ack, anything else (including a bare PUT body) still sends the idempotent ack.

This makes the dedicated `POST /api/saves/{id}/downloaded` ack **redundant on the normal upload path** — the sync engine
skips it when the upload response already shows this device `is_current` (`_confirm_upload_sync`, #1458), saving one
round-trip per uploaded file. The historical `v4.8.1` note claiming the PUT did **not** upsert the sync row is obsolete
at the ≥ 4.9.0 floor.

**The one exception — `add_save`'s content-dedup early-return.** A named-slot `overwrite=false` POST whose content hash
matches an existing save in the slot returns that pre-existing save **before** the `device_save_sync` upsert (a stale
row, or a synthesized `is_current=false` placeholder). On that path the ack is the only writer of our sync row, so
`_confirm_upload_sync` fails open (any non-`is_current` response) and still fires. The ack stays best-effort — failures
are logged at debug and never fail the upload.

### GET `/content?optimistic=true` auto-upserts the sync row

`GET /api/saves/{id}/content?device_id=X&optimistic=true` (default `true`) is the canonical download endpoint. It
auto-upserts `device_save_sync` for the calling device with `last_synced_at = save.updated_at`. After a successful
download our `is_current` evaluates `true` without an extra round-trip.

`download_save_content` in `adapters/romm/romm_api.py` always passes `device_id` and `optimistic=true`. The
non-optimistic legacy `download_save(save_id, dest_path)` is retained for use cases that must not touch sync state but
is not used by the sync flow.

### Implication for the sync algorithm

Because `is_current` is computed and the only ways to make it `true` are a PUT/POST (which upserts the uploader's sync
row directly), or a `GET /content?optimistic=true`, the algorithm can trust `is_current` as authoritative without
further hashing. Row 8 in the matrix (no baseline yet, `is_current=true`, local exists) is the canonical adopt-baseline
case: we believe the server's claim and write `last_sync_hash := local_hash` so future runs can detect drift.

## Sync Flows

All four sync entry points share a single decision primitive — `compute_sync_action` — and a single dispatch path —
`_dispatch_sync_action`. The flows differ only in _when_ they fire and how they surface results.

### Pre-launch sync

Triggered from the game detail page when the user clicks the Play button (if `sync_before_launch` is enabled). This is
**not** triggered automatically via `RegisterForAppLifetimeNotifications` — pre-launch sync runs explicitly from
`CustomPlayButton.handlePlay()`.

1. User clicks Play on the game detail page.
2. `CustomPlayButton` calls `preLaunchSync(romId)` on the backend (15s timeout).
3. Backend fetches server saves, runs `do_sync_rom_saves` which iterates files and dispatches every
   `compute_sync_action` outcome.
4. If a `Conflict` was returned for any file, the result includes a `conflicts` list. `CustomPlayButton` shows
   `SyncConflictModal` for the first conflict, awaits the user's choice, then either re-runs sync (Keep Local / Use
   Server) or falls through (Cancel).
5. Game launches — but a sync failure or timeout no longer launches unconditionally. `runPreLaunchSync` surfaces a "Save
   Sync Unavailable" fallback-launch confirm; the launch proceeds only if the user confirms it, and is aborted (the
   button returns to "play") if they decline (#1050). The benign `savefiles_in_content_dir` skip still proceeds
   silently.
6. Toast notification shown on sync result.

### Post-exit sync

Triggered automatically when a game stops (if `sync_after_exit` is enabled).

1. `RegisterForAppLifetimeNotifications` fires with `bRunning: false`.
2. `sessionManager.handleGameStop` makes a single `finalizeGameSession(romId)` call; the backend
   `SessionLifecycleService.finalize` orchestrates playtime record → post-exit save sync → migration refresh and returns
   one typed payload (the old `recordSessionEnd` / `postExitSync` frontend callables were collapsed into it). If the
   plugin was reloaded mid-session, `handleGameStop` still fires for the adopted session — see
   [Surviving a plugin reload mid-session](#surviving-a-plugin-reload-mid-session) — so the post-exit sync is not
   skipped.
3. Backend runs `do_sync_rom_saves`. For most rows the local file's hash will differ from `last_sync_hash` (the user
   just played), so the typical action is `Upload` — matrix row 9 — POSTed as a new version (`overwrite=false`,
   409-backstopped).
4. If a `Conflict` is returned, a toast notifies the user. The modal is **not** opened post-exit — the conflict re-fires
   at the next pre-launch sync, where the user resolves it via Keep Local / Use Server before launch.
5. Toast notification shown on success or conflict.

### Manual sync all

User-initiated from the "Sync All Saves Now" button in Save Sync settings.

1. Iterates all installed ROMs from the backend registry.
2. For each ROM **whose slot the user has confirmed** (`slot_confirmed`): runs `do_sync_rom_saves`. A never-configured
   ROM — one the user has not yet set up save sync for — is **skipped**, so its possibly-stale local save can't be
   auto-uploaded into the default slot and overwrite another device's newer progress (#1055). The single-ROM paths
   (pre-launch / post-exit / per-game manual sync) stay ungated — those are the user's explicit per-ROM actions and the
   first-sync auto-seed path, where the user decides.
3. Per-rom asyncio.Lock prevents collision with concurrent pre-launch / post-exit syncs.
4. Reports total synced count and number of pending conflicts. Conflicts surface via the modal individually at each
   game's next pre-launch sync. Skipped (unconfirmed) ROMs contribute zero synced / zero conflicts; `roms_checked` stays
   the count of installed ROMs iterated.

### Get save status (read-only)

Triggered by the game-detail panel and SAVES tab via `getSaveStatus(romId)`. Runs `_get_save_status_io` — a read-only
counterpart of `do_sync_rom_saves` that returns the same `compute_sync_action` decisions but performs no upload/download
I/O. The only mutation it allows is recording `last_sync_hash` for `Skip(adopt_baseline=True)` rows so future drift
detection works.

### Offline queue drain

If the RomM server is unreachable when a sync runs:

1. `compute_sync_action` is never reached — `list_saves` raises and the rom-level sync returns an error string.
2. The local save file is untouched. State is untouched.
3. On the next successful server contact (next sync attempt, manual sync, or pre-launch), the algorithm runs against
   current server state and produces the same outcome it would have produced earlier — typically Upload (post-play) or
   Skip.
4. No data is lost. There is no separate retry queue because the algorithm is idempotent: re-running it after a
   transient failure converges on the same end state.

### Heartbeat error classification (launch-time probe)

`pre_launch_sync` and `post_exit_sync` pre-probe the server with a single `heartbeat` call before doing any sync work. A
failure here is **classified by type**, not collapsed onto a blanket "Server offline"
([#971](https://github.com/danielcopper/decky-romm-sync/issues/971)):

- A genuine reachability failure (`RommConnectionError` / `RommTimeoutError`) returns the canonical `SERVER_UNREACHABLE`
  shape with `message: "Server offline"` **plus** the additive `offline: true` flag the launch path routes on
  (offline-drift check instead of a doomed round-trip).
- Any other typed `RommApiError` flows through `lib/errors.py` `classify_error`, so the result carries its **own**
  `reason` + `message`: a revoked token (401) surfaces `AUTH_FAILED` + "Authentication failed — check your username and
  password", an SSL misconfig surfaces the SSL message, a 5xx surfaces the server-error message. These branches **omit**
  the `offline` flag, so the UI never claims a reachable server is unreachable. The Play button's fallback launch modal
  shows that backend `message` verbatim, so a user whose token expired sees "authentication failed" instead of being
  told the server is offline forever.

The raw exception is logged at debug in every branch, so the probe is no longer a silent swallow. The same
classification applies to the device-registration failure path in `services/saves/sync_engine/devices.py`
(`ensure_device_registered`): an auth/SSL failure during `register_device` produces its own classified `reason` +
`message` rather than a generic "Could not register device" unreachable slug.

### `DeviceRegistry` owns device identity

`DeviceRegistry` (`services/saves/sync_engine/devices.py`) is the **single owner** of the server device id. It reads
`kv_config["device_id"]` **once** through a narrow Unit of Work and serves the cached value thereafter via
`get_device_id()` — no per-flow transaction. The cache is refreshed when registration writes a new id and can be dropped
via `invalidate_device_id_cache()` for the rare case where `kv_config["device_id"]` is mutated outside the registry
(registration is the only in-process writer, so this is currently reached only from test backdoors). Every save-sync
sub-service that needs the id — `SyncEngine`, `StatusService`, `VersionsService`, the slot sub-services (`SlotListing` /
`SlotSwitcher` / `SetupWizard` / `SlotDeleter`), and `RollbackOrchestrator` — receives the shared `DeviceRegistry`
through its `*ServiceConfig` (the [same-bounded-context peer-ref carve-out](backend-architecture.md)) and reads the id
through it, instead of each opening its own `kv_config` read. The registry is built once in the `SaveService` facade and
threaded into every sub-service config.

## Playtime Tracking

### Local delta-based accumulation

Playtime is tracked per-ROM in SQLite — the `Playtime` aggregate spanning `rom_playtime` (scalars) and
`rom_playtime_sessions` (the pending-session outbox) — independent of the `saves` lifecycle. Uninstalling a ROM deletes
only its files and `rom_installs` row, leaving playtime and saves intact per
[ADR-0007](../adr/0007-rom-retention-identity-anchor.md).

Session tracking:

1. `recordSessionStart(romId)`: backend opens the session marker (`last_session_start`) on the ROM's `rom_playtime` row
   in a short write Unit of Work, then schedules a background flush of the pending-session outbox (draining any offline
   backlog on launch)
2. `recordSessionStart` also stamps a monotonic start on the row: `begin_session` stores `Clock.monotonic()` in
   `last_session_start_monotonic` alongside the wall-clock start. The monotonic clock pauses while the device is
   suspended, so its delta across the session is awake-only time — this is how suspend time is excluded (#1148). The
   frontend does no suspend accounting: the Steam suspend/resume hooks never fired on current SteamOS, so the frontend
   machinery was removed and `finalizeGameSession(romId)` carries no suspend duration
3. Session end (`finalizeGameSession(romId)` → backend `record_session_end`): in an executor worker, a short write UoW
   folds the closed session into the aggregate (`record_session` counts `monotonic_end` minus the stored monotonic start
   — the awake-only span — clamped to the wall-clock span and to 0–24h, increments `total_seconds` and `session_count`,
   records `last_session_duration_sec`) **and** enqueues the session into the outbox (when a device id is registered —
   an unregistered device folds locally and never enqueues); then, outside the transaction, the outbox flushes to RomM's
   native `/api/play-sessions` ingest (best-effort, offline-safe). When the monotonic delta is unusable (no stored start
   on a pre-migration row, a negative delta from a mid-session reboot, or a delta more than a ~2 s tolerance above the
   wall span) the counted duration falls back to the full wall span — the pre-#1148 behavior, never a regression. A
   debug line (`record_session_end: rom N wall=Xs mono=Ys awake=Zs`) is the on-device verification hook

Playtime is **additive, not a conflict** — the union of per-device session streams, so it needs none of the save-sync
newest-wins / conflict / `.romm-backup` machinery. The server dedupes on `(user_id, device_id, rom_id, start_time)`, so
a re-POST of a queued session is idempotent; the outbox dequeues on a `created` or `duplicate` result and stays queued
only on `error` (ADR-0018).

### Surviving a plugin reload mid-session

`destroySessionManager` wipes the in-memory session on unload, so a plugin reload while a game is running would leave
the next game-stop with nothing to finalize — the pre-reload playtime is lost and the post-exit sync never runs. Two
signals let the re-initialized `sessionManager` recover it:

- **Steam running-state (liveness).** A defensive multi-source reader (`utils/runningApps`) is the authority for
  _whether_ the game is still running at re-init — the durable marker (`last_session_start`) is written by
  `recordSessionStart` precisely so it survives the reload, but only Steam can attest the session has not already ended.
  No single Steam surface is trusted: `Router.MainRunningApp` never repopulates after a full `plugin_loader` restart
  without a fresh lifecycle event the reloaded context missed (#1054 / #1148 round 2), so the reader also consults the
  running-apps lists (`Router.RunningApps`, `SteamUIStore.RunningApps`), each through a guard, merged + de-duped. This
  read is **polled** (every 500ms for up to 15s), not one-shot, and a timed-out round logs the per-source `diagnostics`
  so the on-device log names the surface that actually works on a given build.
- **A localStorage breadcrumb (attestation).** A single versioned row (`decky-romm-sync:active-session` →
  `{v, appId, romId, startMs}`) is written at start and removed at stop. It is **not** cleared by
  `destroySessionManager`, so it outlives the reload. Every localStorage access is wrapped — a storage failure degrades
  to the no-attestation path, never throws.

At `initSessionManager` the recovery runs on the lifecycle chain (so a stop event racing the liveness poll serializes
after adoption — adopt first, then finalize):

- **Game running + breadcrumb matches** → adopt in-memory state from the breadcrumb (`sessionStartTime`) and leave the
  durable marker untouched. Re-stamping would discard the pre-reload span the backend already holds.
- **Game running, no / mismatched / corrupt breadcrumb** → adopt and re-stamp the marker to a truthful lower bound
  (`recordSessionStart`), then write a fresh breadcrumb so a subsequent reload adopts via the matching case instead of
  re-stamping again.
- **Breadcrumb present but nothing running once the liveness poll times out** (or a non-RomM app is foreground) → the
  session ended while the plugin was down; a truthful finalize is impossible without an observed end, so the breadcrumb
  is dropped and the session is logged as orphaned — never a fabricated end time. A stale breadcrumb left by a reboot
  resolves the same way at the next init; no expiry timers.

**Attestation invariant:** every finalize fold uses a marker stamped by a start we actually observed (either the
original `recordSessionStart` or an adoption re-stamp) — the client never invents an end time for a session whose stop
it did not see.

### Steam display

Steam natively tracks playtime for non-Steam shortcuts. No additional work is needed — Steam's built-in tracking handles
the display in the library.

### RomM last_played and cross-device reconcile

The native ingest updates the ROM's `last_played` timestamp (and `device.last_seen`) on the RomM server, keeping the
library sorted by recent activity. Opening a game's detail page runs `reconcile_playtime(romId)`: it flushes the outbox,
then `GET /api/play-sessions` for the ROM and sets the displayed total to
`max(local_total, Σ server duration_ms / 1000)`. Playtime is monotonic, so `max()` is always safe — with the outbox
drained it adopts the cross-device server union; offline / partial-flush / not-yet-backfilled it keeps the display from
regressing below local truth. The GET needs the `roms.user.read` scope (added to the minted token in #1280); without it
the reconcile degrades to local-only — never an error.

The `reconcile_playtime` result also carries the restored `last_played` (ISO-8601). The game-detail Play section
(`RomMPlaySection`) renders it as the **LAST PLAYED** value in preference to Steam's device-local `rt_last_time_played`:
Steam synthesizes the latter to "now" after a device cutover / fresh device, so the restored cross-device timestamp is
the truthful one. When `last_played` is `null` (the server has no session for the ROM yet, or the reconcile ran
local-only) the display falls back to Steam's value, so there is no regression before any server data exists. This is
display-only — the plugin does not write the restored value back into Steam's `rt_last_time_played` (#1294).

## Save-Sync State — the `RomSaveState` aggregate

Per-ROM save-sync state lives in SQLite — there is no JSON file. The per-ROM scalars are the `RomSaveState` aggregate
(`domain/rom_save_state.py`), backed by the `rom_save_states` table; the per-file baselines are `FileSyncState` value
objects (one per filename), backed by the `rom_save_files` table. Both are reached through the Unit of Work as
`uow.rom_save_states`, which spans the two tables (sync sqlite3 run via `run_in_executor`, per
[ADR-0004](../adr/0004-sync-sqlite-unit-of-work.md)).

The canonical source for the table DDL, columns, and aggregate invariants is [database-design.md](database-design.md).
This page describes the state conceptually; the field reference below maps each logical field to its column.

The save-sync **feature toggles** (`save_sync_enabled`, `sync_before_launch`, `sync_after_exit`, `default_slot`,
`autocleanup_limit`) and the **device label** (`device_name`) live in `settings.json`, not in this aggregate — they are
user-intent config, not synced relational state (ADR-0003). Device identity is `kv_config['device_id']` (see the
[Device Registration](#device-registration) section above), not a field on the per-ROM aggregate.

The logical shape of a single ROM's save state — the scalars as a `rom_save_states` row plus its child `rom_save_files`
rows — looks like this:

```json
{
  "42": {
    "system": "gba",
    "active_slot": "default",
    "slot_confirmed": true,
    "last_synced_core": "mgba_libretro",
    "own_upload_ids": [18],
    "last_sync_check_at": "2026-02-17T10:31:00+00:00",
    "files": {
      "game.srm": {
        "tracked_save_id": 18,
        "last_sync_hash": "d41d8cd98f00b204e9800998ecf8427e",
        "last_sync_at": "2026-02-17T10:30:00+00:00",
        "last_sync_server_updated_at": "2026-02-17T10:30:00+00:00",
        "last_sync_server_save_id": 18,
        "last_sync_server_size": 32768,
        "last_sync_local_mtime": 1739789395.0,
        "last_sync_local_size": 32768
      }
    }
  }
}
```

Per-ROM playtime is a separate aggregate (`Playtime`, `rom_playtime` table) — see
[Playtime Tracking](#playtime-tracking) above.

### Field reference

The `saves.<id>.*` fields are columns on the `rom_save_states` table (one row per ROM); the `saves.<id>.files.<fn>.*`
fields are columns on the `rom_save_files` table (one row per `(rom_id, filename)`). The `saves.<id>` / `files.<fn>`
notation here mirrors the logical shape above — see [database-design.md](database-design.md) for the physical column
names and constraints.

| Field                                               | Type                    | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `saves`                                             | object                  | Per-ROM sync metadata, keyed by `rom_id` (string)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `saves.<id>.system`                                 | string                  | RetroDECK system slug (e.g. `"gba"`, `"snes"`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `saves.<id>.emulator`                               | string                  | Emulator tag (default `"retroarch"`); forms the RomM save-folder path `saves/{system}/{rom_id}/{emulator}/`.                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `saves.<id>.active_slot`                            | string                  | Which RomM slot this game syncs to (e.g. `"default"`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `saves.<id>.slot_confirmed`                         | boolean                 | Whether user has explicitly chosen their slot (see "Slot Setup Wizard")                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `saves.<id>.last_synced_core`                       | string / null           | RetroArch core used at last sync (for core change detection, e.g. `"mgba_libretro"`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `saves.<id>.own_upload_ids`                         | array of integer        | Save ids this device originally POSTed. Drives the `uploaded_by_us` indicator on the SAVES tab.                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `saves.<id>.slots`                                  | object                  | Merged slot listing (read-model cache): per slot, its `source` / `count` / latest `updated_at`.                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `saves.<id>.last_sync_check_at`                     | ISO-8601 string / null  | Timestamp of the most recent `do_sync_rom_saves` run for this rom (regardless of whether files transferred).                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `saves.<id>.files`                                  | object                  | Per-file sync state, keyed by filename (e.g. `"game.srm"`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `saves.<id>.files.<fn>.tracked_save_id`             | integer / null          | Most recent RomM save id this device tracked. Used to exclude the active save from the Previous Versions dropdown and as an uploader-attribution hint; **not** consulted by `compute_sync_action` (the algorithm picks newest by `updated_at`).                                                                                                                                                                                                                                                                                                          |
| `saves.<id>.files.<fn>.last_sync_hash`              | content-hash hex string | RomM-parity `content_hash` of the save file at last sync (zip-aware: MD5 for a single file, per-entry combined for a zip — `SaveFileStore.content_hash`, #1457). Drift baseline used by matrix rows 7/8/9/10/11a/11b/12.                                                                                                                                                                                                                                                                                                                                 |
| `saves.<id>.files.<fn>.last_sync_at`                | ISO-8601 string         | Timestamp of last successful sync.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `saves.<id>.files.<fn>.last_sync_server_updated_at` | ISO-8601 string         | Server's `updated_at` at last sync.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `saves.<id>.files.<fn>.last_sync_server_save_id`    | integer                 | RomM save id for the most recently synced server save.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `saves.<id>.files.<fn>.last_sync_server_size`       | integer                 | Server file size at last sync.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `saves.<id>.files.<fn>.last_sync_local_mtime`       | float                   | Local file mtime (epoch seconds) at last sync.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `saves.<id>.files.<fn>.last_sync_local_size`        | integer                 | Local file size (bytes) at last sync.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `playtime`                                          | object                  | Per-ROM playtime lives in the `rom_playtime` table (the `Playtime` aggregate, with its `rom_playtime_sessions` outbox child), a separate aggregate from saves — `RomRemovalService` keeps playtime on uninstall per [ADR-0007](../adr/0007-rom-retention-identity-anchor.md). The scalar columns below (`total_seconds`, `session_count`, `last_session_start`, `last_session_start_monotonic`, `last_session_duration_sec`) are keyed by `rom_id`; the outbox rows are keyed `(rom_id, start_time)`. See [Playtime Tracking](#playtime-tracking) above. |
| `playtime.<id>.total_seconds`                       | integer                 | Accumulated playtime in seconds.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `playtime.<id>.session_count`                       | integer                 | Number of completed play sessions.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `playtime.<id>.last_session_start`                  | ISO-8601 / null         | Start time of current session (null when not playing).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `playtime.<id>.last_session_start_monotonic`        | float / null            | `Clock.monotonic()` reading captured when the session opened; its delta to session end is the awake-only span (suspend excluded, #1148). Null when idle or on a pre-migration row (→ wall-span fallback).                                                                                                                                                                                                                                                                                                                                                |
| `playtime.<id>.last_session_duration_sec`           | integer / null          | Duration of last completed session.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |

The save-sync feature toggles (`save_sync_enabled`, `sync_before_launch`, `sync_after_exit`, `default_slot`,
`autocleanup_limit`) and the device label (`device_name`) live in `settings.json` (ADR-0003), not in this aggregate.
`save_sync_enabled` is the master feature toggle — when it is off, the universal launch gate
(`LaunchGateService.evaluate`) skips the save-status round-trip, and `get_save_status` itself returns an empty
`conflicts` array, so no consumer (the launch gate, the play button, the `save_status_updated` push that `index.tsx`
forwards) surfaces a conflict the user has no UI to resolve — the SAVES tab where one would resolve it is hidden while
disabled. A stale server-side conflict (e.g. another device moved the save) therefore can't render a game unplayable.
`sync_before_launch` / `sync_after_exit` gate the automatic pre-launch / post-exit syncs; `default_slot` is the slot new
games adopt (`"default"`); `autocleanup_limit` caps retained save versions per slot on the server (10).

Conflicts are no longer persisted. They are returned ephemerally from `do_sync_rom_saves` and `_get_save_status_io` and
surfaced via the modal at the moment of the sync. If the user dismisses the modal (Cancel), the conflict re-fires on the
next sync as long as the underlying state still produces a conflict row (12, 6c, 11b, or 9b).

### Legacy field migration

The per-file schema migrations that the old JSON aggregate ran at load time are moot: SQLite starts empty and no JSON
state is imported into it (this is a beta plugin — the library re-syncs from RomM). There is no on-disk aggregate to
rebuild, so the old `active_core` → `last_synced_core` rename and the `dismissed_newer_save_id` strip no longer happen.

The one surviving legacy read is a single one-time settings fold at bootstrap. `fold_legacy_save_sync_settings`
(`py_modules/domain/state_migrations.py`) lifts the old `settings` block (the save-sync feature toggles) plus
`device_name` out of any pre-existing `save_sync_state.json` and folds them into `settings.json` — the `settings.json`
v3 → v4 schema bump. After that fold, `save_sync_state.json` is never read or written again; the file is not a
persistence store anymore.

## Session Detection

Game start and stop events are detected using Steam's frontend APIs, not by polling emulator processes.

### RegisterForAppLifetimeNotifications

The primary mechanism. `SteamClient.GameSessions.RegisterForAppLifetimeNotifications` fires a callback whenever any app
(including non-Steam shortcuts) starts or stops.

The callback receives:

- `bRunning: boolean` — whether the app just started (`true`) or stopped (`false`)
- `unAppID: number` — the app ID

### Running-app detection (`utils/runningApps`)

After a game starts, there is a brief window where the app ID may not be fully resolved. The session manager waits 500ms
and then reads the defensive running-app reader for a reliable `appid` and `display_name`, falling back to `unAppID`
from the notification if nothing is reported. The reader consults MULTIPLE Steam surfaces — `Router.MainRunningApp` plus
the `Router.RunningApps` / `SteamUIStore.RunningApps` lists — each through a guard (an absent, `null`, or
throwing-getter surface degrades to "reported nothing", never a throw), merges them into one de-duped list, and returns
a per-source `diagnostics` string. No single surface is reliable across builds/timing: after a `plugin_loader` restart
`Router.MainRunningApp` stays null for several seconds and never repopulates without a fresh lifecycle event (#1054 /
#1148 round 2), so the adoption path **polls** the reader (every 500ms up to 15s) instead of reading one surface once,
and a failed round logs what every candidate reported. The same reader backs the already-running skip on both launch
surfaces — the interceptor and the Play button ([ADR-0015](../adr/0015-single-launch-gate-cancel-then-relaunch.md)).

### State-aware Resume button (#1313)

When the game is already running, the RomM Play button (`CustomPlayButton`) renders **Resume** — top precedence over its
install / conflict / download states — and a press brings the live session to the foreground via **Steam's own gamescope
"Resume Game" path**: `SteamUIStore.SetRunningApp(appId)` followed by `SteamUIStore.NavigateToRunningApp()`. This is
pure UI focus navigation, not a launch: it fires no `GameActionStart` (so the launch interceptor never re-enters) and
shows no Steam "already running" dialog, which dissolves the mid-session-sync problem at the UX level rather than
defending against it. (`SteamClient.Apps.RaiseWindowForGame` — the earlier approach — is a **desktop-overlay** call:
native only acts on it when `SteamUIStore.GetOverlayInstances(appId)` is non-empty, which it never is in gamescope Game
Mode, so it reports `Success` but silently does nothing. Hence the switch to the store-navigation path.) A click first
runs a **liveness gate**: if nothing is actually running (`isAppRunning(appId)` / `getActiveSessionRomId()` both say no
— a stale overlay from a session that ended without a stop event reaching the button), it clears the overlay and falls
through to the normal launch funnel (self-heal). If `NavigateToRunningApp` is absent on an older SteamUI build, the
foreground falls back to `Navigation.Navigate("/apprunning")` after the `SetRunningApp` selection (same visible effect).

Detection is **reactive, not polled**: the overlay is seeded synchronously at mount from `getActiveSessionRomId()` /
`isAppRunning(appId)` (so a page opened mid-session — or after a reload-adoption — shows Resume immediately) and flipped
live by the `romm_session_changed` DOM event, which `sessionManager` dispatches on game start (`running: true`), game
stop (`running: false`), and both reload-adoption branches (`running: true`). The already-running guards from #1148 /
#1308 (the interceptor and the `handlePlay` guard) stay in place as **backstops** for the render→click race, where the
session begins between the button rendering Play and the user pressing it.

### App ID to ROM ID mapping

The session manager maintains a cached `appId -> romId` map loaded from the backend's synced-ROM registry (the `roms`
SQLite table, via `get_app_id_rom_id_map`). This map is refreshed:

- On session manager initialization (plugin load)
- Before each game start event (in case a sync added new shortcuts)

If the launched app ID is not in the map, it is not a RomM shortcut and the session manager ignores it.

### Suspend exclusion via the monotonic clock (#1148)

Sleep time is excluded from playtime **backend-side**, using the monotonic clock — not the Steam suspend/resume events.
`CLOCK_MONOTONIC` pauses while the device is suspended (hardware-verified on the Steam Deck), so the monotonic delta
across a session counts only awake time. `begin_session` stores a `Clock.monotonic()` start on the `rom_playtime` row;
at session end `record_session` counts `monotonic_end` minus that start, clamped to the wall-clock span (awake can never
exceed the real elapsed span) and then to 0–24 h.

When the monotonic delta is unusable the counted duration falls back to the full wall span — exactly the pre-#1148
behavior, so a missing/invalid reading is never a regression, only the loss of the suspend exclusion. The fallback
covers three cases: no stored monotonic start (a pre-migration row, or a session opened before the column existed), a
negative delta (the monotonic counter reset across a reboot mid-session), and a delta more than a ~2 s tolerance above
the wall span (the two readings cannot belong to the same session).

There is **no frontend suspend machinery**. The earlier design registered Steam suspend/resume hooks and accumulated a
`suspended_seconds` value passed to `finalizeGameSession`, but those hooks (`System.RegisterForOnSuspendRequest` /
`RegisterForOnResumeFromSuspend` and their renamed `User.*` progress successors) never fired on current SteamOS, so the
accumulator stayed at zero and the whole mechanism was removed. `finalizeGameSession(romId)` now carries no suspend
argument.

## Native play-session ingest (ADR-0018)

Playtime uses RomM 4.9's first-party play-session store, not a storage hack. Two round-trips, both best-effort and off
the hot path:

- **Ingest (`POST /api/play-sessions`).** On game-exit the closed session `(rom_id, start_time, end_time, duration_ms)`
  is enqueued into the `rom_playtime_sessions` outbox and POSTed under this device's `device_id` (batch, max 100). The
  server accumulates the additive union across devices and dedupes on `(user_id, device_id, rom_id, start_time)`, so a
  re-POST is idempotent. A `created` or `duplicate` result dequeues the outbox row; only an `error` keeps it queued.
  Offline, the session stays queued and flushes on the next launch/session-end/reconcile.
- **Reconcile (`GET /api/play-sessions?rom_id={id}`).** Opening a game's detail page runs `reconcile_playtime(rom_id)`:
  flush the outbox, sum the returned sessions' `duration_ms`, and fold `Σ / 1000` into the local total via
  `reconcile_total` (monotonic `max()`, never regresses). The GET needs the `roms.user.read` scope (minted in #1280);
  without it the reconcile degrades to local-only, never an error.

The local `Playtime` total stays the always-correct cumulative read-model (folded regardless of network); the server
holds the interoperable per-session record. `duration_ms` is screen-on time — our monotonic-derived awake seconds × 1000
(suspend excluded, #1148) — mapping 1:1 onto RomM's model. There is no `content_hash`, no newest-wins, no conflict
modal, no `.romm-backup`: playtime is additive, so none of the save-sync machinery applies.

### Migration off the note

Before ADR-0018 the plugin stored playtime in a RomM user note (`romm-sync:playtime` =
`{"seconds", "updated", "device"}`). That note is **retired**: the plugin no longer reads or writes it, and migration
`006` drops the now-readerless `rom_playtime.note_id` column. Native accumulation starts fresh at the cutover (option B1
— no backfill of the historical total); the local total keeps showing the true historical value via `max()` until
server-side accumulation overtakes it. Existing `romm-sync:playtime` notes are left in place on the server — orphaned
but harmless (no reader remains) — rather than mass-deleted; a release note tells users they may delete them, and an
optional Settings cleanup action can be added later. A backfill of the historical total is deferred to #868.

## Known Limitations

### Standalone emulators not supported

Phase 5 only covers RetroArch `.srm` saves. Standalone emulators store saves under
`<saves_path>/<platform>/<emulator_name>/` with emulator-specific formats:

| Platform | Emulator    | Save Path                   | Format                          |
| -------- | ----------- | --------------------------- | ------------------------------- |
| psx      | DuckStation | `psx/duckstation/memcards/` | `.mcd` shared memory cards      |
| ps2      | PCSX2       | `ps2/pcsx2/memcards/`       | `.ps2` shared memory cards      |
| gc       | Dolphin     | `gc/dolphin/{US,EU,JP}/`    | Per-region memory card files    |
| wii      | Dolphin     | `wii/dolphin/`              | Wii save data + virtual SD card |
| nds      | melonDS     | `nds/melonds/`              | Per-game `.sav` files           |
| n3ds     | Azahar      | `n3ds/azahar/`              | NAND/SDMC title ID structure    |
| PSP      | PPSSPP      | `PSP/PPSSPP-SA/`            | Title ID directories            |
| wiiu     | Cemu        | `wiiu/cemu/`                | mlc01 title ID structure        |
| switch   | Ryubing     | `switch/ryubing/`           | User profile-based save data    |
| xbox     | Xemu        | `xbox/xemu/`                | Xbox HDD image saves            |

Key challenges:

- PCSX2 and DuckStation use shared memory cards (multiple games on one file) requiring system-level sync
- Dolphin, PPSSPP, Azahar, Cemu, and Ryubing organize saves by title ID, requiring title ID mapping databases
- Each emulator needs a dedicated save handler

Standalone emulator support is tracked on the [GitHub Projects board](https://github.com/users/danielcopper/projects/2).

### Shared memory cards deferred

PS1 and PS2 games using RetroArch cores that save to shared memory cards (rather than per-game `.srm`) are not handled.
Syncing a shared memory card affects all games on the card, requiring system-level tracking rather than per-game
tracking. Deferred to Phase 7.

### No aggregate playtime field in RomM (yet)

RomM 4.9 stores raw play-session rows and renders `last_played`, but has **no aggregate playtime number** and no
frontend playtime surface yet. The plugin's local `Playtime` total is therefore still the display read-model; the native
store is forward-compatible with a future RomM playtime UI (#903). Under the no-backfill cutover (option B1) the server
under-reports the true historical total until it re-accumulates — `max()` protects the local display; a
synthetic-session backfill is deferred to #868. See
[Native play-session ingest (ADR-0018)](#native-play-session-ingest-adr-0018) above.

### Emulator save states not synced

RetroArch save states (`<states_path>/{system}/`, where `<states_path>` comes from `retrodeck.json` →
`paths.states_path`) are not synced. Only SRAM saves (`.srm`) are handled. Save states are large,
emulator-version-specific, and not portable between different RetroArch core versions.

### Save slot migration between slots not yet implemented

Moving saves between slots (copy from slot A to slot B) is not supported. Users can delete slots (which removes all
saves in the slot from the server) and create new ones, but there is no "move saves from slot X to slot Y" operation.

### Cross-device save browsing limited

While `device_syncs` per save shows which devices have synced, the plugin cannot filter or browse saves by a specific
other device. This is an API limitation — `GET /api/saves?device_id=X` only populates `device_syncs` for device X, not
for arbitrary devices.
