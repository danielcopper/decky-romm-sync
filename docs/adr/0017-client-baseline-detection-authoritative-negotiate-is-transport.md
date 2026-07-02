# Client baseline detection is authoritative; negotiate is the transport

## Status

Proposed. Part of [#1234](https://github.com/danielcopper/decky-romm-sync/issues/1234) (adopt RomM's Device Sync
protocol); implements [#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276) (save-sync conflict safety,
Phase G). **Supersedes the detection-authority decision of
[ADR-0016](0016-save-sync-hands-detection-to-romm-negotiate.md)** — the choice to drive a confirmed non-legacy ROM's
sync from `negotiate`'s operation list. Everything else in ADR-0016 (the negotiate transport, the session lifecycle,
per-device serialization, exact hash parity, the first-sync wizard gate, and the `_MIN_REQUIRED_VERSION ≥ 4.9.0` floor)
stands.

## Context

ADR-0016 adopted RomM's 4.9 Device Sync protocol as a hybrid: `negotiate` would **detect** — returning the server's
`upload` / `download` / `conflict` / `no_op` verdicts per `(rom_id, slot)` pair — while the client kept resolution,
slots, and path logic. Phase 2b/2c wired that up: `MatrixExecutor.dispatch_negotiate_ops` mapped each server op onto the
same action the legacy matrix produced and dispatched it through the shared `_dispatch_sync_action`.

Building the conflict-safety hardening for #1276 meant checking the shipped 4.9.2 server against its own code
(`backend/handler/sync/comparison.py`, `backend/endpoints/sync.py`, `backend/models/assets.py`). Several facts undercut
the detection handoff:

