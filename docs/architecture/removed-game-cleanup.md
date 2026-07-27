# Removed-game cleanup

When RomM stops serving a ROM id, the plugin keeps the local row. Nothing is ever deleted automatically — a library sync
that no longer sees an id retains everything it already has. Removing that local state is an explicit, separately
confirmed operation, and this page owns how it stays safe.

The five prune invariants in the repo's `CLAUDE.md` register are one-clause statements of the rules below; this page is
where their detail lives. Save-side path resolution and quarantine mechanics belong to
[save-file-sync-architecture.md](save-file-sync-architecture.md); shortcut identity and artwork belong to
[steam-non-steam-shortcuts.md](steam-non-steam-shortcuts.md).

## Where the code lives

| Module                         | Responsibility                                                          |
| ------------------------------ | ----------------------------------------------------------------------- |
| `services/prune/service.py`    | Callable facade and the ephemeral per-run state                         |
| `services/prune/preview.py`    | Builds the candidate preview (sizes, groups, warnings)                  |
| `services/prune/registry.py`   | Discovers candidates from local rows and fetch generations              |
| `services/prune/recovery.py`   | Sequences bundle creation and sealing                                   |
| `services/prune/executor.py`   | Runs a confirmed group: proofs, Steam actions, mutation, cascade        |
| `services/prune/results.py`    | Shapes progress/completion frames and terminal group results            |
| `services/prune/requests.py`   | Parses and validates the wire payloads                                  |
| `lib/prune_gate.py`            | The admission gate — reservations, conflicting-callable refusal, leases |
| `adapters/recovery_bundle.py`  | Writes, checksums, seals and publishes a recovery bundle                |
| `adapters/steam_recovery.py`   | Captures Steam-only state; edits the controller value in `localconfig`  |
| `adapters/descriptor_paths.py` | Descriptor-relative, no-follow claim capture and claimed mutation       |

## Deletion authority

Only a `RommNotFoundError` from a fresh, single-attempt, exact-id request authorizes deleting anything. A live response,
a wrong or malformed payload, a timeout, a transport or auth failure, a 5xx, a cancellation, or an unknown exception all
retain local data. Liveness is proven before work begins, again around Steam actions, and once more immediately before
local finalization, so a source that comes back to life — or a replacement that disappears — stops that group.

A dropped ROM does **not** 404 the saves endpoint; `GET /api/saves?rom_id=<dead>` answers `200 []`. Only `get_rom`
distinguishes a dead id, so every liveness probe goes through it.

### Server namespace binding

A run pins the RomM namespace it discovered its candidates under: canonical server origin, token origin, and RomM user
id. A response that arrives after any of those changed can never authorize deleting a row discovered under the earlier
namespace — a namespace change is uncertainty, not a 404.

### Discovery

Bulk discovery from the Danger Zone is generation-gated: a platform with no known completed fetch produces no candidates
at all, because absence from an incomplete fetch is not evidence of removal. Inline removal from an already-vanished
version-picker row needs no generation — the row is already known vanished — but still takes the same fresh exact-id
proof.

Cleanup deliberately does **not** clear the platform's `platform_sync_state` completion stamp. It does not need to: a
server that dropped ids also reports a different `rom_count`, and the fetcher's existing stamp-count guard already
forces the re-fetch. Clearing the stamp would cost the platform its incremental skip and disable further bulk discovery
until a new complete fetch landed.

## Admission and conflicting operations

A prune claim excludes library sync, downloads and resumes, migrations, version switches, core/disc/controller writes,
launch evaluation, save mutations, session writes, uninstalls, connection identity changes, and affected cache cleanup.
Each conflicting callable registers its own activity before its first `await`, and detached work retains that
registration for the task's lifetime.

Admission is atomic in the part that matters: `prune_exclusive_start` takes the gate lock, refuses if any conflicting
callable is registered, and reserves the prune claim — all in one lock hold, so no registration can slip between the
check and the reservation. The run then proceeds **without** the lock. The reservation alone already refuses every
conflicting callable, and holding the lock across the run's preview rebuild would make Play, save status, and downloads
wait for that rebuild rather than learning their verdict immediately.

Frontend-owned Steam work spans many calls, so it holds a globally registered, bounded, tokenized lease that it
heartbeats through every sibling continuation's final write — including each paced `sync_stale` removal and the terminal
repoint publication. Every continuation re-checks its abort signal before each later Steam mutation. Failed event
delivery releases an unreachable token. The owner's plugin generation is captured before each backend wait, and teardown
tombstones it so a late lease-bearing response is released without doing work; only a genuine remount opens a new
generation. Teardown stops renewal and blocks future writes but defers the explicit release until already-started Steam
promises settle.

## Actions and frames

A frontend action mutates Steam only after atomically claiming its exact run, token, discriminant, and binding.
Identical repeat claims are idempotent, action delivery is serialized and deduplicated, and a completion retry never
repeats the Steam operation. An outcome that was claimed but lost in transit is reported as **ambiguous** — never as
success — and the local rows are retained for reconciliation against live Steam absence.

Every action, progress and completion frame carries the preview ID it originated from. A pending frontend preview may
adopt only a matching run — including the case where the run started but its success response was lost — and foreign,
stale, duplicate or post-terminal frames trigger no state change and no side effects. Completion finalizes only from a
contiguous chunk set, and an accepted terminal result seals the run against every later frame. Payloads are tokenized
and byte-bounded; warnings distinguish entries that were omitted from text that was merely shortened, and stay visible
even on an otherwise successful run.

