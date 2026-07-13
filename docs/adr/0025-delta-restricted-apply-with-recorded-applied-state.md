# The apply emits only new and changed shortcuts, tracked against a recorded applied launch command

## Status

Accepted. Tracked under [#1383](https://github.com/danielcopper/decky-romm-sync/issues/1383) (with the estimate work of
[#1382](https://github.com/danielcopper/decky-romm-sync/issues/1382) in lockstep). Completes the scale epic that
[ADR-0023](0023-chunked-per-unit-apply.md) (chunked durable apply) and [ADR-0024](0024-session-budget-rss-gate.md)
(RSS-based pause) opened: ADR-0024 explicitly booked "a single oversized platform re-touches its chunks across restarts
… wasted work until the delta-restricted apply tracked in #1383 lands" as a known consequence. This decision lands that
piece.

## Context

The per-unit apply (ADR-0023) re-emits a platform unit's **entire** collapsed shortcut list every time it runs. On a
resume — or any re-sync of a platform that isn't skipped wholesale by its completion stamp — the frontend re-touches
every already-correct Steam shortcut: a full `SetShortcutName` / `SetShortcutExe` / `SetShortcutStartDir` /
`SetAppLaunchOptions` walk plus a ~2 s `AppDetails` confirm poll, per item, at the CEF-safe 50 ms cadence. For a large
platform that is minutes of work that changes nothing, and the progress counter reads the whole library ("402/1953")
rather than the handful of games that actually differ. A resume that only needs to finish 150 games walks 3000.

Two facts make a finer skip possible:

- **The classifier already exists.** `domain/sync_diff.py::classify_roms` buckets fetched ROMs into new / changed /
  unchanged against the bound-shortcut registry — but until now it ran only in the read-only **preview**, to produce the
  summary counts. The apply path never consulted it.
- **Identity is not enough to define "unchanged".** The registry carries a ROM's persisted identity (name, `fs_name`,
  `platform_slug`), but a shortcut's launch command changes without any of those changing: install/uninstall flips it
  between the full RetroDECK command and the empty placeholder (ADR-0009), and a per-game core override or disc pin
  re-bakes it. An identity-only "unchanged" would skip a shortcut whose launch command is stale, leaving it pointing at
  a deleted file or the wrong core.

## Decision

**Run the classifier in the apply path and emit only new + changed shortcuts; skip content-unchanged items entirely.
Define "unchanged" as identity **plus** the launch command, by recording on each ROM the `launch_options` last written
to its shortcut and comparing the freshly built target against it.**

- **Classify in `_sync_one_unit`, between collapse and chunking.** After the sibling-group collapse produces the unit's
  emitted entries, `classify_roms` splits them against the per-unit registry. Only new + changed (plus rebind entries —
  see below) become the emitted **delta** that crosses the wire and drives the frontend `Set*` walk. Unchanged entries
  are dropped from the wire but their `roms` rows still commit (next point). The per-unit `sync_apply_unit` frame's
  `unit_total` therefore becomes the delta size, so the progress counter shows net progress and a resume converges with
  or without a Steam restart.

- **A recorded applied launch command is the skip authority.** A new nullable column `applied_launch_options` on the
  `roms` aggregate (migration 015) holds the launch command last written to that ROM's shortcut. `classify_roms` marks
  an item "changed" on an identity mismatch **or** a mismatch between the item's built target `launch_options` and the
  recorded `applied_launch_options`. This is deliberately **content**, not identity-only: it catches the
  install/uninstall and pin-change cases that leave identity untouched.

- **`NULL` means unknown, and unknown is never skipped.** A pre-migration-015 row, and every freshly created row until
  it is recorded, reads `NULL`; `NULL` never matches a target string, so such a row is always "changed" and re-applied
  once. No skip is ever taken on unknown recorded state — the first post-upgrade sync behaves exactly like today,
  records values as it goes, and only subsequent syncs skip. No applied state is invented from the migration.

- **Five writer sites keep the recorded value fresh**, each recording the value it just had the frontend write onto the
  shortcut: the sync ack-commit (for this cycle's acked representatives), download-complete, uninstall (records `""`),
  the RetroDECK-home migration re-resolve, and a version switch. `applied_launch_options` is written **only** by these
  sites, via a dedicated pin-only write path; it is excluded from the sync UPSERT, exactly like the `emulator_override`
  and `selected_disc` pins, so an unrelated re-save never wipes it and a skipped row keeps its recorded value across
  every re-sync.

- **Composition-aware gate pricing.** Because the emitted chunk is now new + changed only, the ADR-0024 session-budget
  gate prices each chunk by composition instead of pricing every item as a cover-applying create: a create pays the
  worst-case create rate plus its transient cover term, a changed/rebind item pays the lighter `Set*`-walk rate. The
  frontend still decides create-vs-update itself via its existing-shortcut scan; the backend classification only prices
  the chunk, and a small backend/frontend mismatch can only ever overprice (worst-case safe).

## Consequences

- **A resume converges to net work.** A resume redoes only the games that actually differ; the "re-touch its chunks
  across restarts" waste ADR-0024 booked is gone. The progress counter reads the delta, and the time estimate prices
  only the delta (the #1382 estimate work: the preview row and seeds drop the unchanged term, and the live countdown
  corrects each dispatched unit's weight to its real delta via `unit_total`).

- **The stamp contract still holds under a skip-heavy resume.** Skipped rows still commit — chunking routes their groups
  to chunk 0's leftover — so no DB work is dropped, and the per-platform completion stamp (ADR-0023) still rides the
  **final** chunk only. A platform is therefore stamped exactly when its whole delta is durably applied: an empty-delta
  platform emits one empty chunk that commits every row and writes the stamp; a skip-heavy resume that crashes mid-delta
  leaves no stamp, re-fetches, and finds the already-applied (now recorded) items skip on the retry. "Stamp exists" ⟺
  "this platform's apply ran to completion" is unchanged.

- **A missed writer degrades safely — the benign-failure asymmetry.** The recorded value is the safety pivot. If a
  writer site is ever missed, the recorded value stays stale-but-**mismatching** the fresh target, so the item
  classifies as "changed" and is harmlessly re-applied to the same value — a wasted `Set*` walk, never a wrong skip.
  There is no failure mode where a missed write causes a shortcut to be skipped while stale. (Drift from a frontend
  write that silently fails is separately healed by the launch-options reconcile on the Play button and at startup.) The
  asymmetry is what makes recording-the-intended-value the right model: over-applying costs time, under-applying would
  cost correctness, and the design can only ever over-apply.

- **Force Full Sync is the escape hatch for drift the recorded value cannot see.** The one blind spot of
  recording-the-intended-value is Steam-side drift that leaves the recorded value **matching** the fresh target while
  the real shortcut differs (a manually edited or corrupted shortcut) — the delta skip would then skip it forever.
  `clear_sync_cache` (Force Full Sync) therefore resets every `applied_launch_options` to NULL alongside the run history
  and platform stamps: NULL never matches a target, so the forced run re-applies and re-records everything. "Force"
  forces past the per-item skip, keeping the pre-delta repair semantics of the button.

## Alternatives considered

- **Identity-only skip, trusting the frontend stale-shortcut reconcile.** Skip on the identity triple alone and lean on
  `reconcileStaleShortcuts` to catch anything wrong. Rejected: identity is blind to launch-command drift — an
  install/uninstall or a core/disc pin change leaves identity identical while the shortcut's command is stale, so an
  identity-only skip would strand a shortcut pointing at a deleted file or the wrong emulator. The reconcile handles
  user-deleted shortcuts, not command drift on live ones.

- **Read each shortcut's live `launch_options` from Steam at apply time.** Ask `AppDetails` for the current command per
  item and compare. Rejected: that per-item `AppDetails` read is a fat cache hit costing ~2 s — the exact cost the skip
  exists to remove. Recording the value we wrote, once, at write time, is free at classify time.

- **Compare mtime / counts instead of content.** A cheaper "did anything change" proxy. Rejected on the project's hard
  rule that change detection is content, never mtime or counts; a launch command is content and is compared as a string.

## See also

- [#1383](https://github.com/danielcopper/decky-romm-sync/issues/1383) (delta-restricted apply) ·
  [#1382](https://github.com/danielcopper/decky-romm-sync/issues/1382) (skip-aware estimates)
- [ADR-0023](0023-chunked-per-unit-apply.md) (chunked durable apply — the row-commit + platform-stamp mechanics this
  decision skips into)
- [ADR-0024](0024-session-budget-rss-gate.md) (the RSS gate whose per-item cost this decision prices by composition)
- [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked `launch_options` model the recorded
  value tracks)
- [Backend Architecture](../architecture/backend-architecture.md) ·
  [Database Design](../architecture/database-design.md)
