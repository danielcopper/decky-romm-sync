# The per-unit apply is chunked into durable per-chunk commits

## Status

Accepted. Tracked under [#1025](https://github.com/danielcopper/decky-romm-sync/issues/1025). Extends
[ADR-0006](0006-narrow-unit-of-work-scope.md) (the narrow write Unit of Work each chunk commits under) and the
group-aware apply of [ADR-0021](0021-sibling-group-one-shortcut-binding-active-version.md) (chunks cut only at
sibling-group boundaries, so a game's dumps never straddle two commits). It carries the #1041 run/unit ack identity and
the #1052 heartbeat-timeout late-ack recovery down to chunk granularity.

## Context

The library apply emits one `sync_apply_unit` event per platform/collection unit, carrying that unit's whole collapsed
shortcut list, then waits for the frontend's `report_unit_results` ack before committing the unit's `roms` rows in a
single write UoW. On small libraries this is fine. On large ones it is a single point of catastrophic loss.

[#797](https://github.com/danielcopper/decky-romm-sync/issues/797) is the field data. A platform unit of **3084
shortcuts** was emitted in one event. The frontend applied them at the CEF-safe 50 ms cadence — about 24 minutes of work
— and roughly 24 minutes in, `steamwebhelper` died of an internal out-of-memory (a `SIGTRAP` inside `libcef.so`, the
process image near 4 GB). No `report_unit_results` ever arrived, so the backend committed **nothing**: the entire unit's
24 minutes of shortcut creation was forfeit, and the next sync started it over from zero. Two compounding costs made the
single emit fragile:

- **Payload size.** One unit's shortcut list is a multi-MB JSON blob (~3 MB at 3084 entries) pushed through the
  size-limited `decky.emit` WebSocket bridge in one frame, and the frontend closure holds the whole list live for the
  entire apply.
- **Blast radius.** The commit is all-or-nothing at unit granularity, so any crash, cancel, or heartbeat timeout
  anywhere in those 24 minutes discards the whole unit.

## Decision

**A unit's emitted shortcuts are split into fixed-size chunks that are emitted, acked, and committed one at a time, so a
mid-unit failure forfeits only the in-flight chunk.**

- **Chunk size is a fixed 200** (`_APPLY_CHUNK_SIZE`, `services/library/sync_orchestrator.py`). At ~200 entries a
  chunk's payload is ~200 KB (comfortably under the bridge ceiling) and its apply is ~2 minutes at the 50 ms cadence —
  the crash blast radius drops from 24+ minutes to ~2. It is not configurable (see Alternatives).
- **Chunks cut only at sibling-group boundaries** (`domain/sync_chunking.py::build_unit_chunks`, pure). The collapsed
  emit list is group-clustered (ADR-0021); a chunk greedy-fills to 200 but **overflows to finish a group** so a
  multi-version game's entries never straddle two chunks — and therefore two commits. A keyless entry (a legacy or solo
  ROM) is its own singleton run, so a cut before or after it is always legal. Groups fetched but never emitted (a
  partial view's grandfathered siblings) plus any unmatched leftover ROMs ride chunk 0's commit row set; an empty unit
  yields exactly one empty chunk, so the empty round-trip and its unbound-row commit survive.
- **Each chunk is a durable commit.** `sync_apply_unit` now also carries `chunk_index` / `chunk_count` / `chunk_offset`
  / `unit_total`, and `shortcuts` is the chunk slice. On the ack the reporter commits only that chunk's `roms` rows —
  the same group-aware two-pass write it already did per unit (every fetched sibling upserted, only representatives
  bound; Rom row before `rom_metadata`, FK-safe; the ADR-0006 narrow UoW), now over a row subset. A committed chunk is
  crash-safe on its own; the next chunk emits only after it commits.
- **Reuse the existing event and callable, extended in place.** `sync_apply_unit` gains four fields and
  `report_unit_results(map, run_id, unit_id, chunk_index)` gains the chunk index. No new `sync_apply_chunk` event and no
  new ack callable were added — a chunk is the unit at a finer grain, so the chunk protocol is the unit protocol.
  Frontend and backend ship in the same PR, the callable-manifest and event-parity gates keep them in lock-step, and
  there is no half-wired dead surface (a new event with no listener, or a callable with no caller, would fail those
  gates).
- **Ack identity is chunk-scoped.** The #1041 identity check (`run_id` == `current_sync_id`, `unit_id` ==
  `active_unit_id`) gains `chunk_index` == `active_chunk_index`. The orchestrator stamps `active_chunk_index` before
  each chunk's emit and clears it at commit, cancel, or timeout (moved into the abandoned-chunk stash below), so a late
  ack for a superseded chunk can never commit against a newer one — the same defense the run/unit identity already gave,
  at chunk granularity.
- **Late-ack recovery is per chunk, via an abandoned-chunk stash (#1052 / #1367).** A heartbeat timeout moves the
  **abandoned chunk** — its run/unit/chunk identity plus its fetched rows (the `metadatum` source), only that chunk,
  never the whole unit — into an `AbandonedChunk` stash held on `LibrarySyncStateBox` **outside** the run-lifecycle
  state; every chunk committed before the timeout stays committed. The stash deliberately **survives the run's
  teardown** (`finish_run` nulls `current_sync_id` but leaves the stash), because in production the frontend's late
  `report_unit_results` arrives **after** the run has already wound down — the exact window an earlier design missed,
  where the active-unit ack check could no longer match and the recovery was unreachable (#1367). The late ack matches
  the stash **by identity** (`take_abandoned_chunk`) and drives `commit_unit_results` itself over the stashed rows,
  binding the delivered shortcuts instead of leaving orphans; it **never** passes a `platform_stamp`, since a timed-out
  platform is incomplete and must not be stamped complete. The stash has a **bounded lifetime**: it is cleared at the
  next run's `try_begin_run`, so a frontend that crashes and never acks just leaves inert data that the next sync drops
  (the orphan self-heals via the existing-shortcut scan either way).
- **Cancel is chunk-atomic.** A user cancel or a timeout mid-unit forfeits only the in-flight chunk; every chunk
  committed before it survives. The `SyncRun` still completes **only at run end**, so a partial unit is never recorded
  as complete and the incremental-skip gate re-fetches it on the next run; the stale-removal scan is already skipped on
  a cancelled run, so partial progress never triggers removals.

## Consequences

- **A large unit survives a mid-apply crash.** The #797 scenario now loses one ~2-minute chunk, not 24 minutes; the
  committed chunks are on disk and the next sync resumes from the first uncommitted game.
- **The bridge payload and frontend memory are bounded by the chunk, not the unit** — ~200 KB and one chunk's closure
  instead of ~3 MB and the whole list.
- **Cancel/timeout becomes chunk-atomic rather than unit-atomic.** Cancelling a large unit keeps the games already
  committed in prior chunks — a behavior change: previously the whole in-flight unit was discarded. This is strictly
  less lost work and matches "cancel keeps finished games," but it does mean a cancelled unit can leave a **partially**
  applied platform; the next sync completes it. Platform units that _did_ finish (their last chunk committed) before the
  cancel are additionally stamped complete (`PlatformSyncState`, #1025) in the same write UoW as that final chunk, so
  the next run's incremental-skip gate skips them wholesale rather than re-walking every already-applied game through
  CEF — even though the cancelled run never completed its `SyncRun` and so never advanced the library-wide `last_sync`.
- **The stamp is the sole skip authority; its contract is
  `stamp exists ⟺ the platform's most recent apply attempt ran
  to completion`.** A completed run's library-wide
  `last_sync` is deliberately **not** a fallback for the skip: a run-scoped timestamp cannot see a platform whose
  shortcuts were locally removed and only partially re-applied since, so trusting it can silently skip a platform with
  missing shortcuts — the same gap in a second coat. No stamp means a full fetch; installations from before this
  contract carry no stamps, so their first sync re-walks once (update-path cheap) and stamps everything it completes.
  Two rules keep the contract true. (1) The stamp is **cleared at a platform unit's apply start** (in `_sync_one_unit`,
  once the fetch has succeeded and the apply is about to emit its first chunk) and re-written only by that unit's final
  chunk, so an apply interrupted by a crash / cancel / heartbeat-timeout before the final chunk leaves **no** stamp —
  never a stale one from a prior run. (2) The **local destructive flows** invalidate the stamp of every platform whose
  shortcuts they unbind: the DangerZone remove-all and per-platform removals (both via `report_removal_results`) and the
  Steam-UI-deletion reconcile (`reconcile_live_shortcuts`) delete the touched platforms' stamps in the same write UoW as
  the unbind. Both rules exist because unbinding keeps the `roms` row (ADR-0007), so a platform's persisted-row count is
  unchanged and a surviving stamp with a matching `rom_count` would let the skip gate skip a half-mirrored platform and
  silently drop the un-recreated games (the #1025 gap). The server-side stale removal in the reporter is the deliberate
  exception — it does **not** invalidate the stamp, because a ROM the server dropped lowers RomM's platform `rom_count`,
  which the stamp's `rom_count` guard already catches on the next skip.
- **More round-trips.** A 3084 unit now runs ~16 emit/ack/commit cycles instead of one. Each cycle adds an event, a
  callable ack, and a short write UoW; the added overhead is roughly 2% of the unit's apply time — negligible against
  the crash-recovery it buys.
- **No wire-surface growth.** Extending the existing event and callable keeps the callable-manifest and event-parity
  gates green with no new names to police.
- **The operational envelope this buys against.** The crash-resume emphasis rests on a measured finding, not a
  hypothetical. Steam's renderer (`steamwebhelper` / CEF) accumulates memory as each shortcut is touched — roughly
  0.8–1.5 MB per created shortcut observed on-device — against a finite per-session budget, with the process image seen
  climbing to roughly 2.5 GB during a large first import before it OOM-crashed (#797). A mass first import of a
  multi-thousand-game library can therefore exhaust the budget **mid-run**. Chunking does not raise that ceiling; it
  converts hitting it from catastrophic loss (the whole unit forfeit) into a cheap resume — every chunk committed before
  the crash is on disk, and the next sync continues from the first uncommitted game. Raising the ceiling itself is the
  out-of-CEF bulk-import path below, out of scope for this decision.

## Alternatives considered

- **A dedicated `sync_apply_chunk` event and a new chunk-ack callable.** Rejected as pure churn: the chunk is the unit
  at a finer grain, so a parallel event/callable pair would duplicate the identity check, the late-ack path, and the
  parity-gate entries for no behavioral gain — two protocols to keep in sync instead of one.
- **A configurable chunk size** (a setting or a heuristic). Rejected: 200 already balances payload, cadence, and blast
  radius across the library sizes we see, and a user-facing knob invites misconfiguration — a too-large value re-opens
  the #797 loss — for a value no one needs to tune. `_APPLY_CHUNK_SIZE` stays a build-time constant.
- **Bulk-importing shortcuts outside CEF** (writing `shortcuts.vdf` directly, or an out-of-process importer that
  bypasses the per-shortcut SteamClient cadence). Deliberately **not** part of this decision. `shortcuts.vdf` is
  memory-authoritative — Steam rewrites it from memory and clobbers external writes while it is running (see
  [Steam Non-Steam Shortcuts](../architecture/steam-non-steam-shortcuts.md)) — so a safe bulk path requires Steam
  restarted or not running, which is a different, research-gated design. Chunking hardens the in-CEF path we have
  without blocking that future work; the bulk-import route is tracked separately.

## See also

- [#797](https://github.com/danielcopper/decky-romm-sync/issues/797) (the field crash),
  [#1025](https://github.com/danielcopper/decky-romm-sync/issues/1025) (chunked apply),
  [#1041](https://github.com/danielcopper/decky-romm-sync/issues/1041) (run/unit ack identity),
  [#1052](https://github.com/danielcopper/decky-romm-sync/issues/1052) (heartbeat-timeout late-ack recovery),
  [#1367](https://github.com/danielcopper/decky-romm-sync/issues/1367) (abandoned-chunk stash — makes the recovery
  reachable in production)
- [ADR-0006](0006-narrow-unit-of-work-scope.md) (the narrow write UoW each chunk commits under)
- [ADR-0021](0021-sibling-group-one-shortcut-binding-active-version.md) (sibling-group collapse — the boundary chunks
  cut at)
- [Backend Architecture](../architecture/backend-architecture.md) and
  [Steam Non-Steam Shortcuts](../architecture/steam-non-steam-shortcuts.md) (the apply pipeline this chunks)
