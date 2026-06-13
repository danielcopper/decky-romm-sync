# Launcher is a pure exec wrapper; the launch command is baked into the shortcut's `launch_options`

## Status

Accepted. **Supersedes [ADR-0005](0005-launcher-resolves-path-from-sqlite.md)**, the interim DB-read launcher shipped
with the cutover. ADR-0005 left the "dumb exec wrapper"
([#785](https://github.com/danielcopper/decky-romm-sync/issues/785)) as the intended end state, **gated on**
[#827](https://github.com/danielcopper/decky-romm-sync/issues/827) proving `SetAppLaunchOptions`-on-existing reliable.
#827 passed; this ADR records the end state landing.

## Context

ADR-0005 kept the launcher resolving the ROM path **dynamically at launch** — parsing `romm:<rom_id>` from
`launch_options`, reading `SELECT file_path FROM rom_installs WHERE rom_id = ?` from the SQLite database opened
read-only, then exec-ing RetroDECK. That was a deliberately conservative interim: it preserved free path propagation on
RetroDECK-home migration and avoided mutating existing shortcuts, because the documentation at the time claimed `Set*` /
`SetAppLaunchOptions` on an already-existing shortcut "may not take effect reliably." That claim was **documented but
never empirically validated** — ADR-0005 flagged it as the central open risk and deferred the exec-wrapper design until
it could be tested.

[#827](https://github.com/danielcopper/decky-romm-sync/issues/827) tested it on real hardware. `SetAppLaunchOptions` on
an existing shortcut proved **reliable** across all three scenarios that mattered: in-session (set + read-back in the
same Steam session), across a Steam restart (the value persists), and under removal-churn (re-syncing a library that
adds and removes shortcuts in the same pass). The one observed hazard is unrelated to the set itself — heavy
removal-churn can corrupt Steam's in-memory shortcut state, but a Steam restart clears it. With the reliability question
answered, the interim DB-read launcher no longer has a reason to exist.

The other ADR-0005 finding — that a Steam shortcut's `appId` is derived from `exe + appName` (CRC32) — turns out to
**support** the baked design rather than threaten it. Because the `appId` is a function of `exe + appName` only,
mutating `launch_options` (or `startDir`) on an existing shortcut is **appId-safe**: the shortcut keeps its identity,
its artwork, its collection membership, and its `roms.shortcut_app_id` binding. Only changing `exe` or `appName` is
destructive (it yields a different `appId`, i.e. a different shortcut), and the launch command lives entirely in
`launch_options` — never in `exe` or `appName` — so re-resolving a path never disturbs the binding.

## Decision

The launcher becomes a **pure exec wrapper**. `bin/rom-launcher` (renamed from `bin/romm-launcher`) is:

```bash
exec "$@"
```

It owns no state, no path resolution, and no emulator knowledge. Steam hands it the full launch command as the
shortcut's launch options and it runs exactly that.

The launch command is **baked into the shortcut's `launch_options`** as `<emulator-invocation> "<resolved-rom-path>"`.
The emulator invocation is a build-time value rendered by the pure seam
`domain/shortcut_data.resolve_emulator_invocation`, returning RetroDECK's `flatpak run net.retrodeck.retrodeck` today.
That seam **renders** a chosen invocation; it does not **select** one. The per-ROM _selection_ (whether this ROM has a
per-game emulator/core override, and which) is a **service-layer DB read** —
[ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md)'s `ActiveCoreResolver.active_core_for_rom(rom_id)`,
backed by `roms.emulator_override` — that resolves the core and passes it into the seam. The seam only turns the chosen
core into a command string. Multi-emulator support ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129))
extends this single seam, not the launcher. The ROM path is double-quoted so paths with spaces survive `exec "$@"`, and
any embedded `\` and `"` in the path are backslash-escaped (backslash first, then quote) so a server-controlled ROM
filename cannot break out of the quoted token and inject extra argv elements into the emulator invocation through
Steam's launch-options tokenizer. Only the path is escaped — the emulator invocation itself is trusted build-time text
whose own `-e "..."` quoting must survive verbatim. `launch_options` carries:

- the **full command** for an installed ROM, written at **sync** (for ROMs already on disk), at **download-complete**
  (the moment a ROM becomes installed), and **re-resolved on RetroDECK-home migration** (a new
  `migration_relaunch_options` event rewrites every installed+bound shortcut to the relocated path);
- the **empty string `""`** (placeholder) for an uninstalled ROM, until it is downloaded.

The `romm:<rom_id>` marker is **gone**. Two bindings that previously rode on it move off `launch_options`:

- **Ownership detection** — a RomM-managed shortcut is now identified by its **exe path** ending in `/bin/rom-launcher`,
  not by scanning `launch_options` for `romm:`.
- **rom_id ↔ appId** — resolved through the backend's authoritative `get_app_id_rom_id_map()` (the
  `roms.shortcut_app_id` binding), not parsed from `launch_options`.

A shortcut is treated as RomM-owned only when **both** hold: its exe ends in `/bin/rom-launcher` **and** its appId is
bound in the backend map. After a DB reset the backend map is empty, so our shortcuts are detected by exe but unmapped —
treated as orphans, and re-sync recreates them.

Every `SetAppLaunchOptions` on an existing shortcut uses a **fire-then-poll confirm**: set the value, then poll
`RegisterForAppDetails` until the read-back `strLaunchOptions` matches (confirming `""` against an empty read-back is
valid), or time out. The Set is no longer a fire-and-forget with an assumed result.

### Per-game emulator/core override — the `-e` invocation form

A ROM with a per-game emulator/core override carries a different invocation in the same `launch_options` field. The seam
renders RetroDECK's `-e` flag, which sets the emulator invocation directly and bypasses RetroDECK's gamelist lookup, so
the override applies regardless of filename:

```text
flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/<core>.so %ROM%" "<rom-path>"
```

`%EMULATOR_RETROARCH%` and `%ROM%` stay as ES-DE placeholders (RetroDECK resolves and single-quotes them at launch);
only the in-sandbox cores directory is baked literally. A ROM **without** an override keeps the plain
`flatpak run … "<rom-path>"` — `-e` is added only for overrides. The override LABEL is stored in
`roms.emulator_override` and resolved to its `.so` by the service-layer read; the seam never reads the DB. The full
storage + precedence + bake-site model is [ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md).

## Consequences

- The launcher is **resolution-free** — no DB coupling, no inline `python3`, no `rom_installs` schema dependency. The
  boundary reader that ADR-0005 had to keep is gone.
- The launch path is **data** in `launch_options`, computed at the shortcut-build call site. This is the natural home
  for emulator selection too, so [#129](https://github.com/danielcopper/decky-romm-sync/issues/129) (multi-emulator) and
  [#865](https://github.com/danielcopper/decky-romm-sync/issues/865) (disc-switch) are **non-breaking**: they change
  only the `launch_options` data and the build-time invocation resolver, never the launcher binary or the shortcut
  identity.
- Free path propagation on RetroDECK-home migration is **no longer automatic** — the migration now re-resolves and
  rewrites each installed+bound shortcut's `launch_options` (the `migration_relaunch_options` event). This is the
  N-shortcut update pass ADR-0005 wanted to avoid; #827 proved it reliable, so it is now an acceptable cost, and the
  per-shortcut confirm makes a silently dropped write observable.
- **Breaking for existing shortcuts: a re-sync is required.** Shortcuts created under the `romm:<rom_id>` model carry
  the old marker and no baked command; they are detected by exe but recreated by re-sync to pick up the baked
  `launch_options`. Accepted as a **pre-release** breaking change.
- This **supersedes ADR-0005**. The interim DB-read launcher and the `romm:<rom_id>` marker are retired; ADR-0005 is
  kept as the record of why the interim existed and what gate (#827) had to clear before this design could land.

## Alternatives considered

- **Keep the DB-read launcher permanently** (the ADR-0005 test-fails branch). Rejected: #827 proved
  `SetAppLaunchOptions`-on-existing reliable, so the conservatism that justified the interim no longer applies. The
  DB-read launcher keeps the launcher coupled to the `rom_installs` schema and tends to pull emulator selection into the
  launcher alongside path resolution — the opposite of where #129 wants that decision to live.
- **Bake the path but keep the `romm:<rom_id>` marker for ownership.** Rejected: the marker and the baked command would
  both occupy `launch_options`, and the exe path already uniquely identifies our shortcuts. Ownership-by-exe plus the
  backend `get_app_id_rom_id_map()` binding is the cleaner split — the exe says "ours," the backend map says "which
  ROM."

See also: [ADR-0005](0005-launcher-resolves-path-from-sqlite.md) (superseded interim),
[ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (`RomInstall` launch `file_path` — the path baked into the
command), [ADR-0003](0003-json-sqlite-persistence-boundary.md) (persistence boundary).
