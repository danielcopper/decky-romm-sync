# The live `es_systems.xml` is the sole source for per-system emulator resolution; the curated `core_defaults.json` snapshot is deleted

## Status

Accepted. **Refines [ADR-0012](0012-plugin-owns-core-selection-always-e-no-gamelist.md):** §3's precedence chain loses
its bottom `core_defaults.json` layer, and the "es_systems default" it resolves changes meaning from _the first
RetroArch libretro command_ to _the first safely-bakeable command in document order_ (which may itself be a standalone
emulator). It also **completes** the two follow-ups ADR-0012 flagged as out of scope: the emulator picker now lists
**standalone** emulators alongside libretro cores, and per-game / per-platform pins may name either. Builds on the
standalone-emulator seam ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129), epic
[#914](https://github.com/danielcopper/decky-romm-sync/issues/914)). ADR-0011 §1 (the per-game override is a LABEL on
the `Rom` aggregate), the always-`-e` bake ([ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md)), and
the never-touch-the-gamelist ownership rule (ADR-0012) are unchanged. Tracked under
[#1210](https://github.com/danielcopper/decky-romm-sync/issues/1210).

## Context

ADR-0012 made the plugin the sole owner of core selection for its own launches and left the system-layer default coming
from two places: the live `es_systems.xml` (ES-DE's own system definitions, read from the RetroDECK flatpak) **and** a
bundled `defaults/core_defaults.json` snapshot, consulted as an offline fallback and as the home for a hand-curated
`standalone` block per system (PS2 → PCSX2, PS3 → RPCS3). A generator script (`scripts/generate_core_defaults.py`) built
that snapshot _from_ a captured `es_systems.xml`.

Three problems came out of living with that snapshot:

1. **It is a strict subset of the thing it shadows.** The snapshot is generated from `es_systems.xml`, so it can never
   contain a system, command, or emulator the live file does not already have. When the live file is readable the
   snapshot adds nothing; when it is not, the snapshot is stale by exactly however long it has been since the last
   regeneration.

2. **The fallback can never help a launch.** RetroDECK is a hard prerequisite — a ROM launches _into_ RetroDECK via
   `flatpak run net.retrodeck.retrodeck …`. If RetroDECK is not installed, `es_systems.xml` is unreadable **and** there
   is no emulator to launch into. So the case the offline snapshot exists to cover (no live file) is exactly the case
   where nothing can launch anyway. The fallback data buys a resolved core for a launch that cannot happen.

3. **Standalone selection was a curation surface.** A system reached a standalone emulator only if someone had
   hand-added a `standalone` block naming the ES-DE label, because the old resolver picked _the first RetroArch libretro
   command_ as the default — even when ES-DE lists a working standalone first and the libretro core is deprecated or
   absent. Every new standalone system (Switch, Wii U, …) was a manual data edit plus the freshness-guard premise that
   the curated string still matched the live one.

The live `es_systems.xml` already carries everything the snapshot did: it lists **every** `<command>` per system in
ES-DE's own preference order, libretro and standalone alike. What was missing was a rule for deciding which of those
commands the plugin can safely bake into a Steam shortcut's `-e` override — and that rule, once written, makes the
curated snapshot redundant.

## Decision

### 1. The live `es_systems.xml` is the only source; `core_defaults.json` and its generator are deleted

`defaults/core_defaults.json` and `scripts/generate_core_defaults.py` are removed. `CoreResolver`
(`adapters/es_de_config.py`) resolves everything from the live `es_systems.xml` it locates in the RetroDECK flatpak
install (linux/ preferred, unix/ fallback, within each flatpak root). There is no offline branch and no bundled data.

When `es_systems.xml` cannot be found or parsed, the adapter reports **"unavailable"** rather than inventing a fallback:
`get_emulator_options(system)` returns `{"available": False, "options": []}`, `get_default_emulator` returns `None` (the
caller bakes the plain RetroDECK launch), and the picker surfaces "Emulator list unavailable" (one INFO log, no data).
This is the "RetroDECK is a hard prerequisite" fact made structural — the plugin never pretends to know a system's
emulator when it cannot read the file that defines it.

### 2. The system default is the first _safely-bakeable_ command in document order

A new pure kernel `domain/emulator_commands.py` classifies one ES-DE `<command>` (its label + text) into an
`EmulatorOption` value object — `label`, `kind` (`"libretro"` / `"standalone"`), `core_so`, `command`, `status`
(`"bakeable"` / `"needs_setup"` / `"unbakeable"`), and a `reason`. The bake verdict applies these rules in order, first
match wins:

1. contains `%INJECT%` → `needs_setup` (`"inject"`) — needs ES-DE to generate a sidecar first (Vita3K, Xemu);
2. contains `%ENABLESHORTCUTS%` or `%EMULATOR_OS-SHELL%` → `unbakeable` (`"shortcut_script"`);
3. does not end in `%ROM%` → `unbakeable` (`"no_rom_target"`) — trailing args after `%ROM%` break the bake;
4. contains `"` or `\;` → `unbakeable` (`"quoting"`);
5. any placeholder outside the whitelist (`%EMULATOR_*%`, `%ROM%`, `%CORE_RETROARCH%`, `%GAMEDIR%`, `%GAMEDIRRAW%`,
   `%ROMPATH%`, `%BASENAME%`, `%FILENAME%`, `%ROMRAW%`, `%STARTDIR%`) → `unbakeable` (`"unknown_placeholder"`);
6. contains `%STARTDIR%` → `unbakeable` (`"startdir"`) — checked after the whitelist sweep so it surfaces its own
   reason;
7. otherwise `bakeable`.

A leading `env VAR=val …` prefix (the gc/wii Dolphin form) is accepted as a standalone invocation. The kind is
`libretro` when the command matches the strict RetroArch shape
(`%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/<core>_libretro.so %ROM%`, capturing `core_so`), else `standalone`.

`select_default_option` returns **the first `bakeable` option in document order** — ES-DE's own preference order picks
the default, no curation. `get_default_emulator` renders that into the existing `EmulatorInvocation` (a libretro `-L`
form or a standalone command baked verbatim), and `get_emulator_options` ships the full classified list to the picker so
un-bakeable commands are shown disabled with their reason rather than silently dropped.

**The standalone existence probe.** RetroDECK lists more standalone emulators in `es_systems.xml` than it bundles — an
emulator like Ryubing (switch), Eden, or any user-installed external component may be named by a `<command>` but not
present on disk. Baking such a command produces a shortcut that dies in ~0.4 s (the emulator binary is missing), which
is a regression for any system whose new bakeable default is a not-installed standalone (before #1210 those systems
plain- launched and let RetroDECK resolve their emulator). So the adapter probes existence: alongside `es_systems.xml`
it parses the sibling `es_find_rules.xml` (same systems dir, same mtime-cache), resolves each standalone command's
`%EMULATOR_<NAME>%` token to its find-rule entry, and checks whether any of that entry's `staticpath` locations exist on
disk — mapping the sandbox-relative prefixes to their host paths (`/app` → the RetroDECK flatpak `files` tree,
`/var/{data,config}` → the app's `~/.var/app/net.retrodeck.retrodeck` trees, `~` → the user home, glob-aware). The pure
`domain.emulator_commands.downgrade_if_not_installed(option, installed)` rule (adapter passes the verdict in, domain
stays I/O-free) then turns a bakeable **standalone** whose emulator is absent into a `needs_setup` option with reason
`not_installed`, so it drops out of `select_default_option` (the system plain-launches again) and shows disabled in the
picker. Libretro options are never downgraded — RetroArch ships with RetroDECK, always installed.

The probe is honest about its limits and **absence-only** — it never falsely downgrades an emulator it cannot verify. It
reports "not installed" **only** on positive evidence that a RetroDECK-managed component is missing: the emulator has a
`retrodeck/components/…` (bundled) or `retrodeck/external_components/…` (user-installed) `staticpath` and **none** of
its staticpaths exist. It cannot check `systempath` entries (binaries on RetroDECK's own sandbox `PATH`, not visible
from outside the sandbox), so a `systempath`-only emulator — or one whose find rule is absent, or one with only
host-native staticpaths that are missing — is assumed installed. And when `es_find_rules.xml` itself cannot be read,
nothing is downgraded. This keeps the probe purely additive: a normal RetroDECK install classifies exactly as before,
minus the genuinely-missing standalones. A **false-positive** downgrade (marking an installed emulator absent) is
possible only in two shapes that do not occur today: a component-marked standalone that RetroDECK launches purely via a
sandbox `systempath` binary while its `components/<x>/` launcher is absent, or a future find rule that resolves a
component through a `$VAR`-style `staticpath` we do not expand — and both degrade gracefully to a working plain launch,
costing only a wrongly-disabled picker entry, never a broken one.

### 3. The precedence chain drops the snapshot layer; pins may name a standalone

`ActiveCoreResolver.active_emulator_for_rom(rom_id)` is now a **three-layer** chain, then the plain launch:

> **per-game DB `emulator_override` (a LABEL) → per-platform `settings.json` `platform_cores` (a LABEL) → the live
> es_systems default (`get_default_emulator`) → `None`.**

The `core_defaults.json` bottom layer of ADR-0012 §3 is gone. Layers 1 and 2 resolve their LABEL through
`label_to_invocation(options, label)`, so a pin may name a **libretro core OR a standalone emulator** — both resolve
identically, and a label that no longer resolves to a bakeable command warns and degrades to the next layer, uniformly,
never fatal. The read-path projection `active_core_for_rom` still yields `(core_so, label)` for libretro and
`(None, label)` for standalone, so the `.so`-space consumers (BIOS filter, per-core save dir, core-change detection)
degrade on a `None` core exactly as before.

### 4. No storage change, no new callable, no arity change

Both deviations reuse their existing homes: the per-game override stays `roms.emulator_override` (nullable LABEL, **no
schema migration**), the per-platform selection stays the `settings.json` `platform_cores` map (**no rename, no
settings-version bump** — the historical name is kept). `CoreService.set_game_core` now validates the picked LABEL
against the **bakeable** options via `label_to_invocation` first — an unresolvable / `needs_setup` / un-bakeable label
is a hard `{success: False, reason: "core_unavailable", message}` and **nothing is written** — then bakes either a
libretro or a standalone invocation. The service method `get_available_cores` is renamed `get_platform_core_info`; its
payload key `cores` becomes `emulators` and gains `emulator_data_available`, and the `get_firmware_status` per-platform
payload renames `available_cores` → `emulators` and gains `emulator_data_available` (frontend: `AvailableCore` →
`EmulatorOption`; `CoreInfo.cores` → `emulators`). The callable **surface is unchanged** — same names, same arity, 112
callables — so the manifest-parity gate is green without touching `backend.ts` names.

Both pickers share one builder (`src/utils/emulatorMenu.ts` → `buildEmulatorMenu`): the game-detail menu
(`RomMPlaySection`) and the System-page control (`SystemPage`, now a `ButtonItem` that opens the same context menu
instead of a `DropdownItem`). Menu keys are the emulator LABEL. Bakeable entries are clickable, the default is marked
`(default)`, and un-bakeable entries are **disabled** with reason copy — `inject` → "needs setup files (launch via ES-DE
once)", `not_installed` → "emulator not installed", `shortcut_script` → "script/shortcut form", everything else → "not
launchable from Steam". When `emulator_data_available` is `false` the menu shows a single disabled "Emulator list
unavailable — RetroDECK installation not found". The per-game "Use System Override" reset item is unchanged.

## Consequences

- **The default set is data-derived, not hand-maintained.** A RetroDECK update that ships a new emulator for a system
  (or a new system) changes the plugin's default the moment `es_systems.xml` changes — no snapshot regeneration, no code
  edit. This is the whole point of #1210: the plugin follows RetroDECK's own preference order.
- **Standalone systems launch on their working emulator without curation.** A system whose first bakeable command is a
  standalone emulator (Dolphin, PPSSPP, Azahar, Ryubing, Cemu, …) launches on it, even where the libretro core the old
  resolver would have picked is deprecated or absent.
- **The offline snapshot is gone and nothing is lost.** The one case it covered (no live file) is the case where no ROM
  can launch anyway, so its removal changes no reachable launch. RetroDECK-absent now degrades cleanly to "unavailable"
  instead of a stale invented core.
- **A frozen default still needs a Force Full Sync.** Because the resolved default is baked literally into every
  shortcut (ADR-0012), a new RetroDECK default only reaches existing shortcuts on a full re-bake; a normal sync skips
  unchanged platforms. Unchanged from ADR-0012, restated because #1210 makes external default changes more frequent (any
  RetroDECK emulator addition can move a default).
- **A not-installed standalone default degrades to a plain launch, not a dead shortcut.** The `es_find_rules.xml`
  existence probe catches a standalone default whose emulator is missing (Ryubing on a switch that never installed it,
  any un-installed external component) and downgrades it to `needs_setup`/`not_installed`, so the system falls back to
  the next bakeable command or the plain RetroDECK launch — restoring the pre-#1210 behavior for that system instead of
  baking a shortcut that dies in ~0.4 s. The probe is absence-only and cannot see `systempath` binaries inside
  RetroDECK's sandbox, so an emulator installed only that way is (correctly) still treated as available; the residual
  gap is a standalone that is genuinely missing **and** has only a `systempath` find rule, which the probe cannot prove
  absent.

## Alternatives considered

- **Keep the curated `core_defaults.json` snapshot to avoid the default flips.** Rejected. The snapshot is generated
  _from_ `es_systems.xml`, so the live file is a strict superset — the snapshot can only shadow, never add. And because
  RetroDECK is a hard prerequisite, the snapshot's only unique role (a fallback when the live file is unreadable) covers
  exactly the state in which nothing can launch. Keeping it would preserve a maintenance surface (every new standalone
  system is a manual edit) and a freshness-guard premise (the curated command must still match the live one) purely to
  suppress default flips that are, themselves, the corrected behavior.
- **Select on "contains `%ROM%`" instead of "ends with `%ROM%`".** Rejected. A command with `%ROM%` in the middle has
  trailing arguments after the ROM path; baking it into `-e "… %ROM% --extra"` would place those args where RetroDECK's
  `run_game.sh` does not expect them and break the launch. "Ends with `%ROM%`" is the shape the `-e` bake can carry
  verbatim.
- **Treat `%STARTDIR%` as bakeable.** Rejected for v1. RetroDECK's `run_game.sh` parses-but-drops `%STARTDIR%`, so a
  baked command relying on it would launch from the wrong working directory. Surfacing it as un-bakeable (reason
  `"startdir"`) is honest; supporting it needs an upstream fix or a wrapper (see Deferred).

## Deferred (deliberately)

- **`%STARTDIR%` support.** Needs an upstream `run_game.sh` change (honor the directory) or a plugin-side wrapper that
  `cd`s before exec. Until then, `%STARTDIR%` commands are un-bakeable and, if they are a system's only command, the ROM
  plain-launches.
- **`%INJECT%` sidecar generation.** Vita3K / Xemu commands need ES-DE to generate a per-game sidecar first, so they are
  surfaced as `needs_setup` ("launch via ES-DE once") and are not clickable in the picker. Generating the sidecar from
  the plugin is a separate effort.
- **BIOS-badge and standalone save-sync accuracy for standalone-default systems.** A system whose default is now a
  standalone emulator reads `active_core = None` and degrades — the launch works, but the BIOS badge and standalone
  save-sync are separate efforts (the same deferral ADR-0012 recorded, now reachable for more systems).

## The 27 default flips (data-derived from this machine's live `es_systems.xml`)

Switching the system default from _first libretro command_ to _first safely-bakeable command in document order_ changes
the resolved default for **27 systems** on the RetroDECK install this was generated against. They fall in three
categories; the six that move where saves are written are called out because a standalone emulator saves to a different
directory than the libretro core it replaces.

**A. libretro core → standalone emulator (6 — the save-location-moving flips):**

| System | Old default (libretro) | New default (standalone) |
| ------ | ---------------------- | ------------------------ |
| doom   | `prboom_libretro`      | GZDoom (Standalone)      |
| gc     | `dolphin_libretro`     | Dolphin (Standalone)     |
| n3ds   | `azahar_libretro`      | Azahar (Standalone)      |
| pico8  | `retro8_libretro`      | PICO-8 (Standalone)      |
| psp    | `ppsspp_libretro`      | PPSSPP (Standalone)      |
| wii    | `dolphin_libretro`     | Dolphin (Standalone)     |

**B. plain launch (no libretro default) → newly-baked standalone emulator (9):**

| System     | New default (standalone)       |
| ---------- | ------------------------------ |
| coco       | XRoar CoCo 2 NTSC (Standalone) |
| dragon32   | XRoar Dragon 32 (Standalone)   |
| flash      | Ruffle (Standalone)            |
| primehack  | PrimeHack (Standalone)         |
| solarus    | Solarus (Standalone)           |
| switch     | Ryubing (Standalone)           |
| tanodragon | XRoar (Standalone)             |
| triforce   | Dolphin (Standalone)           |
| wiiu       | Cemu (Standalone)              |

**Caveat (the existence probe gates each B flip on the emulator being installed).** Each of these is a flip **only when
the standalone emulator is actually installed in RetroDECK**. The `es_find_rules.xml` existence probe (see Decision §2)
re-checks this per install: on the machine this list was generated against, every emulator above is bundled **except**
Ryubing — RetroDECK ships `switch`'s Ryubing command but not the binary — so `switch` does **not** flip there and stays
a plain launch until the user installs Ryubing. The other eight flip because their emulators (XRoar, Ruffle, PrimeHack,
Solarus, Dolphin, Cemu) are RetroDECK-bundled and resolve on disk. A later RetroDECK update that bundles Ryubing (or a
user who installs it as an external component) turns `switch` into a real flip with no code change — the probe follows
the install, the same way the default follows `es_systems.xml`.

**C. baked (generic, likely broken) libretro → plain launch (12):** the platform's only libretro command is a quoted
MAME template that cannot be baked, and the standalone MAME uses `\;` / `%STARTDIR%` — both un-bakeable, so the default
resolves to `None` and the ROM plain-launches, which lets RetroDECK resolve its own working MAME command.

| System                                                                                       | Old libretro  | New   |
| -------------------------------------------------------------------------------------------- | ------------- | ----- |
| apple2, apple2gs, astrocde, fmtowns, gamate, gamecom, gmaster, pv1000, scv, supracan, vsmile | mame_libretro | plain |
| x1                                                                                           | x1_libretro   | plain |

Note: **ps2** and **ps3** are **not** flips. Their first bakeable command is the same standalone the old
`core_defaults.json` curated (`PCSX2 (Standalone)` / `RPCS3 Directory (Standalone)`), so the resolved default is
identical — the curation is simply gone.

**Why 27, not ~15.** The deep-planning pass estimated ~15 flips. The live `es_systems.xml` on the current RetroDECK
ships more emulators than the plan assumed — Azahar replaced Citra, and XRoar / Ruffle / Ryubing / Cemu / Solarus /
PrimeHack were added, along with the `triforce` system — so the real count is 27. The six save-location-movers match the
plan exactly. That the count is **derived from live data rather than a fixed table is the whole point of #1210**: this
list will shift again as RetroDECK updates, and that is by design, not drift.

## On-device Definition of Done (gates the merge)

CI cannot catch a syntactically-valid-but-wrong `-e` — a command can bake cleanly and still launch the wrong emulator or
save to the wrong place. These checks must be run on a real Steam Deck before merge:

1. **A plain libretro system still launches.** A libretro-default system (e.g. psx → SwanStation) launches from its
   Steam shortcut, unchanged.
2. **An env-prefixed standalone launches.** A gc or wii game (Dolphin Standalone, `env … dolphin-emu …` form) launches,
   and its save now lands where the **standalone** emulator writes — different from the old `dolphin_libretro` location.
3. **A `-batch` / `--no-gui` standalone launches.** ps2 (PCSX2 `-batch`) and ps3 (RPCS3 `--no-gui`) launch from their
   baked `-e` command.
4. **A save-location-mover launches and saves where the standalone expects.** psp (PPSSPP) or n3ds (Azahar) launches and
   its saves land in the standalone emulator's directory, not the old libretro core's.
5. **A Category-C system plain-launches.** apple2 (or any MAME-only system) launches via the plain
   `flatpak run net.retrodeck.retrodeck` command and RetroDECK resolves its own working MAME invocation.
6. **The game-detail picker is correct.** It shows standalone and disabled un-bakeable entries with the right reason
   copy, and pinning a standalone applies live (the shortcut re-bakes and the confirm-set succeeds).
7. **The System-page picker fans out.** The `ButtonItem` → menu sets a per-platform emulator and re-bakes every
   installed+bound ROM on the platform.
8. **RetroDECK-absent degrades cleanly.** With `es_systems.xml` missing/unreadable, the picker shows "Emulator list
   unavailable" and launches degrade to plain — no crash.

## Related

Supersedes the `core_defaults.json` half of [#917](https://github.com/danielcopper/decky-romm-sync/issues/917) (already
closed); the `bios_registry` freshness piece folds into
[#916](https://github.com/danielcopper/decky-romm-sync/issues/916).

See also: [ADR-0012](0012-plugin-owns-core-selection-always-e-no-gamelist.md) (plugin owns core selection, always `-e`,
gamelist dropped — refined here), [ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md) (the per-game
LABEL on the `Rom` aggregate), [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the
baked-`launch_options` model + the `resolve_emulator_invocation` seam),
[ADR-0013](0013-platform-gated-m3u-via-es-systems.md) and
[ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md) (other live-`es_systems.xml`
reads on the same bake), [Core and Emulator Selection](../architecture/core-emulator-selection.md) (the resolver,
precedence, classifier, and picker in detail), [Config Source Parsers](../architecture/config-source-parsers.md)
(`es_systems.xml` as the sole core/emulator source).
