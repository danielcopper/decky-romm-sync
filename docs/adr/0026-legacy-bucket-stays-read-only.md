# The legacy (slot-less) bucket stays read-only — no opt-in write support

## Status

Accepted. Triggered by [#1478](https://github.com/danielcopper/decky-romm-sync/issues/1478) (legacy saves synced to the
wrong location). Implemented by the triage fixes [#1491](https://github.com/danielcopper/decky-romm-sync/pull/1491) (the
switch gate), [#1494](https://github.com/danielcopper/decky-romm-sync/pull/1494) (the no-device slot-less upload),
[#1496](https://github.com/danielcopper/decky-romm-sync/pull/1496) (the read-only panel) and
[#1507](https://github.com/danielcopper/decky-romm-sync/pull/1507) (content-based wizard migration).

Sharpens — does not supersede — [ADR-0017](0017-client-baseline-detection-authoritative-negotiate-is-transport.md)'s
"`slot:null` is retired as a confirmable target": that retirement is now permanent and enforced on every write path, not
just at slot confirmation.

## Context

RomM stores a save either in a **named slot** or with no slot at all (`slot: null`). The slot-less bucket is where
RomM's own web player (EmulatorJS) writes, and where saves created before slots existed still live. Migration `005`
retired it as a confirmable sync target; it survived only as a one-time migration source.

#1478 reported that legacy saves were being synced "to the wrong location", with the web player and the plugin
disagreeing about the current save. Triaging it surfaced three plugin defects (an open back door into legacy mode, a
wizard migration that silently migrated nothing, and slot-less uploads carrying an emulator tag) — and re-opened the
larger question the report implied: **should the slot-less bucket come back as an explicit, opt-in sync target so that
browser play and Deck play can share saves?**

That question was settled by measuring, against a live RomM 5.0.0 instance and both projects' source, rather than by
reasoning from the plugin's assumptions:

- **The premise for an opt-in was false.** The assumption was "the web player lives exclusively in `slot:null`, so
  interop requires slot-less writes". It does not: when a save is selected on the pre-play screen, "Save & Quit"
  **updates that save in place (PUT), preserving its slot** — including a named slot the plugin owns. Web ↔ Deck interop
  already exists today.
- **Slot-less saves are created by a player UX trap, not by necessity.** Once any savestate exists for a game, the
  pre-play screen defaults to the States tab with **no save file selected**; "Save & Quit" then POSTs a brand-new
  slot-less save that no named-slot client ever sees. A third path loses data entirely: in-game saving followed by the
  plain "Quit" button uploads nothing at all.
- **The slot-less bucket is the least safe place on the server.** It has no write-currency gate (RomM's `add_save` 409
  covers `device` + non-null slot only), `negotiate` excludes it from matching, and two further defects were reproduced
  live and reported as [rommapp/romm#3833](https://github.com/rommapp/romm/issues/3833): a slot-less upload's filename
  lookup could adopt — and overwrite — a row in a **named** slot, and the upsert wrote content to a new path without
  updating the row's `file_path`, leaving `content_hash` describing bytes the row no longer served.
- **Capability was never the blocker.** `compute_sync_action` is entirely slot-agnostic; it decides on baselines, hashes
  and `device_syncs`, never on a slot. The plugin could detect and resolve conflicts in the slot-less bucket today. What
  it lacks is a safe **write** path there — which is a server property, not a kernel one.
- **The root causes are being fixed upstream.** RomM merged [#3831](https://github.com/rommapp/romm/pull/3831) (the
  player keeps the origin save bound and surfaces slot names) and opened
  [#3846](https://github.com/rommapp/romm/pull/3846) for the server-side desync. Both remove the causes at their source,
  for every client, rather than per-client mitigation.

## Decision

**The plugin never writes to the slot-less bucket.** It stays exactly two things:

1. **A migration source.** The setup wizard copies the newest legacy save per canonical target into a named slot
   (content-based since #1507); the source rows are left in place.
2. **Read-only visible.** The SAVES tab lists the bucket last, muted, with a note that it belongs to the web player — no
   activate, no delete, and both refusals are enforced in the backend as well, not just hidden in the UI.

No opt-in setting, no PUT transport for slot-less writes, no client-side currency gate substituting for the missing
server one. Consequently the invariant "every automatic upload POSTs `overwrite=false`" needs no carve-out, and the
device-registration guard makes a slot-less upload unreachable even when no device id is available.

## Consequences

**Accepted costs.** A browser session that ends as a slot-less stray (the v1 player, or the v2 player with no origin
save bound) is visible to the user in the read-only panel but cannot be adopted with one click; recovering it means
downloading the save in RomM's web UI, placing it in the Deck's saves directory, and letting the next sync upload it.
Until RomM ships the fixed player, the States-tab trap remains, so reliable Web ↔ Deck interop asks the user to pick
their save explicitly before playing in the browser. Pre-existing strays are never cleaned up by the plugin.

**What this buys.** No plugin write path exists into a bucket that has no server-side write protection, so the TOCTOU
window such a path would carry (list → decide → write, with no conditional-write primitive to close it) never opens. The
upload invariant stays universal, the failure surface stays inside paths the 409 backstop covers, and no toggle state
space (on / off / mid-migration / stale opt-in) enters the save-sync engine.

## Alternatives considered

**Explicit opt-in with a client-side guard funnel.** Fully designed before rejection: a per-ROM opt-in flag, `PUT`
in-place on the bucket head instead of the filename-upserting `POST`, and a funnel around every write (client currency
gate, re-list immediately before writing with refuse-on-drift, server-head backup, post-write verification), plus a CI
check binding all writes to that funnel. Rejected on cost/benefit rather than feasibility: it is a large change to the
most safety-critical code in the plugin, it needs a declared exception to the upload invariant, its residual race is
unclosable from the client — and its motivating use case is being fixed upstream. The design is recorded here rather
than in the repository because it is not intended to be built as specified; if it is ever revisited, the server side
should be re-measured first, since a conditional-write primitive would change the analysis.

**Aliasing the slot-less bucket into a named one** (the approach the Grout client takes: read slot-less saves under a
name, write named). Rejected: it mixes the buckets, which contradicts the separation this project relies on, and it does
not actually solve interop — the web player still reads slot-less, so named writes stay invisible to it.

**A one-click "adopt this stray into my slot" gesture** (read-only detection plus a user-confirmed copy into the active
named slot). Rejected as a new UI concept for a shrinking problem: the emulator core that produced a stray is not
guaranteed compatible with the slot's core, the correct emulator tag for the copy is ambiguous, and the manual recovery
path already exists.

**Doing nothing.** Rejected — the three #1478 defects were real and are fixed regardless of this decision.

## Revisit trigger

Reopen if RomM's fixed player does not ship (leaving stray creation a permanent property of browser play) **and** there
is real demand for browser ↔ Deck save sharing, **or** if RomM adds a conditional-write primitive for slot-less saves,
which would remove the unclosable race that decided this.
