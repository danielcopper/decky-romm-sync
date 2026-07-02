# Save-sync hands conflict _detection_ to RomM's negotiate protocol; the client keeps resolution, slots, and path/core logic

## Status

Proposed. Part of [#829](https://github.com/danielcopper/decky-romm-sync/issues/829) (evaluate adopting RomM's
first-party Device Sync Protocol) and tracked for implementation by
[#1234](https://github.com/danielcopper/decky-romm-sync/issues/1234). Auth prerequisite is
[#163](https://github.com/danielcopper/decky-romm-sync/issues/163); retires the `confirm_download` ack per
[#748](https://github.com/danielcopper/decky-romm-sync/issues/748); play-session reporting
([#1219](https://github.com/danielcopper/decky-romm-sync/issues/1219)) is split out as its own slice; serialization
reuses the single-owner pattern from [#1202](https://github.com/danielcopper/decky-romm-sync/issues/1202).

> **Superseded in part by [ADR-0017](0017-client-baseline-detection-authoritative-negotiate-is-transport.md)
> ([#1276](https://github.com/danielcopper/decky-romm-sync/issues/1276)).** The **detection-authority** decision below —
> driving a confirmed non-legacy ROM's sync from `negotiate`'s operation list — is reversed: client-side
> `compute_sync_action` is the sole sync authority for **every** ROM, and `negotiate` is retained as **transport only**
> (per-device session serialization + play-session ingest, its `operations` discarded). Everything else here still holds
> — the negotiate/complete transport, the session lifecycle, exact hash parity, the wizard gate, and the
> `_MIN_REQUIRED_VERSION ≥ 4.9.0` floor. Where this ADR says "detection moves server-side," read ADR-0017.

## Context

RomM shipped a first-party **Device Sync Protocol** in 4.9 (GA 2026-06-12): `POST /api/sync/negotiate` → execute ops →
`POST /api/sync/sessions/{id}/complete`, with `/api/devices` registration. #829 asked whether to adopt it as a client.

The issue's premises were drawn from `docs/developers/device-sync-protocol.md`, which has **drifted hard** from the
shipped 4.9.2 server. Verified against the code (`backend/handler/sync/comparison.py`, `backend/endpoints/sync.py`,
`backend/models/assets.py`):

- **The server detects conflicts but does not resolve them.** `compare_save_state` returns
  `action ∈ {upload, download, conflict, no_op}` + a `reason` string. There is **no** `resolution` field, **no**
  `/resolve` endpoint, and the doc's `keep_both` / `server_wins` / `device_wins` triad does **not** exist in shipped
  code (repo-wide grep: zero hits). Conflict policy is 100% the client's. "Keep both" exists only _by construction_ —
  every slot upload is datetime-tagged (`name [YYYY-MM-DD_HH-MM-SS].ext`), so a conflicting upload becomes another row
  and the human picks in the RomM web UI.
- **Inventory hashing is MD5, not SHA1** — the same algorithm we already use (`adapters/save_file.py`). The residual
  mismatch is narrower: zip saves use an undocumented per-entry scheme (`md5` per sorted entry → `name:hexdigest` lines
  joined `\n` → `md5` of the whole, `assets_handler.py:134-143`) our whole-file hash does not replicate.
- **Slots are the pairing dimension** (`(rom_id, slot)`); the doc's "no slots" is a doc gap. Multi-disc is genuinely
  absent.
- **Multi-file saves** are stored as one zipped Save row with the per-entry hash above — RomM owns the blob's integrity,
  but there is no "save set" model; set recognition + zip/unzip + local-layout mapping stay client-side.

An ownership audit (us vs. the 4.9.2 server) classified each capability:

- **HANDOFF** (server can own): the per-save newest-wins _decision_ (`domain/sync_action.py`), 3-way conflict
  _detection_, the cross-device `is_current` watermark (we never computed it — we only read `device_syncs[].is_current`,
  which is literally the server's `DeviceSaveSync.last_synced_at` surfaced as a boolean), device identity/UUID,
  play-session reporting.
- **SHARED**: byte-identical dedup (#1013), the `(rom_id, slot)` wire mapping, device registration, multi-file zip
  integrity.
- **KEEP** (no server equivalent): conflict _resolution_ (`keep_local` / `use_server` + `.romm-backup` quarantine +
  `STALE_CONFLICT`), the corruption/shrink guard (#1062), the slot data model + first-sync wizard + switch gates,
  multi-disc/multi-file _grouping_, RetroDECK path + RetroArch core + per-system-extension resolution, event-driven
  launch/exit triggers.

The net-deletable code under adoption is small — realistically **~250–500 LOC** (the detection kernel + the
`confirm_download` ack), not the issue's ~1.5k — and even the deleted kernel carries two safety branches (#1062 shrink
guard, #1013 dedup) the 73-LOC server comparator lacks. **The motivation is interoperability and offloading cross-device
bookkeeping, not code reduction.**

The protocol is proven by ≥2 first-party clients (Argosy, Grout) + a third-party wave; Argosy mirrors our exact posture
(content-hash provenance, false-conflict auto-collapse, user prompt only on true divergence). The convergence thrash
(rommapp/romm#3453 / #3448) is genuinely fixed in 4.9.0 — but convergence now rests entirely on cross-client MD5
agreement, and the conflict branch is untested server-side. The clean "pair once → negotiate just works" auth UX
(device-bound tokens + RFC-8628 pairing) is **5.0-only**, absent at 4.9.2.

## Decision

**Adopt the negotiate transport; keep our resolution/slot/path brain layered on top (Option B — hybrid). Require RomM
≥4.9.0 as the hard minimum (`_MIN_REQUIRED_VERSION`) rather than a soft per-server capability gate — a breaking change
taken while still beta (#1234 phase 0b) — and retain the legacy `list_saves` path only for legacy `slot:null` saves
(RomM cannot address `slot:null` through the negotiate inventory param).**

> **0b refinement (supersedes the original "soft ≥4.9 capability gate" framing).** The first cut of this ADR kept
> `_MIN_REQUIRED_VERSION` at 4.8.1 and gated negotiate as a per-server capability, retaining the legacy path for both
> `<4.9` servers and `slot:null` saves. That was reversed: `_MIN_REQUIRED_VERSION` gates the **whole** plugin, so a soft
> gate bought no benefit (≥4.9 users get negotiate either way) while a hard bump only evicts ≤4.8 users — and the legacy
> path cannot be deleted regardless, because `slot:null` still needs it. So the floor is bumped to 4.9.0 and the
> `<4.9-server` capability dimension is dropped; the legacy path survives **only** for `slot:null`.

- **Detection moves server-side.** When a ROM has a non-legacy slot, the sync run is driven by `negotiate`'s operation
  list instead of `compute_sync_action`. The client builds a `(rom_id, slot)` inventory, POSTs it, and executes the
  returned `upload` / `download` / `no_op` ops with the existing executors.
- **Resolution stays the client's.** A `conflict` operation routes into our existing resolution UX (`keep_local` /
  `use_server`) and `.romm-backup` quarantine — the server gives no resolution directive.
- **The wizard gates the inventory.** Only ROMs with `slot_confirmed=True` and a resolved `active_slot` enter the
  `ClientSaveState[]`. An unconfirmed ROM stays out of negotiate entirely until the user passes the first-sync wizard —
  otherwise the server's newest-wins silently overrides our "user decides on ambiguity" invariant.
- **Runs serialize per device.** `negotiate` calls `cancel_active_sessions(device_id)`, so the client holds one
  in-flight save-sync run per device, reusing the single-owner discipline from
  [#1202](https://github.com/danielcopper/decky-romm-sync/issues/1202).
- **Hash parity is non-negotiable.** The client's `content_hash` must match RomM's MD5 exactly, including the zip
  per-entry scheme, or zip/multi-file saves never converge and fall through to the unguarded mtime path
  ([#1235](https://github.com/danielcopper/decky-romm-sync/issues/1235)).
- **Slots survive as a client-side UX dimension** over negotiate's `slot` field; `active_slot` / `source:local` /
  `slot_confirmed` / `default_slot` have no server analog and are unaffected.
- On the negotiate path (the min is now a hard RomM ≥ 4.9.0), the v4.8.1 `confirm_download` / PUT-bump bookkeeping is
  dead and is removed ([#748](https://github.com/danielcopper/decky-romm-sync/issues/748)); `is_current` is read from
  the server watermark.

## Consequences

- **(+)** Interop with the first-party protocol (Argosy/Grout, web/EmulatorJS); the server becomes the authoritative
  source of cross-device sync bookkeeping; the `confirm_download` ack and PUT-bump tricks die.
- **(−)** New surface to build and maintain: device-id threading into negotiate, the session lifecycle, per-device
  serialization, the `slot:null` legacy fallback, and exact zip-hash parity. Net code change is roughly flat — small
  deletions offset by new wiring.
- **(−)** Breaking change: the `_MIN_REQUIRED_VERSION` bump to 4.9.0 evicts ≤4.8 servers from the whole plugin (not only
  save sync). Acceptable while pre-1.0/beta; surfaced as a `BREAKING CHANGE` in the release notes.
- **Reversible:** the floor can be lowered again and the detection kernel restored if the protocol regresses; the legacy
  path stays on disk for `slot:null` regardless.
- **A real existing blocker, fixed first (phase 0a, #1258):** `establish_token` never cleared `device_id` on origin
  change, so a server switch left a stale device id and negotiate hard-404s a foreign `device_id`. Per-origin device
  identity now forgets the id on an origin change (ties into
  [#163](https://github.com/danielcopper/decky-romm-sync/issues/163)).
- **On-device (Game Mode) verification required** — the negotiate round-trip, serialization under rapid Sync/Cancel, and
  zip-save convergence can't be unit-tested alone.
- Multi-file saves _may_ later move to RomM's zip convention (SHARED) to offload integrity and potentially resolve
  [#908](https://github.com/danielcopper/decky-romm-sync/issues/908) version-history-for-multi-file — tracked in
  [#1235](https://github.com/danielcopper/decky-romm-sync/issues/1235), not committed here.

## Alternatives considered

- **A — Full adopt** (delete our brain, lean entirely on the server). Rejected: the server provides no resolution, slot,
  or path equivalent, so we'd rebuild them on top of negotiate anyway — and lose the #1062 corruption guard and #1013
  dedup. A downgrade dressed as a simplification.
- **C — Keep custom until RomM 5.0.** Viable and lowest-cost; forgoes ecosystem interop and lets the `confirm_download`
  workaround linger. **Reconsider-trigger:** if 5.0's device-bound tokens + pairing land in GA (cleans up the auth UX),
  or if RomM ever ships a real server-side resolution primitive (RFC #2199), Option A/full adoption becomes attractive
  and this ADR should be revisited.

## See also

[#829](https://github.com/danielcopper/decky-romm-sync/issues/829) (umbrella eval),
[#163](https://github.com/danielcopper/decky-romm-sync/issues/163) (device auth),
[#748](https://github.com/danielcopper/decky-romm-sync/issues/748) (drop `confirm_download`),
[#1219](https://github.com/danielcopper/decky-romm-sync/issues/1219) (play sessions, separable),
[#1235](https://github.com/danielcopper/decky-romm-sync/issues/1235) (multi-file as zip),
[#1202](https://github.com/danielcopper/decky-romm-sync/issues/1202) (single-owner serialization),
[#794](https://github.com/danielcopper/decky-romm-sync/issues/794) (save-sync v1 hardening). Upstream doc drift worth
reporting to rommapp/docs.
