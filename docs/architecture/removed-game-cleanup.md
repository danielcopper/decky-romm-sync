# Removed-game cleanup

When RomM stops serving a ROM id, the plugin keeps the local row. Nothing is ever deleted automatically — a library sync
that no longer sees an id retains everything it already has. Removing that local state is an explicit, separately
confirmed operation, and this page owns how it stays safe.

The five prune invariants in the repo's `CLAUDE.md` register are one-clause statements of the rules below; this page is
where their detail lives. Save-side path resolution and quarantine mechanics belong to
[save-file-sync-architecture.md](save-file-sync-architecture.md); shortcut identity and artwork belong to
[steam-non-steam-shortcuts.md](steam-non-steam-shortcuts.md).

## Where the code lives

| Module                            | Responsibility                                                          |
| --------------------------------- | ----------------------------------------------------------------------- |
| `services/prune/service.py`       | Callable facade and the ephemeral per-run state                         |
| `services/prune/preview.py`       | Builds the candidate preview (sizes, groups, warnings)                  |
| `services/prune/registry.py`      | Discovers candidates from local rows and fetch generations              |
| `services/prune/recovery.py`      | Sequences bundle creation and sealing                                   |
| `services/prune/executor.py`      | Sequences a confirmed group's phases and arms its recovery bundle       |
| `services/prune/planning.py`      | Decides what a group would do, or the reason it is refused              |
| `services/prune/liveness.py`      | Namespace-bound exact-ID proof — the only deletion authority            |
| `services/prune/steam_actions.py` | Requests the frontend's Steam mutations and reads their outcome         |
| `services/prune/finalize.py`      | Revalidates every proof, then runs the irreversible cascade             |
| `services/prune/save_locks.py`    | Holds save locks over an ownership set proven stable under them         |
| `services/prune/results.py`       | Shapes progress/completion frames and terminal group results            |
| `services/prune/requests.py`      | Parses and validates the wire payloads                                  |
| `lib/prune_gate.py`               | The admission gate — reservations, conflicting-callable refusal, leases |
| `adapters/recovery_bundle.py`     | Writes, checksums, seals and publishes a recovery bundle                |
| `adapters/steam_recovery.py`      | Captures Steam-only state; edits the controller value in `localconfig`  |
| `adapters/descriptor_paths.py`    | Descriptor-relative, no-follow claim capture and claimed mutation       |

## Deletion authority

Only a `RommNotFoundError` from a fresh, single-attempt, exact-id request authorizes deleting anything. A live response,
a wrong or malformed payload, a timeout, a transport or auth failure, a 5xx, a cancellation, or an unknown exception all
retain local data. Liveness is proven before work begins, again around Steam actions, and once more immediately before
local finalization, so a source that comes back to life — or a replacement that disappears — stops that group.

A dropped ROM does **not** 404 the saves endpoint; `GET /api/saves?rom_id=<dead>` answers `200 []`. Only `get_rom`
distinguishes a dead id, so every liveness probe goes through it.

### The 404 has to come from RomM

A 404 is only evidence that a ROM is gone if the thing answering is RomM and the request reached the ROM route. RomM is
FastAPI, so a reverse proxy with a wrong path prefix makes **RomM itself** answer a clean JSON 404
(`{"detail": "Not Found"}`) for every id — including ids that plainly exist. No body-shape check distinguishes that
route-404 from an entity-404, and one request at a time neither can a human. Believed, it is also amplified: the run
probes a group's **live** siblings too, so a misroute turns "one version vanished, repoint to the other" into "the whole
game is gone", taking the Steam shortcut and any unselected installed content with it.

So no 404 is honoured unless the same round holds positive proof, recorded as the tier that supplied it:

| Tier          | What it is                                                                     |
| ------------- | ------------------------------------------------------------------------------ |
| `still_there` | A probed ROM answered 200 with its own id — the round already proved the route |
| `canary_rom`  | A known-live id answered 200 with its own id                                   |
| `canary_user` | No known-live id exists; the authenticated user matched the pinned namespace   |
| `none`        | Nothing vouched — every 404 in the round is unconfirmed and nothing is removed |