If a completion chunk set never completes, the frontend does not stay wedged: a staleness timeout clears the stalled
progress, surfaces a "result was lost" warning, and re-enables the entry point so the user can re-scan.

## Filesystem safety

Every prune source carries a claim before it is touched: the root's device, inode, mount id, mode, size, mtime and
ctime, every descendant's identity, and every regular file's hash. Mutation is descriptor-relative and no-follow
throughout, and a nested mount transition — including a same-device bind mount — fails closed. A path re-lookup alone
never authorizes a delete or a quarantine.

- Regular-file and controller-claim deletion holds kernel writer exclusion from final validation through the unlink; if
  exclusion cannot be established the source is retained. A writer-exclusion teardown fault is ambiguity, not success.
- Selected sources consume claims decoded from the same held, digest-bound sealed bundle. Unselected and recovery-off
  sources take a final presence-or-absence claim instead.
- Every exclusive save is expected absent after quarantine, and the whole set is rechecked collectively immediately
  before the aggregate cascade — a save an emulator recreated in between stops the deletion.
- Quarantine publication is atomic no-replace, so a concurrently created `.romm-backup` destination is never
  overwritten.
- Controller-claim branches revalidate and preserve a newer held-inode edit at a surfaced path.
- Unsafe recovery-failure cleanup preserves the anchored staging path and reports it.

Steam's `localconfig.vdf` is parsed with the duplicate-key-preserving mapper and only the one relevant per-app
controller value is touched. Valve's format permits duplicate keys, and a plain-dict round-trip would silently collapse
unrelated user data on rewrite. Cleanup never edits `shortcuts.vdf`.

Sizing is not hashing: the preview measures installed content with a size-only, descriptor-relative, no-follow
traversal. Hashing a multi-gigabyte ROM tree to fill in a preview row — before the user has confirmed anything — would
stall the Danger Zone for minutes. A path whose size cannot be measured reports `installed_bytes: None`, carries a
warning, and cannot be selected for recovery.

## Recovery

Recovery is a temporary per-run choice, not a persisted setting. The confirmation dialog offers **Create recovery
bundle** (default on), and **Include installed ROM content** per candidate (default off). The dialog shows recursive
size per ROM alongside required and free space, and blocks confirmation when space is insufficient.

The root is `~/<package-name>-recovery`, with the package name taken from `package.json` through the canonical metadata
adapter and path-sanitized (today: `~/decky-romm-sync-recovery`). Reading free space must not create that layout — a
read-only preview stats the nearest existing parent, and the directories appear only when a bundle is actually written.

A bundle records the complete pre-cascade state in lossless JSON: the ROM aggregate, install and metadata state,
save-sync baselines and files, playtime including pending sessions, completion stamps, plugin artifacts, and applicable
Steam-only state. It also writes a human-readable README and a `playtime.txt` with exact seconds. Every exact
attributable current save is copied and checksum-verified, and existing `.romm-backup` history is copied in while
remaining at its original location. Bundles are sealed, checksum-verified, descriptor-bound, and published atomically
under `bundles/`.

For a fully vanished game whose shortcut will be removed, an enabled bundle also captures Steam artwork and per-app
Steam Input files, the appId, name, executable, start directory, launch options, collection membership, available Steam
playtime, and the relevant controller-config value. No artwork base64 crosses the Decky bridge.

Failure is never rewritten into success. Seal, rename and cleanup durability failures surface as exact or ambiguous
partial mutations. If a bundle cannot be proven durable it is renamed aside rather than published; if that rename also
fails, the reported message names what actually remains on disk rather than claiming a preservation that did not happen.

There is no automatic restore. A bundle is machine-readable and documented for future or manual recovery.

### Saves

Exclusive current saves leave the emulator directory only through the existing `.romm-backup` quarantine funnel. A save
path shared with any remaining local version — installed **or not** — is copied into the bundle but never removed.
Unknown or unsafe-to-resolve save locations are recorded, left untouched, and do not block pruning the row.

Save **states** are entirely untouched and remain unsupported. Extending save-state support must extend this recovery
contract before purge is allowed to remove state files.

A bound vanished row with unsynced saves may bypass the normal save-stranding guard only after its recovery bundle
sealed successfully. With recovery disabled or failed, the group is skipped instead.

## Steam-side outcomes

Repointing a bound vanished shortcut preserves the shortcut's appId, collections, Steam-side playtime, and identity, and
confirm-writes the exact new launch command. The replacement chosen is exactly the version picker's natural live
Default. A group with multiple bound shortcuts is skipped unchanged.

Removing a fully vanished game's shortcut requires a claimed action and live Steam confirmation. An attempted but
unconfirmed removal is reported as ambiguous and its local rows are retained for reconciliation. A fully vanished
group's explicit action removes its confirmed shortcut and every confirmed-404 row and file, regardless of the
individual-row toggle.

## Run outcomes

Groups execute serially, and an ordinary group failure does not stop unrelated groups. Recovery, path, disk-space,
checksum, liveness, or Steam-acknowledgement failure skips the affected group **before** any destructive state deletion.
Cancellation stops every group that has not started. Terminal results distinguish exact success, skipped work, known
partial mutation, and ambiguous mutation, and the removed ids and affected appIds stay truthful even after cancellation
or a failed event delivery.
