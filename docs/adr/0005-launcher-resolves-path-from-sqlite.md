# Launcher resolves the ROM path dynamically from SQLite, not from baked shortcut `launch_options`

## Status

**Superseded by [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md).**
The gate this ADR set — #827 proving `SetAppLaunchOptions`-on-existing reliable —
passed, so the "dumb exec wrapper / path baked into `launch_options`" end state
(#785) landed and the interim DB-read launcher described below is retired. This
record is kept for the history of why the interim existed.

Originally accepted as the **interim** launcher design shipped with the cutover
([#784](https://github.com/danielcopper/decky-romm-sync/issues/784)). Revisited
the "dumb exec wrapper / path baked into `launch_options`" approach locked in
[#785](https://github.com/danielcopper/decky-romm-sync/issues/785), which was
**deferred, not rejected** — it stayed the intended end state, gated on the
hardware test in [#827](https://github.com/danielcopper/decky-romm-sync/issues/827).

## Context

`bin/romm-launcher` is the exe Steam runs for every RomM shortcut. Today it
parses `romm:<rom_id>` from `launch_options`, reads `state.json` via an inline
`python3` heredoc, looks up `installed_roms[<id>].file_path`, and execs
`flatpak run net.retrodeck.retrodeck "<path>"`. The path is resolved
**dynamically at launch time**, not stored in the shortcut.

The cutover deletes `state.json`. So the launcher must change — it is the one
**out-of-process** reader of the state being migrated; every in-process reader
is handled by the service refactor.

Issue #785 proposed making the launcher a pure exec wrapper: bake the resolved path
into `launch_options` at **download-complete** and have the launcher just `exec`
what it's handed. Two facts, established by investigation, undercut that as the
*near-term* design:

- **Updating an existing shortcut is documented-unreliable.**
  `docs/architecture/steam-non-steam-shortcuts.md` and `CLAUDE.md` both state
  that `Set*` / `SetAppLaunchOptions` on an already-existing shortcut "may not
  take effect reliably; the workaround is delete + recreate." Shortcuts are
  created at **sync** time (before download), so writing the path at
  download-complete is exactly an update-to-existing — the unreliable path. The
  claim is documented but **never empirically validated** (no hardware test, no
  Steam version note). #785's own body flags this as the central open risk.
- **Dynamic resolution gives free path propagation.** A RetroDECK-home
  migration rewrites install paths and the launcher picks up the new path on the
  next launch with **zero shortcut updates**. Baking would force a re-resolve
  pass that rewrites every affected shortcut's `launch_options` — N more
  unreliable updates, and behaviour the code deliberately does not do today.

The dynamic-resolution launcher was a deliberate choice to avoid unreliable
shortcut mutation. The cutover should preserve that property, not trade it for
an unvalidated mechanism — while leaving #785's vision reachable if the
mechanism proves reliable.

## Decision

The launcher **keeps resolving the path dynamically at launch**, swapping its
source from `state.json` to the SQLite database:

```sql
SELECT file_path FROM rom_installs WHERE rom_id = ?
```

opened **read-only** (`file:<db>?mode=ro`) — WAL allows an external reader to
query the last committed snapshot while the plugin writes, without blocking. The
launcher locates `<runtime_dir>/romm_sync.db` the same way it locates
`state.json` today. `launch_options` **keeps carrying `romm:<rom_id>`**, so
ownership detection (scanning for `romm:`) is unchanged and no shortcut is ever
updated — at download, at migration, or otherwise.

This source-swap ships **inside the cutover (#784)** — the launcher is migrated
like every other reader of the state being moved. Consequently **#784 no longer
depends on #785**: the launcher keeps working the moment `state.json` is
deleted, with no broken-launch window.

### Gated end state

The "dumb exec wrapper" (#785) — path baked into `launch_options`, launcher
resolves nothing — remains the **intended target**, contingent on #827 proving
`SetAppLaunchOptions`-on-existing reliable on real hardware:

- **Reliable** → #785 proceeds; the interim DB read is removed in favour of the
  baked path; the launcher becomes resolution-free.
- **Unreliable / flaky** → the DB-read launcher is the **permanent** design;
  #785 shrinks to the `bin/rom-launcher` rename plus the #129 groundwork.

## Consequences

- The launcher is coupled to the `rom_installs` schema (`file_path`, `rom_id`).
  This is **no worse than today** — it is already coupled to the `state.json`
  shape (`installed_roms[<id>].file_path`); a boundary reader must read
  *something*. ADR-0003's decoupling intent was about the *services*, not this
  external launcher.
- Free path propagation on RetroDECK-home migration is preserved (migration
  updates `rom_installs.file_path`; the launcher reads it next launch).
- The interim launcher code is throwaway-ish if #827 passes and the dumb wrapper
  lands — accepted as cheap insurance (~a few lines of inline `python3`).
- **#129 (multi-emulator) directional note:** if the DB-read launcher becomes
  *permanent* (test fails), emulator selection tends to drift into the launcher
  alongside path resolution. A hybrid — path from the DB, emulator invocation
  still passed as data via `launch_options` — keeps selection at the
  shortcut-build call site. Not a blocker, and only relevant in the test-fails
  branch.

## Alternatives considered

- **Bake the resolved path into `launch_options` at download-complete (the #785
  locked design).** Deferred, not adopted now: it relies on
  `SetAppLaunchOptions`-on-existing, which is documented-unreliable and
  unvalidated, and it forces an N-shortcut re-resolve pass on home migration.
  Revisit after #827. If reliable, it is the cleaner end state (resolution-free
  launcher, no DB coupling, natural fit for #129).
- **Hold #784's `state.json` deletion until #785 lands.** Rejected: recreates
  the #784→#785 dependency and leaves the cutover not self-contained. The
  source-swap is small and keeps the cutover whole.

See also: [ADR-0003](0003-json-sqlite-persistence-boundary.md) (persistence
boundary), [ADR-0004](0004-sync-sqlite-unit-of-work.md) (sync UoW).