**`still_there` is free.** Any 200-carrying-the-right-id came from the very route whose 404s are in question, so groups
with a live member — every repoint case — pay nothing.

**`canary_rom` is the dedicated check**, and it deliberately rides the same route family rather than a health or version
endpoint, which a path misroute leaves working. Its subjects are ids the last complete fetch is recorded as returning:
the best available liveness prior, and by construction never candidates. One 200-with-its-own-id proves route, auth and
server at once. At most two are asked — a retry, because the first can genuinely have vanished since the sync, and no
more, because this is a check rather than a survey.

**`canary_user` is the weaker fallback**, reached only when the library holds no known-live id at all (an essentially
empty library). It shows the server is RomM and the token still belongs to the pinned user, but says nothing about the
ROM route, which is why it is last and why the tier is written down.

**A control that 404s is a proof failure, not a licence to keep looking.** Every 404 in the round becomes `uncertain`
with reason `unconfirmed_server`, the group is skipped, and the message says the server's answers could not be confirmed
— the same outcome whether the route is broken or the control genuinely vanished, because from here those are the same
observation.

Proof is per round and never carried over, for the same reason the re-proof rounds exist at all: a route can start
misbehaving between them. The tier is logged with the run id, so which authority a run acted on is readable after the
fact rather than inferred from what survived.

This is a precondition on top of the 404 rule, never a softening of it: deletion authority is still, only, a fresh
single-attempt exact-id 404 under the pinned namespace.

**What it does not cover.** The control and the candidate are different ids by construction, so a _per-id_ misroute —
one that answers correctly for the control and a bogus 404 for the candidate — passes every check here. Nothing
client-side can close that: a client cannot distinguish "this id is gone" from "this id, specifically, was misrouted"
without a second source of truth about that id. What the ROM tiers rule out is the whole-route failure, which is the
shape a proxy misconfiguration actually takes.

