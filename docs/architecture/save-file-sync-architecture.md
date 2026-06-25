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

Requires RomM >= 4.8.1. The plugin rejects servers below 4.8.1 with `reason: "version_error"`.

| Endpoint                                                 | Method | Notes                                                                                                                                                                                                                              |
| -------------------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/api/saves?rom_id={id}`                                 | GET    | Returns array. Each item now includes `slot`, `file_name_no_tags`, `file_extension`, `content_hash`, and `device_syncs` array.                                                                                                     |
| `/api/saves/{id}`                                        | GET    | Single save metadata with v4.7 fields                                                                                                                                                                                              |
| `/api/saves?rom_id={id}&emulator={emulator}&slot={slot}` | POST   | Creates a new save entry. Slot-aware: `slot=default` causes RomM to append a timestamp to the filename (e.g. `Game.srm` becomes `Game [2026-03-24_15-18-50].srm`). Same filename + same slot = upsert. Different slot = new entry. |
| `/api/saves/{id}`                                        | PUT    | Updates file content only. No metadata changes, no new entry created.                                                                                                                                                              |
| `/api/saves/{id}/content`                                | GET    | Binary download by save ID (new in v4.7)                                                                                                                                                                                           |
| `/api/devices`                                           | GET    | List all registered devices for the authenticated user. Returns array of `{id, name, platform, client, client_version, last_seen, created_at, ...}`.                                                                               |
| `/api/devices`                                           | POST   | Register a device. Accepts hostname, platform, client info. Returns `device_id` (UUID).                                                                                                                                            |
| `/api/devices/{id}`                                      | DELETE | Remove a device registration. Returns 204 No Content. PATCH (rename) is not supported (405).                                                                                                                                       |
| `/api/saves/delete`                                      | POST   | Bulk delete saves by ID. Body: `{"saves": [id1, id2, ...]}`. Returns result dict.                                                                                                                                                  |

**New parameters on POST:**

- `slot` — slot name (e.g. `"default"`). If omitted, save has `slot=null` (legacy behavior).
- `autocleanup_limit` — max save versions retained per slot (default: 10).
- `device_id` — server-registered device UUID. Used to populate `device_syncs` per save.

**New fields on save metadata:**

- `slot` — the slot this save belongs to (string or null)
- `file_name_no_tags` — base filename without timestamp tags (e.g. `Game` from `Game [2026-03-24_15-18-50].srm`)
- `file_extension` — file extension (e.g. `srm`)
- `content_hash` — MD5 hash of the save file content (eliminates download-and-hash slow path)
- `device_syncs` — array of per-device sync records: `device_id`, `device_name`, `is_current`, `last_synced_at`

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
- All subsequent syncs = PUT to the tracked `save_id` (content update, no stacking)
- Normal single-device flow: exactly 1 save entry per game per slot
- Multi-device: all devices share the same save entry via `tracked_save_id`

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

### The `none` slot (legacy)

- Saves uploaded before v2 (or without slot parameter) have `slot=null`
- These are separate entries from `slot="default"` — different slot = different save
- The Slot Setup Wizard detects these and lets users choose how to handle them
- In the plugin the legacy slot is the equivalence class `slot ∈ {null, ""}`: state stores `active_slot=None`, the
  persisted slots map keys it `""`, and the server returns `slot: null`. `domain/save_slot.py` (`normalize_slot`) is the
  single place this equivalence is defined.

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

The **upload** side honours the same equivalence (`MatrixExecutor._resolve_upload_slot`): a sync on the legacy slot
(`active_slot=None` with a populated `slots` map — the state after switching to / confirming legacy) uploads with the
`slot` param **omitted**, so the server stores a `slot: null` save. Only a brand-new ROM (no `slots` yet) seeds the
configured default slot for its first sync. Returning `"default"` for `active_slot=None` was a sibling of the same bug —
a save played on the legacy slot was misfiled into the default slot, so switching back to legacy found nothing on the
server (#1061).

### Confirming a slot (`confirm_slot_choice`)

The wizard confirms a slot through `confirm_slot_choice(rom_id, chosen_slot, migrate, migrate_from_slot)`:

- `chosen_slot=null` confirms **legacy** mode (`active_slot=None`, `slot_confirmed=true`); a non-empty string confirms a
  named slot; an empty/whitespace string is rejected (`invalid_slot_name`).
- `migrate` is an explicit boolean — the default (`false`) never migrates. When `true`, saves are migrated from
  `migrate_from_slot` (`null` = the legacy source) into `chosen_slot`, and a server save is deleted from the old slot
  **only if it was successfully re-uploaded** into the new one; non-matching saves are left in place and reported (so a
  save uploaded under a different ROM filename by another device is never destroyed).

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

RomM ≥4.8.1 dedupes devices by fingerprint — `mac_address`, OR `hostname` + `platform` — and returns the existing device
instead of minting a duplicate (`allow_existing` defaults true). The `name` field is **not** fingerprinted, so without a
stable fingerprint every local-state wipe (the SQLite reinstall path) would create a fresh duplicate device on each
reinstall.

The plugin sends `/etc/machine-id` as the RomM `hostname`: it is machine-derived (survives a reinstall), unique per
device (two Steam Decks stay distinct), and stable. The real OS hostname is deliberately **not** sent — two stock Steam
Decks both report `steamdeck`, so a `hostname` + `platform` fingerprint built from the OS hostname would collide them
into one server device. The friendly OS hostname remains the display-only `name`. When `/etc/machine-id` is unreadable
the `hostname` field is omitted entirely, degrading to the pre-4.8.1 no-fingerprint behaviour rather than sending a
colliding value.

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
  `tracked_save_id`, `last_sync_hash`, `last_sync_server_updated_at`, `last_sync_local_mtime`, etc.
- **`device_id`** — this device's RomM-server ID (used to find our entry in `server_save.device_syncs`).
- **`local_hash`** — pre-computed MD5 of `local_file`, or `None`.

### Pick rule and discriminators

Within `server_saves_in_slot`, the algorithm picks the **newest by `updated_at`** as the canonical save and decides
against that one. Other saves in the slot are ignored — no foreign-save surfacing, no per-save dismiss state.

Three discriminators drive the branch:

1. **Our device's entry on the picked save**: `server.device_syncs[me]` may be missing (we never touched this save),
   `is_current=true` (server claims our last write/read is current), or `is_current=false` (someone else has moved this
   save forward since we last touched it).
2. **Hash divergence vs. baseline**: `local_hash != files_state["last_sync_hash"]` means the local file has been edited
   since the last successful sync. Without a baseline (`last_sync_hash` is missing) we cannot claim divergence.
3. **Size plausibility (upload guard only)**: in the one branch that PUTs in place (our device `is_current=true` + local
   diverged, row 9), `local_file.size` is checked against the recorded `last_sync_local_size` baseline via
   `domain/save_size.is_implausibly_shrunken`. A 0-byte or implausibly-shrunk local is a crash artifact, not an edit,
   and diverts that branch to `Conflict` (row 9b) so RomM's in-place PUT never overwrites the only good copy (#1062).

`is_current` is **computed server-side**, not stored — see [RomM Save Sync API Behaviour](#romm-save-sync-api-behaviour)
below.

### Outcomes

| Variant                       | Service behaviour                                                                                                                  |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `Skip(reason)`                | No I/O. Optional `adopt_baseline=True` flag: dispatcher writes `last_sync_hash := local_hash` (state mutation only, no network).   |
| `Upload(target_save_id=None)` | POST a new save to the slot. Server assigns an ID; we record it in state.                                                          |
| `Upload(target_save_id=int)`  | PUT to the existing save id (re-upload). Used when our offline edits need to land on the existing server save.                     |
| `Download(server_save)`       | GET save content, overwrite local file, update sync state.                                                                         |
| `Conflict(server_save)`       | Surface a `SyncConflict` to the frontend. The user resolves via `resolve_sync_conflict(rom_id, filename, server_save_id, action)`. |

`Skip(adopt_baseline=True)` is recorded both from the mutating sync path (`SyncEngine.do_sync_rom_saves`) and the
read-only status path (`StatusService._get_save_status_io`). The alternative — only writing the baseline from the
mutating path — would leave state incomplete forever for users who only ever open the game-detail panel.

### Implementation

The algorithm is `compute_sync_action` in `py_modules/domain/sync_action.py`. The `SaveService` aggregate
(`py_modules/services/saves/`) calls it from two sub-services:

- `SyncEngine.do_sync_rom_saves` (`services/saves/sync_engine/`) iterates local files and server-only-in-slot groups,
  dispatching each action via the matrix executor's `_dispatch_sync_action` (POST/PUT/GET + state update).
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
- **Content identity** — in the `never touched` branch, the server save's RomM-provided `content_hash` is compared to
  the local content hash first; a match short-circuits to row 6d before mtime/baseline are consulted.

| #  | local file | server in slot | our entry     | local vs baseline | mtime vs server      | decision                            | reason                                                                                                                          |
| -- | ---------- | -------------- | ------------- | ----------------- | -------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 1  | no         | none           | n/a           | n/a               | n/a                  | `Skip(nothing_to_sync)`             | nothing local, nothing server                                                                                                   |
| 2  | yes        | none           | n/a           | n/a               | n/a                  | `Upload(POST)`                      | first push for this save (or recovery after server-side wipe)                                                                   |
| 3  | no         | ≥1             | never touched | n/a               | n/a                  | `Download(picked)`                  | no relation, pull newest                                                                                                        |
| 4  | no         | ≥1             | current=true  | n/a               | n/a                  | `Download(picked)`                  | recovery — server still tracks our last version, local is gone                                                                  |
| 5  | no         | ≥1             | current=false | n/a               | n/a                  | `Download(picked)`                  | server moved forward, nothing local to protect                                                                                  |
| 6a | yes        | ≥1             | never touched | no baseline       | local mtime ≥ server | `Upload(POST)`                      | post our local as a new save in the slot — no overwrite risk                                                                    |
| 6b | yes        | ≥1             | never touched | no baseline       | local mtime < server | `Download(picked)`                  | server is newer than our untracked local                                                                                        |
| 6c | yes        | ≥1             | never touched | changed           | n/a                  | **`Conflict(picked)`**              | baseline held from a prior sync but the picked head is a save we never synced — both sides moved (#1059)                        |
| 6d | yes        | ≥1             | never touched | any               | any                  | `Skip(synced, adopt_baseline=true)` | `server.content_hash == local_hash` — byte-identical to an existing server save; adopt it, never POST a duplicate (#1013)       |
| 7  | yes        | ≥1             | current=true  | unchanged         | n/a                  | `Skip(synced)`                      | steady state                                                                                                                    |
| 8  | yes        | ≥1             | current=true  | no baseline       | n/a                  | `Skip(synced, adopt_baseline=true)` | trust server's `is_current=true`, write `last_sync_hash := local_hash` so future drift can be detected                          |
| 9  | yes        | ≥1             | current=true  | changed           | n/a                  | `Upload(PUT to picked.id)`          | offline edit (plausible size) — push our changes back onto the save the server still considers ours                             |
| 9b | yes        | ≥1             | current=true  | changed           | n/a                  | **`Conflict(picked)`**              | diverged local is 0-byte or shrunk past the baseline (crash / full disk) — refuse the in-place PUT, let the user decide (#1062) |
| 10 | yes        | ≥1             | current=false | unchanged         | n/a                  | `Download(picked)`                  | another device synced; we did nothing — adopt their version                                                                     |
| 11 | yes        | ≥1             | current=false | no baseline       | n/a                  | `Download(picked)`                  | no baseline → cannot prove our local is newer; server wins                                                                      |
| 12 | yes        | ≥1             | current=false | changed           | n/a                  | **`Conflict(picked)`**              | both sides changed independently — only true conflict                                                                           |

Conflict happens in three rows — #12 (we already hold an entry on the picked save), #6c (we hold a baseline from a prior
sync but no entry on the picked head), and #9b (we own the picked save and our local diverged, but the local file is
0-byte / implausibly shrunk). #12 and #6c are the same "both sides moved to content we never synced" situation; #9b is a
different hazard — protecting the server's only good copy from being overwritten in place by a corrupt-looking local
file (#1062). Every other row resolves silently to a Skip, Upload, or Download.

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

### Why row 9b conflicts instead of PUTting in place

Row 9 is the steady offline-edit path: we own the picked save (`is_current=true`), our local diverged from the baseline,
so we PUT the local content onto the existing save id. Row 9b is the same branch with one extra guard. A crashed
emulator or a full disk can leave a **0-byte or truncated** save on disk — still a valid regular file, with a
valid-but-wrong content hash, so it reads as a "diverged" edit and would take the row 9 PUT. But RomM's
`PUT /api/saves/{id}` updates the save **in place** and creates a version only on **POST**, never on PUT — so that PUT
would overwrite the only good server copy with the corrupt bytes and leave **no recoverable version**. This is the
upload mirror of the [#965](https://github.com/danielcopper/decky-romm-sync/issues/965) backup-or-confirm invariant: the
download-overwrite path already quarantines the local file into `.romm-backup` first, but the upload-overwrite PUT had
no equivalent guard.

The plausibility check is pure (`domain/save_size.is_implausibly_shrunken`, fed `local_file.size` and the recorded
`last_sync_local_size` baseline): a new size of **0** fires unconditionally, and a non-empty new size below **50%** of
the recorded baseline fires as a shrink. The threshold is a hard-coded conservative default — not a setting. When the
guard fires, the kernel returns `Conflict(picked)` instead of `Upload(PUT)`, routing through the existing
`SyncConflictModal` so the user decides: **Use Server** downloads the good server copy (quarantining the bad local
first), **Keep Local** re-PUTs the corrupt file only after an explicit choice. A plausible-size divergent edit (or a
save that grew) is unaffected and still PUTs in place (row 9).

### Why row 11 downloads instead of uploading

Row 11 looks superficially symmetrical to row 6a — local file exists, mtime is whatever, no baseline. The difference is
that our device **does** have an entry on the picked save (we touched it before) and the entry says `is_current=false`.
Some other device has PUT to that save since our last interaction, so its content is foreign to us. Without a baseline,
we cannot prove our local has edits that postdate the foreign PUT. mtime is unreliable (filesystem touches, migrations,
clock skew). Pushing a PUT here would overwrite the foreign content blindly. We download instead, accepting the
trade-off that a state-corrupted-but-genuinely-newer local file gets overwritten — that scenario is rare and a silent
overwrite of another device's work would be worse.

### Why is there no foreign-save modal anymore

Earlier versions surfaced every server save in the slot the user had not authored as a "newer-in-slot" prompt. The
pragmatic newest-wins model used by the official RomM clients (Argosy, Grout) treats the slot as a single timeline:
whichever save has the highest `updated_at` wins, regardless of which device PUT it. We adopted that model in v0.16
because it eliminates ~1500 lines of foreign-tracking code and aligns with the wider RomM ecosystem. Cross-device
uploads are silently adopted unless local edits diverge from baseline (row 12). This is documented behaviour, not a
regression.

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
4.8.1, all features (device sync, version history, slot deletion, device management) are unconditionally available. The
frontend no longer fetches or checks capability flags.

## Conflict Resolution

A `Conflict` outcome from `compute_sync_action` (matrix rows 12, 6c, and 9b) is the only surface that shows a modal. The
common case fires when the local file has diverged from the recorded baseline (`local_hash != last_sync_hash`) while the
server moved to content we never synced — either on a save we already have an entry for
(`device_syncs[me].is_current=false`, row 12) or on a new head we have no entry for (row 6c). Both sides have unsynced
changes that cannot be silently merged. Row 9b is the corrupt-local guard: we own the picked save and our local
diverged, but the local file is 0-byte or implausibly shrunk, so the in-place PUT is refused and the user resolves it
instead of the only good server copy being overwritten (#1062).

### The modal

`SyncConflictModal` (`src/components/SyncConflictModal.tsx`) shows the local-save row and the picked server-save row
side by side, each with size and timestamp. Three actions:

- **Keep Local** → `resolveSyncConflict(rom_id, filename, "keep_local")` → backend PUTs local content onto the picked
  server save.
- **Use Server** → `resolveSyncConflict(rom_id, filename, "use_server")` → backend downloads the picked server save and
  overwrites local.
- **Cancel** → pure UI close, no callable, no state mutation. The conflict re-fires on the next sync as long as the
  underlying state still produces matrix row 12, 6c, or 9b.

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
     without re-uploading. Otherwise the local file is PUT to the picked save id, then `confirm_download` registers our
     device as `is_current=true`.
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

The realistic race the lock prevents: user clicks Keep Local → executor runs PUT + state mutation → in parallel,
`post_exit_sync` for a game that just stopped runs and mutates the same per-file state → last-writer-wins on the
`rom_save_states` aggregate, dropping one set of fields. The same lost-update window applies to `get_save_status`'s
baseline-adopt write versus a concurrent pre-launch / post-exit / manual sync. The lock makes each
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
| `Upload(POST/PUT)`                           | Local changes are silently pushed to the server first. Switch proceeds.                                                                                                                                     |
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
   PUT against the target save id with byte-identical content. RomM v4.8.1 fires the SQLAlchemy `onupdate=utc_now` hook
   on every PUT regardless of whether the content changed, so `save.updated_at` becomes NOW. The target save is now
   newest in the slot.
3. **Confirm download**: `_do_upload_save` also calls `confirm_download(target_save_id, device_id)`. RomM v4.8.1's PUT
   does **not** auto-upsert the calling device's `device_save_sync` row, so without this call our `is_current` would
   evaluate `false` immediately after our own PUT. The dedicated `/api/saves/{id}/downloaded` endpoint upserts
   `last_synced_at = save.updated_at` so the computed `is_current` flips back to `true` for us.
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
`sync.last_synced_at > save.updated_at` (strict greater-than — equality counts as not-current). There is no column to
set; you can only push the components.

### `GET /api/saves` upserts `device_syncs` for the queried device

Hardware-verified on RomM 4.8.1: `GET /api/saves?rom_id=X&device_id=Y` upserts a `device_save_sync` row for device Y on
every save returned that did not already have one. The `optimistic` query flag does not appear to prevent the upsert.
The upserted row has `last_synced_at = save.updated_at`, which under the strict-`>` formula evaluates to
`is_current = false` — i.e. the row is created in a "not current yet" state.

This has a concrete consequence for the sync algorithm: the "no entry for our device on the picked save" branch of
`compute_sync_action` (matrix rows 6a/6b) is unreachable in real plugin operation, because
`SyncEngine.do_sync_rom_saves` always calls `list_saves` (which triggers the upsert) before passing the data to the
algorithm. By the time the algorithm runs, our device entry exists on every server save. The branch is retained as
defensive code and is exercised by unit tests in `tests/domain/test_sync_action.py`.

### PUT bumps `updated_at`, not the calling device's sync row

`PUT /api/saves/{id}` triggers SQLAlchemy's `onupdate=utc_now` hook on every PUT, so `save.updated_at` becomes the
server's NOW even if the content is byte-identical. **It does not** upsert the calling device's
`device_save_sync.last_synced_at`. The computed `is_current` flag therefore flips to `false` for the calling device
immediately after the PUT response is observed (because `save.updated_at > sync.last_synced_at` for everyone, including
us).

To restore `is_current=true` for our device after a PUT, we must explicitly call `POST /api/saves/{id}/downloaded`,
which upserts `last_synced_at = save.updated_at`. `_do_upload_save` does this unconditionally after every successful
POST or PUT (best-effort — failures are logged at debug and don't fail the upload).

### GET `/content?optimistic=true` auto-upserts the sync row

`GET /api/saves/{id}/content?device_id=X&optimistic=true` (default `true`) is the canonical download endpoint. It
auto-upserts `device_save_sync` for the calling device with `last_synced_at = save.updated_at`. After a successful
download our `is_current` evaluates `true` without an extra round-trip.

`download_save_content` in `adapters/romm/romm_api.py` always passes `device_id` and `optimistic=true`. The
non-optimistic legacy `download_save(save_id, dest_path)` is retained for use cases that must not touch sync state but
is not used by the sync flow.

### Implication for the sync algorithm

Because `is_current` is computed and the only ways to make it `true` are PUT/POST followed by `confirm_download`, or a
`GET /content?optimistic=true`, the algorithm can trust `is_current` as authoritative without further hashing. Row 8 in
the matrix (no baseline yet, `is_current=true`, local exists) is the canonical adopt-baseline case: we believe the
server's claim and write `last_sync_hash := local_hash` so future runs can detect drift.

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
   one typed payload (the old `recordSessionEnd` / `postExitSync` frontend callables were collapsed into it).
3. Backend runs `do_sync_rom_saves`. For most rows the local file's hash will differ from `last_sync_hash` (the user
   just played), so the typical action is `Upload(PUT to picked.id)` — matrix row 9.
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

Playtime is tracked per-ROM in the SQLite `rom_playtime` table (the `Playtime` aggregate), independent of the `saves`
lifecycle. Uninstalling a ROM deletes only its files and `rom_installs` row, leaving playtime and saves intact per
[ADR-0007](../adr/0007-rom-retention-identity-anchor.md).

Session tracking:

1. `recordSessionStart(romId)`: backend opens the session marker (`last_session_start`) on the ROM's `rom_playtime` row
   in a short write Unit of Work
2. During play, the frontend `sessionManager` accumulates device-suspend wall-clock across suspend/resume cycles (via
   `RegisterForOnSuspendRequest` / `RegisterForOnResumeFromSuspend`; an in-flight suspend still open at game-stop is
   folded in even without a resume event), and passes the rounded `suspended_seconds` to `finalizeGameSession` at
   session end
3. Session end (`finalizeGameSession(romId, suspendedSeconds)` → backend `record_session_end`): in an executor worker, a
   short write UoW folds the closed session into the aggregate (`record_session` subtracts `suspended_seconds` from the
   raw elapsed span, then clamps the result to 0–24h — subtraction before the cap, never negative — increments
   `total_seconds` and `session_count`, records `last_session_duration_sec`); then, outside the transaction, the merged
   total is pushed to RomM via user notes (best-effort)

### Steam display

Steam natively tracks playtime for non-Steam shortcuts. No additional work is needed — Steam's built-in tracking handles
the display in the library.

### RomM last_played

After each play session, the backend updates the ROM's `last_played` timestamp on the RomM server. This keeps the RomM
library sorted correctly by recent activity. When a RomM playtime API becomes available in the future, the locally
accumulated `playtime_seconds` can be synced to RomM as well.

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

| Field                                               | Type                   | Description                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `saves`                                             | object                 | Per-ROM sync metadata, keyed by `rom_id` (string)                                                                                                                                                                                                                                                                                                                                                                                       |
| `saves.<id>.system`                                 | string                 | RetroDECK system slug (e.g. `"gba"`, `"snes"`)                                                                                                                                                                                                                                                                                                                                                                                          |
| `saves.<id>.emulator`                               | string                 | Emulator tag (default `"retroarch"`); forms the RomM save-folder path `saves/{system}/{rom_id}/{emulator}/`.                                                                                                                                                                                                                                                                                                                            |
| `saves.<id>.active_slot`                            | string                 | Which RomM slot this game syncs to (e.g. `"default"`)                                                                                                                                                                                                                                                                                                                                                                                   |
| `saves.<id>.slot_confirmed`                         | boolean                | Whether user has explicitly chosen their slot (see "Slot Setup Wizard")                                                                                                                                                                                                                                                                                                                                                                 |
| `saves.<id>.last_synced_core`                       | string / null          | RetroArch core used at last sync (for core change detection, e.g. `"mgba_libretro"`)                                                                                                                                                                                                                                                                                                                                                    |
| `saves.<id>.own_upload_ids`                         | array of integer       | Save ids this device originally POSTed. Drives the `uploaded_by_us` indicator on the SAVES tab.                                                                                                                                                                                                                                                                                                                                         |
| `saves.<id>.slots`                                  | object                 | Merged slot listing (read-model cache): per slot, its `source` / `count` / latest `updated_at`.                                                                                                                                                                                                                                                                                                                                         |
| `saves.<id>.last_sync_check_at`                     | ISO-8601 string / null | Timestamp of the most recent `do_sync_rom_saves` run for this rom (regardless of whether files transferred).                                                                                                                                                                                                                                                                                                                            |
| `saves.<id>.files`                                  | object                 | Per-file sync state, keyed by filename (e.g. `"game.srm"`)                                                                                                                                                                                                                                                                                                                                                                              |
| `saves.<id>.files.<fn>.tracked_save_id`             | integer / null         | Most recent RomM save id this device tracked. Used to exclude the active save from the Previous Versions dropdown and as an uploader-attribution hint; **not** consulted by `compute_sync_action` (the algorithm picks newest by `updated_at`).                                                                                                                                                                                         |
| `saves.<id>.files.<fn>.last_sync_hash`              | MD5 hex string         | Hash of the save file at last sync. Drift baseline used by matrix rows 7/8/9/10/11/12.                                                                                                                                                                                                                                                                                                                                                  |
| `saves.<id>.files.<fn>.last_sync_at`                | ISO-8601 string        | Timestamp of last successful sync.                                                                                                                                                                                                                                                                                                                                                                                                      |
| `saves.<id>.files.<fn>.last_sync_server_updated_at` | ISO-8601 string        | Server's `updated_at` at last sync.                                                                                                                                                                                                                                                                                                                                                                                                     |
| `saves.<id>.files.<fn>.last_sync_server_save_id`    | integer                | RomM save id for the most recently synced server save.                                                                                                                                                                                                                                                                                                                                                                                  |
| `saves.<id>.files.<fn>.last_sync_server_size`       | integer                | Server file size at last sync.                                                                                                                                                                                                                                                                                                                                                                                                          |
| `saves.<id>.files.<fn>.last_sync_local_mtime`       | float                  | Local file mtime (epoch seconds) at last sync.                                                                                                                                                                                                                                                                                                                                                                                          |
| `saves.<id>.files.<fn>.last_sync_local_size`        | integer                | Local file size (bytes) at last sync.                                                                                                                                                                                                                                                                                                                                                                                                   |
| `playtime`                                          | object                 | Per-ROM playtime lives in the `rom_playtime` table (the `Playtime` aggregate), a separate aggregate from saves — `RomRemovalService` keeps playtime on uninstall per [ADR-0007](../adr/0007-rom-retention-identity-anchor.md). The fields below (`total_seconds`, `session_count`, `last_session_start`, `last_session_duration_sec`, `note_id`) are its columns, keyed by `rom_id`. See [Playtime Tracking](#playtime-tracking) above. |
| `playtime.<id>.total_seconds`                       | integer                | Accumulated playtime in seconds.                                                                                                                                                                                                                                                                                                                                                                                                        |
| `playtime.<id>.session_count`                       | integer                | Number of completed play sessions.                                                                                                                                                                                                                                                                                                                                                                                                      |
| `playtime.<id>.last_session_start`                  | ISO-8601 / null        | Start time of current session (null when not playing).                                                                                                                                                                                                                                                                                                                                                                                  |
| `playtime.<id>.last_session_duration_sec`           | integer / null         | Duration of last completed session.                                                                                                                                                                                                                                                                                                                                                                                                     |
| `playtime.<id>.note_id`                             | integer / null         | Cached RomM note ID for playtime storage (avoids ROM detail fetch).                                                                                                                                                                                                                                                                                                                                                                     |

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
next sync as long as the underlying state still produces matrix row 12.

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

### Router.MainRunningApp

After a game starts, there is a brief window where the app ID may not be fully resolved. The session manager waits 500ms
and then reads `Router.MainRunningApp` for a reliable `appid` and `display_name`. Falls back to `unAppID` from the
notification if `MainRunningApp` is null.

### App ID to ROM ID mapping

The session manager maintains a cached `appId -> romId` map loaded from the backend's synced-ROM registry (the `roms`
SQLite table, via `get_app_id_rom_id_map`). This map is refreshed:

- On session manager initialization (plugin load)
- Before each game start event (in case a sync added new shortcuts)

If the launched app ID is not in the map, it is not a RomM shortcut and the session manager ignores it.

### Suspend/resume handling

To exclude sleep time from playtime tracking:

- `SteamClient.System.RegisterForOnSuspendRequest` — records the suspend timestamp
- `SteamClient.System.RegisterForOnResumeFromSuspend` — calculates paused duration and subtracts it from the session

## RomM Notes API Bug and Workaround

> **Historical context:** This bug affects RomM 4.6.1. RomM 4.7.0+ fixes the underlying issue. The workaround is
> retained because the `all_user_notes` approach remains the plugin's primary read path regardless.

### The bug

`GET /api/roms/{id}/notes` returns HTTP 500 Internal Server Error in RomM 4.6.1 whenever any note exists for a ROM. POST
(create), PUT (update), and DELETE all work correctly — only the GET list endpoint is broken.

This bug is in the `get_rom_notes()` handler in RomM's `backend/endpoints/rom.py`. The function calls
`db_rom_handler.get_rom_notes()` which uses `json_array_contains_value()` for tag filtering — this utility appears to
fail depending on the database driver or JSON column format.

### The workaround

`GET /api/roms/{id}` (the ROM detail endpoint) returns the full `DetailedRomSchema` which includes an `all_user_notes`
array of `UserNoteSchema` objects. This completely bypasses the broken notes list endpoint.

Each note in `all_user_notes` contains:

- `id` — note ID (needed for PUT updates and DELETE)
- `title` — note title
- `content` — note body (we store JSON here)
- `is_public` — visibility flag
- `tags` — array of strings (do **not** send when creating notes — contributes to GET bug)
- `created_at`, `updated_at` — timestamps
- `user_id`, `username` — note author

### How the plugin uses this

The plugin stores playtime data in RomM notes (since RomM has no dedicated playtime API). The workflow:

1. **Read**: Fetch `GET /api/roms/{id}`, filter `all_user_notes` for notes with `title == "romm-sync:playtime"`
2. **Create**: `POST /api/roms/{id}/notes` with `title: "romm-sync:playtime"`, JSON content, `is_public: false`. Do
   **not** send `tags` — it contributes to the GET bug.
3. **Update**: `PUT /api/roms/{id}/notes/{note_id}` with updated playtime JSON
4. **Delete**: `DELETE /api/roms/{id}/notes/{note_id}` if needed

The note `id` is recorded on the `rom_playtime` row (`note_id`) when a note is first created. The session-end push
re-reads `all_user_notes` each time to merge against the current server total, so it does not currently rely on the
cached id to skip the fetch.

**Reconcile-on-view (pull-only).** Opening a game's detail page triggers `reconcile_playtime(rom_id)`: the plugin reads
`all_user_notes` from the ROM detail, finds the `title == "romm-sync:playtime"` note, and folds its `seconds` total into
the local `rom_playtime` row via `reconcile_total` (a `max`-merge that never regresses the local total). This is
**pull-only and total-only** — it catches the local row up to a server record that moved ahead on another device, links
the note `id` if it wasn't cached yet, and never writes a note (the push stays at session end). If no note exists it
returns the local total without seeding an empty row; a server-unreachable fetch degrades to the local total with a
`server_query_failed` flag.

### Future: RomM playtime API

**Feature request #1225** (dedicated playtime API) is still open. Until it ships, playtime continues to use notes-based
storage.

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

### No RomM playtime API

RomM currently supports `last_played` timestamps but does not have a dedicated playtime tracking API (feature request
#1225 is open). The plugin stores playtime in RomM user notes (see "RomM Notes API Bug and Workaround" above) and
updates `last_played` on the server after each session. When a RomM playtime API becomes available, the plugin can
migrate from notes-based storage to the native API.

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
