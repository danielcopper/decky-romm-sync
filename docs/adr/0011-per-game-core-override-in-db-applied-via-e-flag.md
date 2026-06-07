# Per-game emulator/core override lives in the plugin DB and is applied via RetroDECK's `-e` flag; we read external config but never write it

## Status

Accepted. **Supersedes the per-game gamelist-override mechanism** shipped by
[#864](https://github.com/danielcopper/decky-romm-sync/issues/864) /
[#942](https://github.com/danielcopper/decky-romm-sync/pull/942) (closed as the wrong layer). Extends
[ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options` model) with the `-e`
override form and **corrects the "foundation for multi-emulator" framing** in
[ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) §Consequences and
[ADR-0010](0010-normalize-romm-slug-to-retrodeck-system.md) §Consequences: RetroDECK is the V1 target, not an MVP
stepping-stone. Tracked under epic [#945](https://github.com/danielcopper/decky-romm-sync/issues/945).

## Context

A user can pick a non-default RetroArch core for a single game (e.g. _Beetle PSX_ for one PS1 title while the rest of
the platform stays on the default). The first attempt (#864/#942) stored that choice the way ES-DE does — by writing a
per-game `<altemulator>` element into ES-DE's `gamelist.xml`. Real-device testing on a Steam Deck disproved the premise
behind that approach:

- **The plugin's launch path is not ES-DE's launch path.** Per
  [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md), the plugin bakes
  `flatpak run net.retrodeck.retrodeck "<rom-path>"` into the Steam shortcut. At launch RetroDECK's `run_game.sh` must
  look the ROM up in `gamelist.xml` to find a per-game `<altemulator>` — and it does that lookup with an **awk `~` regex
  match** of the ROM path. Any regex metacharacter in the filename (`(USA)`, `(Disc 1)`, `[!]`, …, i.e. nearly every
  real ROM dump) breaks the match, the override is silently ignored, and the game launches on the default core. This is
  upstream bug [#210](https://github.com/danielcopper/decky-romm-sync/issues/210) /
  [RetroDECK#1358](https://github.com/RetroDECK/RetroDECK/issues/1358), confirmed against the live `run_game.sh` logic.
  The override the user saw "work" applied only because they had launched that title from **ES-DE's own UI**, which
  resolves `<altemulator>` itself and bypasses the awk — a path the plugin's shortcut never takes.
- **Writing another system's config is fragile.** `gamelist.xml` is ES-DE's file, in ES-DE's format, on ES-DE's update
  cycle. When a system-wide alternative emulator is set, ES-DE writes `<alternativeEmulator>` as a sibling of
  `<gameList>` at the document root — invalid multi-root XML that ES-DE tolerates (line-based awk + `xmllint --recover`)
  but the plugin's strict expat parser rejects, so per-game writes failed silently on exactly the platforms most likely
  to have an override.

RetroDECK's `run_game.sh` also accepts an **`-e` flag** (`-e "<command-with-placeholders>"`) that sets the emulator
invocation directly and **skips the gamelist awk lookup entirely**. Baking the resolved core into that flag was verified
end-to-end on device: the override applies regardless of filename, because the metacharacter-sensitive lookup is never
reached.

## Decision

### 1. Storage: the override lives in the plugin's own DB, as a LABEL, on the `Rom` aggregate

A nullable `roms.emulator_override` column (migration `002_add_emulator_override.sql`) holds the **core LABEL** the user
picked (e.g. `"Beetle PSX HW"`). `NULL` means "no override — follow the RetroDECK/ES-DE default." It anchors on `roms`
(not `rom_installs`) so the choice **survives uninstall/reinstall**, per
[ADR-0007](0007-rom-retention-identity-anchor.md). Mutations go through verb-named aggregate methods
`pin_emulator_override(label)` / `clear_emulator_override()`; only `pin`/`clear` ever write the column — it is
**excluded from the sync UPSERT `SET` clause**, so a re-sync never wipes a user's pin.

We store the **override (the deviation the plugin owns)**, not a resolved `active_core`. The default and system layers
are owned by RetroDECK/ES-DE and change externally (a RetroDECK update can ship a new default core); a stored resolved
value would go stale. `NULL` is precisely the "this ROM has no deviation — bake the plain launch" signal. The LABEL is
resolved to its `.so` filename through the same es_systems `available_cores` map at use time, never stored.

### 2. Application: bake the `-e` override into `launch_options` — only for ROMs that have an override

Per [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md), the launcher is a pure `exec "$@"` wrapper and
the full command lives in the Steam shortcut's `launch_options`. For a ROM with an override the command carries
RetroDECK's `-e` flag:

```text
flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/<core>.so %ROM%" "<rom-path>"
```

`%EMULATOR_RETROARCH%` and `%ROM%` stay as ES-DE placeholders — `run_game.sh` resolves and single-quotes them at launch,
so a ROM path with spaces or parens is handled. Only the in-sandbox cores directory (`/var/config/retroarch/cores`) is
baked literally; ES-DE's `%CORE_RETROARCH%` variable is **not** expanded via `-e`, so the plugin must bake the resolved
path itself. A ROM with **no** override (`NULL`) keeps the plain `flatpak run … "<rom-path>"` — `-e` is added only for
overrides, which keeps the blast radius minimal and preserves RetroDECK's default-emulator behaviour everywhere the user
has not made a per-game choice.

### 3. One read seam; read-path core == launched core

A single service-level resolver, `ActiveCoreResolver.active_core_for_rom(rom_id) -> (core_so, label)`, answers "which
core will this ROM actually launch with?" Its precedence is the invariant:

> **DB `emulator_override` (top) → system `<alternativeEmulator>` → es_systems default → `core_defaults`.**

Every per-game core read consumer (BIOS-requirement filtering, save-directory corename, save-sync core tag, core-change
detection, the game-detail core badge) **and** all three bake sites draw from this one seam, so the launch core never
diverges from the BIOS core, the per-core save path, or the core-change warning. A pinned label that no longer resolves
(a core removed by a RetroDECK update) degrades to the system-layer result on the read path and to the plain launch on
the bake path — a stale override is never fatal and never produces a bogus `.so`.

### 4. We read external config but never write it for per-game state

The plugin **reads** RetroDECK/ES-DE configuration it does not own — `es_systems.xml` (defaults + available cores), the
system-level `<alternativeEmulator>` in `gamelist.xml`, `retrodeck.json` paths. Those reads are legitimate: the data is
authoritative in its own source and has no equivalent in the plugin's DB. The plugin **does not write** the gamelist for
per-game override state; that state is the plugin's own and lives in the plugin's own DB. The one remaining write into
ES-DE's config is the **system-level** `<alternativeEmulator>` (`set_system_override`, behind the System-page per-system
core dropdown) — a deliberate, single-element, system-scoped write that the user explicitly drives, kept because a
system-wide core choice is genuinely an ES-DE-level setting that ES-DE-native launches must also honour.

### 5. RetroDECK is the V1 target, not an MVP stepping-stone

The `-e` flag, the `%EMULATOR_RETROARCH%` / `%ROM%` placeholders, and the `/var/config/retroarch/cores` path are all
**RetroDECK-adapter concerns**, and they live at the single pure seam
`domain/shortcut_data.resolve_emulator_invocation`. This corrects the "foundation for multi-emulator support" framing in
[ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) and
[ADR-0010](0010-normalize-romm-slug-to-retrodeck-system.md): RetroDECK is the supported launcher for V1, and the
RetroDECK-specific invocation is correctly RetroDECK-shaped, not a generic placeholder. The multi-emulator lift
([#129](https://github.com/danielcopper/decky-romm-sync/issues/129) /
[#918](https://github.com/danielcopper/decky-romm-sync/issues/918)) is a **near-mechanical extraction** of that one seam
into a RetroDECK adapter behind a `Frontend`-style port — but only the extraction is mechanical. A sibling emulator
(e.g. EmuDeck) needs net-new per-emulator argv: the chenasraf EmuDeck fork proved the port shape but its
`launch_command()` is a `NotImplementedError` stub, so the hard part (each emulator's launch argv) is unsolved. The port
is therefore **not built now** — a one-implementation Protocol is indirection without payoff; the seam stays dormant.

## Consequences

- **The override works for any filename via the plugin's shortcut.** Baking `-e` bypasses the metacharacter-sensitive
  awk lookup (#210), so `Final Fantasy VII (USA)` gets its pinned core exactly like `Tetris.gb` does. The plugin no
  longer depends on an upstream RetroDECK fix.
- **No gamelist writes for per-game state** moots the multi-root-XML parse failure and the
  silent-success-on-write-failure bug that the gamelist-write path suffered — there is no per-game gamelist write to
  fail. The ES-DE folder-collapse quirk ([#943](https://github.com/danielcopper/decky-romm-sync/issues/943)) is
  downgraded to a cosmetic ES-DE-native display concern, decoupled from whether the override functions.
- **The override survives uninstall/reinstall.** Anchoring on `roms` means a download-complete re-bake (the third bake
  site) re-applies the pin without the user re-picking — the exact path `roms` storage was chosen to protect.
- **Three bake sites, one seam.** `launch_options` is (re)written at sync, on download-complete, and on RetroDECK-home
  migration; each resolves the override through the same path-rendering seam, so they cannot diverge.
- **A core a stale pin points at is handled gracefully.** Read-path degrades to the system default + WARNING; bake-path
  emits the plain launch + WARNING; set-path hard-fails before writing (an unresolvable label is never persisted).
- **No migration from the old model.** Per-game overrides previously written to `gamelist.xml` are not imported — there
  is deliberately no gamelist-import migration (it would revive the multi-root parse failure and the folder-collapse
  ambiguity). Users re-apply per-game cores through the plugin UI once. See the user-facing note in
  [BIOS and Emulator Cores](../user-guide/bios-management.md#per-game-cores-do-not-migrate).
- **Multi-emulator stays cheap to reach.** Because `-e` construction is isolated at one domain seam and the per-ROM
  _selection_ is a separate service-layer DB read, #129/#918 changes only the seam's rendering plus net-new per-emulator
  argv — never the launcher binary, the shortcut identity, or the resolver.

## Alternatives considered

- **Write the per-game `<altemulator>` into ES-DE's `gamelist.xml`** (the #864/#942 mechanism). Rejected: the plugin's
  shortcut launch hits RetroDECK's awk regex lookup, which breaks on filename metacharacters (#210) and silently drops
  the override for nearly every real ROM dump; and writing another system's strict-parser-hostile config is fragile
  (multi-root XML, silent write failures). It intrudes into a config the plugin does not own to store state the plugin
  _can_ own.
- **Always bake `-e` for every ROM** (resolve and pin the active core unconditionally). Rejected: it freezes the default
  core into every shortcut, so a later RetroDECK default change no longer takes effect without a re-sync, and it
  maximises intrusion — every launch command carries a `-e` override even when the user made no choice. `-e`-only-for-
  overrides keeps RetroDECK's default-emulator behaviour live for the common case.
- **Store a resolved `active_core` (`.so`) instead of the override LABEL.** Rejected: the resolved value is owned by
  layers the plugin does not control (the es_systems default, the system `<alternativeEmulator>`), which change
  externally; a stored resolved value goes stale on a RetroDECK update. Storing only the deviation (the LABEL, `NULL`
  when there is none) keeps the plugin authoritative over exactly the slice it owns and re-resolves the rest live.
- **Build the multi-emulator `Frontend` port now.** Rejected: RetroDECK is the only inhabitant for V1; a
  one-implementation Protocol is indirection without payoff, and the genuinely hard part (per-emulator launch argv) is
  net-new work the deferred seam does not solve. The seam stays dormant until a second emulator is concrete.

See also: [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (baked `launch_options` + the `-e`
section), [ADR-0007](0007-rom-retention-identity-anchor.md) (`roms` as the identity anchor the override survives on),
[ADR-0010](0010-normalize-romm-slug-to-retrodeck-system.md) (platform→`system` normalization feeding the resolver),
[Core and Emulator Selection](../architecture/core-emulator-selection.md) (the resolver, bake sites, and read seam in
detail).