**How this composes with the adapter-side 404 discrimination** (#1622): the two run in series and are not redundant. The
adapter filters non-entity 404 **shapes** at the transport boundary, so a 404 that does not look like RomM answering
about an entity never reaches this layer as a `RommNotFoundError`. The canary demands **endpoint proof** per round,
regardless of shape. A misroute that answers an entity-shaped JSON 404 passes the adapter untouched, so the canary
remains the sole defence against it.

### Server namespace binding

A run pins the RomM namespace it discovered its candidates under: canonical server origin, token origin, and RomM user
id. A response that arrives after any of those changed can never authorize deleting a row discovered under the earlier
namespace — a namespace change is uncertainty, not a 404.

### Discovery

Bulk discovery from the Danger Zone is generation-gated: a platform with no known completed fetch produces no candidates
at all, because absence from an incomplete fetch is not evidence of removal. Inline removal from an already-vanished
version-picker row needs no generation — the row is already known vanished — but still takes the same fresh exact-id
proof.

The preview discloses **every** member of an affected group, not only the candidates. A member carrying the platform
stamp's current fetch generation is not evidence that RomM still serves it: whole-game removal is decided by the run's
fresh probe of every id in the group, never by the stored generation, so a generation-current row can still be taken and
may never be deleted unseen. What the generation does establish is that the row was there at the last completed fetch,
so the projection separates the two classes rather than flattening them — candidates sort first, the page carries a
`candidate_total` beside `total`, and the dialog leads with the candidate count and labels the rest as retained.

The wire always carries both classes; which ones are **rendered** is a frontend decision keyed on the whole-game removal
option, because `selected_prune_ids` returns a non-candidate only under that option. With it off, the retained rows
describe an outcome that cannot occur, so the dialog hides them and drops any installed-content selection they carried;
every page is still fetched, and the completeness gate before confirmation is unchanged.

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

Every claim on the gate is **named**. A callable registration carries the callable's own name, detached work carries the
name of the callable that spawned it, and a lease carries its acquisition key plus the time it was taken. A refusal logs
the complete holder inventory at INFO — label, kind, age, and a lease's remaining time — and names the holder in the
refused message itself when its key has a user-facing name, falling back to the generic text rather than putting an
internal token in front of the user. Acquire, renew and release are logged at debug; a lease that reaches its deadline
is logged at INFO instead, because an expiry means its owner never released it. Without this a blocked cleanup is
indistinguishable from a plugin that has stopped responding, and the holder cannot be identified after the fact.

The live-claim count is derived from the holder registry rather than tracked beside it: a counter that can drift from
the registry is exactly what made an unexplained refusal unattributable.

Frontend-owned Steam work spans many calls, so it holds a globally registered, bounded, tokenized lease that it
heartbeats through every sibling continuation's final write — including each paced `sync_stale` removal and the terminal
repoint publication. Every continuation re-checks its abort signal before each later Steam mutation. Failed event
delivery releases an unreachable token. The owner's plugin generation is captured before each backend wait, and teardown
tombstones it so a late lease-bearing response is released without doing work; only a genuine remount opens a new
generation. Teardown stops renewal and blocks future writes but defers the explicit release until already-started Steam
promises settle.

A frontend that has just mounted disowns every lease outstanding at that moment, once, before anything else can acquire
one. A continuation whose JS context is torn down mid-call — the double mount at plugin load does this — never reaches
its release and never renews either, so its lease pins the gate for a full TTL with nobody behind it. A fresh mount is
the proof that no earlier continuation survives, which makes it the one moment such an orphan is provably safe to drop;
run claims and callable registrations are untouched, because only the frontend's own leases are the frontend's to
disown. Each one released this way is logged at INFO, since it means a leak happened.

A lease is the frontend's to release, so every path that receives one must give it back — including the paths that do no
work. A terminal completion frame carries a publication lease whenever the run committed a repoint; when the frame turns
out to have nothing to publish, the listener releases it immediately rather than letting it pin the gate until its TTL.
The TTL remains the backstop for the one case the frontend cannot cover: a response lost in transit carries a token the
frontend never learned, and the expiry log is what makes that visible.

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
bundle** (default on), and **Include installed ROM content** per candidate (default off). Whole-game removal also
defaults **on**: removing a game the server no longer has is the operation this dialog exists for, and the default-on
bundle is what keeps it reversible by hand. The two defaults are a pair — turning recovery off requires a separate
acknowledgement toggle before the run can start. The dialog shows recursive size per ROM alongside required and free
space, and blocks confirmation when space is insufficient.

The root is `~/<package-name>-recovery`, with the package name taken from `package.json` through the canonical metadata
adapter and path-sanitized (today: `~/decky-romm-sync-recovery`). Reading free space must not create that layout — a
read-only preview stats the nearest existing parent, and the directories appear only when a bundle is actually written.
The root's own `README.txt`, which explains what the folder is, is written by the same layout-creating step for the same
reason: the only moment the root is known to be wanted is the one that creates it. It is best-effort, because a bundle
must never fail to seal over its folder's signpost.

A bundle records the complete pre-cascade state in lossless JSON: the ROM aggregate, install and metadata state,
save-sync baselines and files, playtime including pending sessions, completion stamps, plugin artifacts, and applicable
Steam-only state. Every exact attributable current save is copied and checksum-verified, and existing `.romm-backup`
history is copied in while remaining at its original location. Bundles are sealed, checksum-verified, descriptor-bound,
and published atomically under `bundles/`.

### The human layer

There is no restore UI, so the folder itself is the restore interface and is built to be read by a person months later.
That layer is **presentation over** the sealed machine layer, never a change to it: `files/NNNNNN`, `manifest.json`,
`checksums.sha256` and `SEAL.json` are what the run's own claim consumption is digest-bound to, and they keep their
shapes.

- **Folder name** — `<sanitized game name>_<YYYY-MM-DD>_<short id>`, named after the row the run is **removing** rather
  than the group's lowest id, so a bundle is never titled after a version that survived. Uniqueness rides on the short
  id; the name is bounded and path-sanitized, and the seal refuses to overwrite an existing directory regardless. The
  folder name is not a rename — it is the `bundle_id` the seal is written with, so `SEAL.json`'s basename binding holds
  by construction.
- **`README.txt`** — a generated index: every ROM id with its name, file name, platform and role in the run (removed, or
  kept and recorded for context); every `files/NNNNNN` blob with its artifact kind in plain words, its size, and the
  absolute path it must be copied back to; playtime in whole units beside the game's name; and step-by-step manual
  restore instructions starting with the checksum verification. It is rendered **inside** the seal, because the blob
  mapping it indexes only exists once the artifacts have been copied.
- **`playtime.txt`** — keeps its exact machine-readable fields, with the game's name beside each ROM id.

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

The shortcut's Steam-side files — grid artwork, the per-app Steam Input roots, and the `localconfig.vdf` controller
entry — are removed only when a recovery bundle captured them, because the capture is where their claims are taken and
no claim means no mutation. With recovery off, the shortcut, rows, ROM content, and plugin caches are still removed, but
those Steam files stay behind: the existing user-triggered orphaned-artwork cleanup collects the grid images later,
while the Steam Input files and controller value linger harmlessly under an appId no shortcut uses.

## Run outcomes

Groups execute serially, and an ordinary group failure does not stop unrelated groups. Recovery, path, disk-space,
checksum, liveness, or Steam-acknowledgement failure skips the affected group **before** any destructive state deletion.
Cancellation stops every group that has not started, and abandons the one in flight if it has not committed anything
yet. Terminal results distinguish exact success, skipped work, known partial mutation, and ambiguous mutation, and the
removed ids and affected appIds stay truthful even after cancellation or a failed event delivery.

`cancel_prune(run_id)` is the wire entry point, reachable from the confirmation dialog and from the Danger Zone while a
run is live. It is deliberately **not** gated by the prune claim — stopping the run is the one operation that must stay
available while that claim is held. It cancels only the run whose id matches, is idempotent for repeat requests, and
answers the canonical failure shape for an unknown, finished, or malformed id. Nothing is rolled back: the group already
executing runs to its own verdict and reports what it committed.

### Where a cancellation lands

Cancellation is cooperative up to the commit point and shielded past it, and the line between them is the first
irreversible act: the Steam action, or — for a group with no Steam action — the finalizer's cascade.

Before that line, the backup phase is the only stretch long enough to matter: copying and hashing a selected ROM of
several hundred megabytes off an SD card takes minutes. It is interruptible. The run hands the sealing worker a one-way
stop flag, polled between artifacts and between copy/hash chunks, so a cancellation is noticed within a chunk instead of
after the copy. The worker unwinds through the same failure path as any other sealing error: its staging directory is
removed, no bundle is published, and nothing else in the group has been touched — so the group is reported `skipped`
with reason `cancelled`, which is the truth. A cleanup that itself fails is still reported as a preserved unsafe staging
directory rather than rewritten into a tidy stop.

Past the line nothing changes: the group runs to its own terminal verdict and reports what it committed, because a
half-finished mutation that nobody recorded is worse than a slow stop. A cancellation arriving during a Steam action or
the cascade therefore still waits, by design.

The claim's release is bound to the run **task**, not to the run body. A task cancelled before the event loop first
schedules it never enters the body whose `finally` normally releases the claim, so a done-callback releases a claim
stranded that way — otherwise the run id would stay set for the process's lifetime and every conflicting callable would
keep being refused with no run left to release it.

## Audit trail

A destructive run logs at INFO on the injected logger, independently of the UI that asked for it: run start (run id,
option set, group and candidate counts), one line per group carrying the fresh liveness verdicts the group's every later
decision turns on (which ids RomM confirmed gone, which are still there, which went unconfirmed, which were discovery
candidates, and which row holds the shortcut), a second line per group with its outcome (status, reason slug, committed
action, removed ids, bundle path), and run end (removed ids, affected appIds, cancellation or failure reason). The end
line is written **before** the completion frame is emitted, so a run whose terminal event never reaches the frontend
still leaves its outcome on disk. Frontend confirmations and cancellations log through `frontend_log`, so one log holds
both sides of the handshake.