- **The server's comparator is timestamp-based, and its `conflict` branch is near-unreachable.** `compare_save_state`
  decides `upload` / `download` / `no_op` almost entirely from `last_synced_at` versus `updated_at`; the `conflict`
  verdict fires only in a narrow window that the plugin's own bookkeeping (`confirm_download` after every write) keeps
  the device out of. In practice the server almost never returns `conflict`. Handing it detection therefore silences the
  client's baseline-anchored safety branches — the [#1062](https://github.com/danielcopper/decky-romm-sync/issues/1062)
  corrupt/shrunk-local guard, the [#1013](https://github.com/danielcopper/decky-romm-sync/issues/1013) byte-identical
  dedup, and the "both sides moved to content we never synced" conflict (matrix rows 6c / 12).
- **`POST /api/saves` self-guards with a 409.** RomM's `add_save` rejects a plain (non-`overwrite`) POST into a slot the
  device is not current on — **including a device that has never synced that slot** (the `not sync` branch of the gate).
  This is a real server-side conflict backstop the detection path never leaned on.
- **`is_current` is `last_synced_at >= save.updated_at`** — greater-or-**equal**, so equality counts as current. The
  architecture doc had it as strict `>`.
- **A pre-existing divergence.** The read-only status path (`StatusService`, which feeds the game-detail SAVES tab)
  always computed its verdicts from `compute_sync_action`, while a confirmed non-legacy ROM's sync run took its verdicts
  from the server's op list. The two decision sources could disagree — the SAVES tab could show one thing while the sync
  did another — because they no longer shared a kernel.

Taken together: the server's detection is weaker than the client kernel it was meant to replace, and running two
different decision sources — the op list for sync, the kernel for status — is itself a latent bug.

## Decision

**Client-side `compute_sync_action` is the sole save-sync authority for every ROM; `negotiate` is retained purely as the
transport.**

- **One kernel, every ROM.** `SyncEngine._run_rom_sync` runs the same path for confirmed non-legacy and legacy
  `slot:null` ROMs alike: load the `RomSaveState`, fetch server saves with `list_saves`, run `compute_sync_action` per
  file, dispatch through the unchanged `_dispatch_sync_action`. The op→action fork (`dispatch_negotiate_ops` and its
  helpers) is deleted. `StatusService` was already kernel-driven and is untouched, so the sync run and the SAVES tab now
  provably share the one decision.
- **`negotiate` stays, transport-only.** When a ROM has a confirmed non-legacy slot the run still opens a negotiate
  session and closes it in a `finally`. The session earns its keep as the per-device serialization primitive
  (`negotiate` cancels the device's prior in-flight sessions) and as the ingest hook for play-session reporting
  ([#1219](https://github.com/danielcopper/decky-romm-sync/issues/1219)). Its returned `operations` are **discarded** —
  detection is the client's. A session-open failure is non-fatal: the run proceeds without a session.
- **Automatic uploads POST with `overwrite=false`, backstopped by the 409.** Every upload the kernel dispatches is a
  POST that creates a new datetime-tagged version and does **not** set `overwrite`. If the server 409s (the `add_save`
  gate above), the client re-fetches the slot and resolves through the pure
  `resolve_upload_conflict(local_hash, last_sync_hash, server_content_hash)`: local unchanged since the baseline →
  downgrade to a download; local already byte-identical to the server head → download; otherwise surface the same
  `SyncConflictModal` a matrix `Conflict` uses. The in-place PUT is no longer on the automatic path.
- **`overwrite=true` only on an explicit `keep_local`.** The single caller that sets `overwrite=true` is the user's
  Keep-Local conflict resolution — a deliberate choice to overwrite the server head.
- **Legacy `slot:null` is retired as a confirmable target, kept as a migration source.** `confirm_slot` now requires a
  real slot name (empty / `None` rejected), and `resolve_default_slot` never returns `None` (blank → `"default"`).
  Migration `005` un-confirms any ROM previously confirmed in legacy mode (`active_slot IS NULL AND slot_confirmed=1`),
  so the first-sync wizard reappears and the user picks a named slot — optionally migrating the legacy saves into it.
  `domain/save_slot.py` still recognizes `slot:null` on the wire so those saves can be **read and migrated**
  ([#1061](https://github.com/danielcopper/decky-romm-sync/issues/1061)), but no ROM is ever confirmed onto the legacy
  slot again.
- **The version-switch PUT is out of scope.** `versions.py`'s rollback flow still PUTs-to-bump a chosen older save's
  `updated_at` to make it authoritative cross-device; that destructive, user-initiated flow is unchanged here.

## Consequences

- **(+) The status/sync divergence is closed.** Every ROM's sync and its SAVES-tab status come from the one kernel, so
  they can no longer disagree.
- **(+) The silent-overwrite gap in matrix row 11 is fixed.** A `current=false` local with no baseline whose content
  differs from the server head is now a `Conflict` (row 11b) instead of a silent `Download`; only a byte-identical local
  downloads (row 11a).
- **(+) The ungated in-place PUT is gone from the automatic path.** Automatic uploads POST a new version and are
  backstopped by the server 409, so a stale device can no longer overwrite a save it is not current on without the user
  deciding.
- **(−) More version churn.** Steady-state uploads now POST a new tagged version each sync instead of PUTting in place,
  so a slot accumulates versions faster. This is bounded by `autocleanup_limit` — the server prunes each slot back to
  the cap.
- **(−) Two round-trips per confirmed sync.** A confirmed ROM now does both a `negotiate` (transport/session) and a
  `list_saves` (detection), where the detection-handoff path did one.
- **(−) The never-synced case surfaces more conflicts.** Matrix row 6a and the 409 `not sync` branch mean a brand-new
  device whose local save differs from an existing server head is more likely to prompt the user than to silently pick a
  side — intended under the "user decides on ambiguity" invariant, but more prompts than the timestamp comparator would
  have raised.
- **Breaking:** migration `005` runs once and re-opens the wizard for any ROM that was confirmed in legacy mode; those
  users re-pick a slot. **No save data is deleted** — it is a `slot_confirmed` flag flip.
- **Reversible in spirit:** authority lives in one kernel, so restoring server-side detection is re-reading the op list
  the transport already returns — the negotiate wiring, sessions, and hash parity all stay. Migration `005` carries no
  data loss.
- **On-device (Game Mode) verification required** — the POST-per-upload churn, the 409 backstop under a genuinely stale
  device, and the negotiate session under rapid Sync/Cancel can't be exercised by unit tests alone.

## Alternatives considered

- **A — Keep the server's operation list authoritative (ADR-0016 as written) and only bolt the 409 backstop onto
  uploads.** Rejected: the 4.9.2 comparator is timestamp-only with a near-dead `conflict` branch, so it cannot reproduce
  the client's baseline-anchored safety (the #1062 shrink guard, the #1013 dedup, and the rows 6c / 12 both-sides-moved
  conflict), and it leaves the `StatusService`-versus-sync divergence in place — the SAVES tab and the sync run would
  still decide from different sources.
- **B — Drop `negotiate` entirely and return to pure `list_saves` + `compute_sync_action` with no session.** Rejected:
  `negotiate` is the only source of the per-device session serialization (`cancel_active_sessions`) and the play-session
  ingest hook ([#1219](https://github.com/danielcopper/decky-romm-sync/issues/1219)), and it is the interop point with
  RomM's first-party device-sync ecosystem (Argosy, Grout) that ADR-0016 set out to join. Keeping it as transport-only
  preserves both while moving detection back to the client kernel.

## See also

[#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276) (conflict safety, Phase G),
[#1234](https://github.com/danielcopper/decky-romm-sync/issues/1234) (Device Sync adoption),
[ADR-0016](0016-save-sync-hands-detection-to-romm-negotiate.md) (the detection handoff this supersedes in part),
[#748](https://github.com/danielcopper/decky-romm-sync/issues/748) (`confirm_download` / PUT-bump — now moot on the
automatic POST path, still live for the version-switch flow),
[#1062](https://github.com/danielcopper/decky-romm-sync/issues/1062) (corrupt/shrunk-local guard),
[#1013](https://github.com/danielcopper/decky-romm-sync/issues/1013) (byte-identical dedup),
[#1061](https://github.com/danielcopper/decky-romm-sync/issues/1061) (legacy `slot:null` wire addressing, now
migration-only).
