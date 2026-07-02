# Native play-session tracking — additive per-session ingest, local aggregate stays the read-model

## Status

Proposed. Implements Phase 5 of [#1234](https://github.com/danielcopper/decky-romm-sync/issues/1234) (adopt RomM's
Device Sync ecosystem) and closes the research in [#1219](https://github.com/danielcopper/decky-romm-sync/issues/1219).
Depends on [#1280](https://github.com/danielcopper/decky-romm-sync/pull/1280) (the `roms.user.read` token scope).
Refines the "play-session ingest rides the `negotiate` session-complete hook" framing that
[ADR-0016](0016-save-sync-hands-detection-to-romm-negotiate.md) /
[ADR-0017](0017-client-baseline-detection-authoritative-negotiate-is-transport.md) assumed in passing — see _Decision →
Standalone endpoint_.

## Context

We already track playtime. `PlaytimeService` folds a session's duration into a per-ROM `Playtime` aggregate on game-exit
and mirrors the cumulative total into a RomM **note** (`romm-sync:playtime` = `{"seconds", "updated", "device"}`). The
note is a storage hack: it has no `session_count`, no per-session granularity, and no `last_played` (exactly the
[#903](https://github.com/danielcopper/decky-romm-sync/issues/903) gap); no other RomM client reads it; and its
cross-device merge is a `max(local, server)` clamp that silently mis-counts once two devices are both active.

RomM 4.9 shipped first-party play-session tracking (rommapp/romm#3155). Verified against a live 4.9.2 server:

- **`POST /api/play-sessions`** — a standalone batch (max 100) of `{rom_id, start_time, end_time, duration_ms}` under a
  top-level `device_id`. Duplicates are dropped by `(user_id, device_id, rom_id, start_time)`; an unknown device/rom
  resolves to `NULL` rather than rejecting. The ingest updates `rom_user.last_played` and `device.last_seen`. `GET`
  (with filters) and `DELETE` round it out. The same ingest is _also_ reachable embedded in
  `POST /api/sync/sessions/{id}/complete`.
- **There is no aggregate playtime field.** `rom_user` carries `last_played` but no total/`play_count`; the PR ships "no
  frontend surface." RomM stores the raw session rows but renders **no** aggregated playtime number yet — only
  `last_played` is visible.
- `duration_ms` is **screen-on** time (may diverge from the `start_time`..`end_time` window under suspend). This maps
  1:1 onto our existing model: window = session start/end, `duration_ms` = our suspend-adjusted elapsed × 1000.

The decisive observation: **playtime is additive, not a conflict.** A save is a single overwritable artifact — two
devices editing it diverge and one must win (the newest-wins / conflict / `.romm-backup` machinery). Playtime is the
**union of per-device session streams**: device A plays 1 h, device B plays 2 h, the true total is 3 h. RomM's native
model already expresses this — per-device rows, dedup by `start_time`. So the save-sync machinery is the wrong tool
here.

## Decision

**Adopt RomM's native play-sessions as the shared store via the standalone endpoint, treating playtime as an additive
per-session stream. The local `Playtime` aggregate stays the cumulative display read-model; the note is retired.**

- **Per-session ingest, additive.** On game-exit (`record_session_end`) we keep folding the duration into the local
  cumulative total exactly as today (so the local total is always correct, offline included), **and** we record the
  session `(rom_id, device_id, start, end, duration_ms)` and `POST /api/play-sessions`. RomM accumulates the union
  across devices; `start_time` dedup makes any re-POST idempotent. No `content_hash`, no newest-wins, no conflict modal,
  no `.romm-backup` — none of the save-sync machinery applies.
- **Offline outbox.** Unsent sessions are held in a small pending-session outbox owned by the `Playtime` aggregate. Exit
  enqueues and attempts the POST; success dequeues. Offline → the session stays queued and is flushed on the next
  launch/sync/reconnect. This is the "only DB, then catch up local→server" path, minus any conflict logic.
- **Reconcile is `max(local, Σ server)`.** On the detail view we first flush the outbox, then `GET /api/play-sessions`
  for the ROM and set the displayed total to `max(local_total, Σ server duration)`. Playtime is monotonic, so `max()` is
  always safe: with the outbox drained it naturally adopts the server union ("the server holds the whole total"); with a
  partial flush, an unreachable server, or a not-yet-backfilled history it keeps the display from regressing below the
  local truth. There is no newest-wins branch.
- **Standalone endpoint, not the `negotiate` hook.** We ingest through `POST /api/play-sessions`, **not** the
  `play_sessions` field on `POST /api/sync/sessions/{id}/complete`. Playtime must be recorded for **every** ROM on every
  exit — including ROMs whose save-sync is off or whose slot is unconfirmed — so it cannot be coupled to the save-sync
  session lifecycle (which only opens a `negotiate` session for confirmed non-legacy slots). This intentionally narrows
  the "ingest rides `negotiate`" aside in ADR-0016/0017: the `negotiate` session stays a save-sync transport, and
  playtime is its own decoupled ingest.
- **Migration: fresh native start, no backfill (option B1).** The pre-migration cumulative total is **not** written to
  RomM. Native accumulation starts at the cutover; the local total (via `max()`) keeps showing the real historical value
  until server-side accumulation overtakes it. A synthetic-session backfill of the historical total is deferred to
  [#868](https://github.com/danielcopper/decky-romm-sync/issues/868) — it is lossy (fabricated window), risks
  cross-device double-counting (two devices each backfilling a note-derived total over-count shared history), and buys
  almost nothing while RomM renders no aggregate playtime.
- **Retire the note (option ii — stop writing, don't mass-delete).** `PlaytimeService` stops writing
  `romm-sync:playtime` and reconcile stops reading it. Existing notes are left in place (ignored, harmless) rather than
  swept with a per-ROM `DELETE` across the whole library; a release note tells users they may delete the old notes
  themselves, and an optional Settings cleanup action can be added later.
- **Reconcile needs `roms.user.read`.** `GET /api/play-sessions` is scoped `roms.user.read`, added to the minted token
  in [#1280](https://github.com/danielcopper/decky-romm-sync/pull/1280); it takes effect on the next sign-in. Without
  the scope the reconcile GET degrades to local-only (no cross-device pull) — never an error.

## Consequences

- **(+)** A canonical, structured, interoperable store replaces the note hack; `last_played` becomes correct in RomM's
  web UI immediately (the visible half of #903). Session data is forward-compatible with a future RomM playtime UI and
  with other Device-Sync clients.
- **(+)** No conflict machinery. The additive model needs no hashing, no newest-wins, no modal, no backup — materially
  simpler than the save-sync path.
- **(+)** Offline-robust by construction: the local total is always correct (folded regardless of network), and the
  outbox + `start_time` dedup make catch-up safe and idempotent.
- **(−)** Under B1 the native store under-reports the true historical total until it re-accumulates; a freshly-installed
  device pulling only from the server sees just post-migration play. `max()` protects the local display; full history is
  a later #868 backfill.
- **(−)** Old `romm-sync:playtime` notes are orphaned on the server until a user deletes them (cosmetic; no reader
  remains).
- **(−)** Two best-effort round-trips are added off the hot path (POST at exit, GET at reconcile); neither blocks the
  game-exit UX, and both degrade silently offline.
- **(−)** Cross-device reconcile depends on a re-signed-in token carrying `roms.user.read`; until then reconcile is
  local-only.
- **Not data-destructive.** No local playtime is lost; the note is retired in place, not deleted.
- **On-device (Game Mode) verification required** — the exit-time POST, the offline→outbox→flush catch-up, and the
  cross-device `max()` reconcile can't be fully exercised by unit tests.

## Alternatives considered

- **A — Keep the `romm-sync:playtime` note.** Rejected: non-interoperable, no `session_count` / `last_played`, and the
  `max(local, server)` clamp mis-counts once two devices are active.
- **B — Sync the cumulative total as a single number** (one native session bumped on each exit). Rejected: a number does
  not merge across devices (double-count or loss); it discards RomM's per-session model and would feed a future
  aggregate UI nothing usable. It is the note hack relocated.
- **C — Ingest via the `negotiate` session-complete hook** (as ADR-0016/0017 assumed). Rejected: it couples playtime to
  the save-sync lifecycle (confirmed slot, save-sync enabled), but playtime must be recorded for every ROM on every
  exit. The standalone endpoint is purpose-built and decoupled.
- **D — Backfill the historical total now (option B2).** Rejected for now: lossy synthetic window, cross-device
  double-count risk, and ~zero payoff while RomM shows no aggregate playtime. Deferred to #868, to be done carefully
  once it is worth the fidelity cost.

## See also

[#1219](https://github.com/danielcopper/decky-romm-sync/issues/1219) (play-session research),
[#1234](https://github.com/danielcopper/decky-romm-sync/issues/1234) (Device Sync adoption, Phase 5),
[#1280](https://github.com/danielcopper/decky-romm-sync/pull/1280) (`roms.user.read` scope),
[#903](https://github.com/danielcopper/decky-romm-sync/issues/903) (`session_count` + `last_played`),
[#868](https://github.com/danielcopper/decky-romm-sync/issues/868) (playtime backfill — the deferred B2),
[#978](https://github.com/danielcopper/decky-romm-sync/issues/978) /
[#1148](https://github.com/danielcopper/decky-romm-sync/issues/1148) (suspend/resume `duration_ms` accuracy),
[ADR-0016](0016-save-sync-hands-detection-to-romm-negotiate.md) /
[ADR-0017](0017-client-baseline-detection-authoritative-negotiate-is-transport.md) (the `negotiate` transport this
ingest deliberately does _not_ ride).
