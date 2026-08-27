# Core and Emulator Selection

## Overview

A RomM game launches through RetroDECK on some **emulator** — most often a **RetroArch libretro core**, but for a few
platforms (PS2, PS3, …) a **standalone emulator** (PCSX2, RPCS3) that ES-DE lists as the working default. Most games use
their platform's default emulator, but the user can pin a different core for a single game (a **per-game** emulator
override) or for a whole platform (a **per-platform** emulator override). This page documents how the plugin decides
which emulator a game uses, where that decision is stored, and how it is applied at launch.

The central rule: **the read-path core equals the launched core.** Whatever core the plugin reports for a game — in the
BIOS-requirement filter, the save-directory name, the save-sync core tag, the core-change warning, the game-detail badge
— is the exact core that game will launch on. A single resolver guarantees that, and the launch command is baked from
the same resolved emulator. The plugin **owns emulator selection end to end**: it reads RetroDECK/ES-DE configuration
for the default emulator, but its own launches never depend on ES-DE's `gamelist.xml` — it neither reads nor writes that
file. The **live `es_systems.xml` is the sole source** for the system-layer default and the picker's emulator list;
there is no bundled snapshot (the curated `core_defaults.json` and its generator were deleted in #1210). See
[ADR-0011](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0011-per-game-core-override-in-db-applied-via-e-flag.md)
(the per-game DB override + `-e`),
[ADR-0012](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0012-plugin-owns-core-selection-always-e-no-gamelist.md)
(per-platform core in `settings.json`, always `-e`, gamelist dropped), and
[ADR-0020](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0020-live-es-systems-emulator-resolution.md)
(live `es_systems.xml` as the only source; the default is the first safely-bakeable command) for the decision records.

### Two emulator kinds: libretro core and standalone

The resolved emulator is a pure value object — `domain.shortcut_data.EmulatorInvocation` — that carries **exactly one**
of two payloads:

- **`kind == "libretro"`** — a RetroArch core, identified by its bare `.so` name (`core_so`). Rendered as the
  `-e "%EMULATOR_RETROARCH% -L <coresdir>/<so>.so %ROM%"` form. This is the overwhelming-majority path.
- **`kind == "standalone"`** — a standalone emulator, identified by its full ES-DE `<command>` text (already ending in
  `%ROM%`, e.g. `%EMULATOR_RPCS3% --no-gui %ROM%`). Baked verbatim into `-e`. RetroDECK resolves `%EMULATOR_*%` and
  substitutes `%ROM%` at launch, the same as the libretro form. This is the standalone-emulator seam
  ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129)): a system whose working ES-DE default is a
  standalone emulator (PS2 → PCSX2, PS3 → RPCS3, GameCube/Wii → Dolphin, PSP → PPSSPP, …) launches on that emulator
  instead of a deprecated/absent libretro core.

Both kinds are first-class throughout: the **system default** may be either (whichever ES-DE lists first that the plugin
can bake), and the per-game / per-platform picker lists both — so a pin may name a standalone emulator OR a libretro
core (#1210). A pin is stored as the emulator **LABEL** and resolved through the same classified option list at use
time.

A standalone emulator has **no** libretro `.so`, so the read-path projection reports `core_so = None` and every
`.so`-space consumer degrades exactly as it does for an unconfigured platform (see
[the single read seam](#the-single-read-seam-activecoreresolver)).

## The two override scopes

The plugin owns two **deviations** from the RetroDECK default core. Each stores only the deviation as a core LABEL;
absence means "follow the default."

| Scope            | Stored where                                             | Applies to              | Written by                     |
| ---------------- | -------------------------------------------------------- | ----------------------- | ------------------------------ |
| **Per-game**     | Plugin DB — `roms.emulator_override` (nullable LABEL)    | one ROM (by `rom_id`)   | the plugin (`pin`/`clear`)     |
| **Per-platform** | `settings.json` — `platform_cores` map (`{slug: label}`) | every ROM on a platform | the plugin (`set_system_core`) |

Both overrides are the plugin's own state and live in the plugin's own stores. Neither is written into ES-DE's
`gamelist.xml` — the plugin **never writes that file**. It still **reads** the RetroDECK/ES-DE configuration it does not
own (the live `es_systems.xml` — the system default emulator and the full classified command list the picker offers),
but the per-game and per-platform deviations are layered on top of that read by the plugin itself.

## Storage: the per-game override is a LABEL on the `Rom` aggregate

`roms.emulator_override` is a nullable `TEXT` column added by migration `002_add_emulator_override.sql`. It holds the
core **LABEL** the user picked (e.g. `"Beetle PSX HW"`), exactly as ES-DE displays it — never a resolved `.so` filename.

- **`NULL` = no override** → the game follows the RetroDECK/ES-DE default.
- It anchors on `roms`, not `rom_installs`, so the choice **survives uninstall/reinstall** (per
  [ADR-0007](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0007-rom-retention-identity-anchor.md)).
- Mutations go through the verb-named aggregate methods `Rom.pin_emulator_override(label)` (rejects a blank label) and
  `Rom.clear_emulator_override()`. Only `pin`/`clear` ever write the column; it is **excluded from the sync UPSERT `SET`
  clause**, so a re-sync never wipes a user's pin.

The plugin stores the **deviation** (the LABEL, or `NULL`), not a resolved core. The default and system layers are owned
by RetroDECK/ES-DE and change externally — a RetroDECK update can ship a new default emulator — so a stored resolved
value would go stale. Storing only the deviation keeps the plugin authoritative over exactly the slice it owns and
re-resolves the rest live. The LABEL is turned into an `EmulatorInvocation` (a libretro `.so` **or** a standalone
command) through the live-`es_systems.xml` classified option list at use time
(`domain.emulator_commands.label_to_invocation`) — so a per-game pin may name a standalone emulator, not only a libretro
core (#1210).

## Storage: the per-platform core is a LABEL in `settings.json`

The per-platform core lives in a `platform_cores` map in `settings.json` — `{platform_slug: core_label}` — added at
settings schema version 7 (a `setdefault("platform_cores", {})` migration; `adapters/persistence.py` +
`domain/state_migrations.py`). It holds the same kind of value as the per-game pin: the core **LABEL**, never a resolved
`.so`. An **absent key** means "no per-platform deviation — follow the es_systems default for this platform."

It is an
[ADR-0003](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0003-json-sqlite-persistence-boundary.md)
**bucket-1** value: a flat, user-set, relationship-free intent toggle. So it lives in `settings.json`, **not** SQLite,
and there is **no `Platform` aggregate** — consistent with the `platform_slug`-is-denormalized stance. The map starts
empty: there is no seed and no import from any previously-set ES-DE gamelist core (see
[No migration](#no-migration-re-apply-once)). The plugin reads it through the `PlatformCoreReader` Protocol
(`PlatformCoreReaderAdapter` in `adapters/persistence.py`), which holds the **live** settings dict so a fan-out after a
write resolves the freshly-written value rather than a stale snapshot.

## The single read seam: `ActiveCoreResolver`

`ActiveCoreResolver` (`py_modules/services/active_core_resolver.py`) is the one place that answers "which emulator will
this ROM actually launch with?" It exposes two methods over the **same** three-layer resolution:

- **`active_emulator_for_rom(rom_id) -> EmulatorInvocation | None`** — the **launch-bake seam**. Returns the full
  resolved emulator (libretro core OR standalone), or `None` when the platform has no resolvable emulator at all.
- **`active_core_for_rom(rom_id) -> (core_so, label)`** — the **read-path projection** of the above, kept for the
  `.so`-space consumers. A libretro emulator yields `(core_so, label)`; a **standalone** emulator yields
  `(None, label)`; an unresolvable platform yields `(None, None)`. Consumers already degrade on a `None` core, so a
  standalone launch never breaks them.

The precedence is the invariant:

> **per-game DB `emulator_override` (top) → per-platform `settings.json` `platform_cores` → live es_systems default →
> `None` (plain launch).**

```text
active_emulator_for_rom(rom_id):
  rom = read roms row (platform_slug + emulator_override)  ── one UoW read
  system = resolve_system(rom.platform_slug)               ── platform→system (ADR-0010)
  options = get_emulator_options(system)["options"]        ── every es_systems <command>, classified
  if rom.emulator_override is not None:                    ── layer 1: per-game pin (libretro OR standalone)
      inv = label_to_invocation(options, override)
      if inv is not None:
          return inv
      # stale / un-bakeable per-game label: warn, fall through
  platform_label = get_platform_core(rom.platform_slug)    ── layer 2: per-platform settings.json (libretro OR standalone)
  if platform_label is not None:
      inv = label_to_invocation(options, platform_label)
      if inv is not None:
          return inv
      # stale / un-bakeable per-platform label: warn, fall through
  return get_default_emulator(system)                      ── layer 3: live es_systems default (may be standalone), else None

active_core_for_rom(rom_id):                               ── the .so-space projection
  e = active_emulator_for_rom(rom_id)
  return (None, None) if e is None else (e.core_so, e.label)
```

Layers 1 and 2 resolve their LABEL through `domain.emulator_commands.label_to_invocation`, which finds the option
carrying that label and renders it into an `EmulatorInvocation` **only if it is bakeable** — so a pin may name a
standalone emulator or a libretro core, and a label that is unknown, un-bakeable, or `needs_setup` reads as "this pin no
longer resolves" and degrades to the next layer with a WARNING (never a bogus `None.so`). The system-layer fallback is
`CoreResolver.get_default_emulator(system)` (`adapters/es_de_config.py`): **the first safely-bakeable command in the
live `es_systems.xml` document order** (see
[Standalone-emulator selection](#standalone-emulator-selection-first-safely-bakeable)), which may itself be standalone
(PCSX2, RPCS3, Dolphin, …) or libretro. It reads **only** the live file — no bundled snapshot, and **no gamelist**
(neither a per-game `<altemulator>` nor a system-level `<alternativeEmulator>`; the gamelist is off every plugin code
path). When nothing is bakeable, or `es_systems.xml` cannot be read, it returns `None` and the caller bakes the plain
RetroDECK launch.

### Standalone-emulator selection: first safely-bakeable

Selection is **data-derived from the live `es_systems.xml` alone** — there is no curated snapshot. The pure kernel
`domain/emulator_commands.py` classifies each of a system's ES-DE `<command>` entries into an `EmulatorOption` (`label`,
`kind`, `core_so`, `command`, `status`, `reason`), and `select_default_option` returns **the first `bakeable` command in
document order**. ES-DE already lists a system's emulators in preference order, so ES-DE's own preference picks the
default — no plugin curation, no per-system table.

A command is `bakeable` only when it is a real emulator invocation the plugin can carry verbatim into a Steam shortcut's
`-e`. `classify_command` applies these rules in order, first match wins:

1. contains `%INJECT%` → `needs_setup` (`"inject"`) — needs ES-DE to generate a sidecar first (Vita3K, Xemu);
2. contains `%ENABLESHORTCUTS%` or `%EMULATOR_OS-SHELL%` → `unbakeable` (`"shortcut_script"`);
3. does not end in `%ROM%` → `unbakeable` (`"no_rom_target"`) — trailing args after `%ROM%` break the bake;
4. contains `"` or `\;` → `unbakeable` (`"quoting"`);
5. any placeholder outside the whitelist (`%EMULATOR_*%`, `%ROM%`, `%CORE_RETROARCH%`, `%GAMEDIR%`, `%GAMEDIRRAW%`,
   `%ROMPATH%`, `%BASENAME%`, `%FILENAME%`, `%ROMRAW%`, `%STARTDIR%`) → `unbakeable` (`"unknown_placeholder"`);
6. contains `%STARTDIR%` → `unbakeable` (`"startdir"`) — RetroDECK's `run_game.sh` parses-but-drops it, so a baked
   command would launch from the wrong directory (a v1 limitation);
7. otherwise `bakeable`.

This is why a system whose first command is a fragile shortcut form (`%ENABLESHORTCUTS% %EMULATOR_OS-SHELL% %ROM%`)
skips it and bakes the next command (the direct `--no-gui` launch): the shortcut form is `unbakeable`, so
`select_default_option` walks past it. A leading `env VAR=val …` prefix (the GameCube/Wii Dolphin form) is accepted as a
standalone invocation. The kind is `libretro` when the command matches the strict RetroArch shape
(`%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/<core>_libretro.so %ROM%`), else `standalone`.

**Existence probe (a bakeable standalone whose emulator is not installed → `needs_setup` `not_installed`).** RetroDECK
lists more standalone emulators in `es_systems.xml` than it bundles, so a system's bakeable default could name an
emulator that is not on disk (Ryubing on `switch`, any un-installed external component) — baking it produces a shortcut
that dies in ~0.4 s. To prevent that regression the adapter probes existence against the sibling `es_find_rules.xml`: it
maps a standalone command's `%EMULATOR_<NAME>%` token to the find-rule entry, checks whether any of that entry's
`staticpath` locations exist on disk (mapping the sandbox `/app` and `/var/{data,config}` prefixes to their host paths,
glob-aware), and hands the verdict to the pure `downgrade_if_not_installed(option, installed)` rule — which turns a
bakeable **standalone** whose emulator is absent into `needs_setup` with reason `not_installed`. It then drops out of
`select_default_option` (the system plain-launches, as it did before #1210) and shows disabled in the picker. The probe
is **absence-only** — it downgrades only on positive evidence that a RetroDECK component (`retrodeck/components/…` or
`retrodeck/external_components/…`) is missing; a `systempath`-only emulator (binary on RetroDECK's sandbox `PATH`, not
visible from outside), an emulator with no find rule, and the whole `es_find_rules.xml`-unreadable case are all assumed
installed, so it never falsely downgrades. Libretro is always installed (RetroArch ships with RetroDECK), so libretro
options are never downgraded. See [ADR-0020](../adr/0020-live-es-systems-emulator-resolution.md) §2.

`get_emulator_options(system)` returns `{"available": bool, "options": [EmulatorOption, ...]}`. **`available` is `False`
when `es_systems.xml` cannot be found or parsed** — the picker surfaces that as "Emulator list unavailable" rather than
an empty list it cannot distinguish from a system with no commands; the launch degrades to plain. `options_to_payload`
projects the list to the frontend picker shape (`{label, kind, core_so, is_default, bakeable, reason}`): bakeable
entries are clickable, the default is marked, and `needs_setup` / `unbakeable` entries are disabled with their reason.
See [ADR-0020](../adr/0020-live-es-systems-emulator-resolution.md) — including the 27 default flips this selection rule
produces relative to the old first-libretro default.

Adding a new standalone system needs **no plugin change** — the moment a RetroDECK update lists its emulator in
`es_systems.xml` in preference order, the plugin's default follows.

**Every per-game core read consumer draws from this one seam**, so the launch core cannot diverge from any derived
value:

| Consumer                                      | What it uses the core for                                   |
| --------------------------------------------- | ----------------------------------------------------------- |
| `FirmwareService` / game-detail BIOS check    | which BIOS files the active core requires (optional vs req) |
| `RomInfoService` (saves) → RetroArch corename | the sort-by-core save subdirectory name                     |
| `SyncEngine` (saves) core tag                 | the per-core save-sync identity                             |
| `StatusService.check_core_change`             | detect a core change since the last save sync               |
| `GameDetailService` → CPU badge / Active Core | the core shown on the game detail page                      |

A pinned per-game or per-platform label that no longer resolves (the core was removed by a RetroDECK update) is **never
fatal**: the resolver logs a WARNING and degrades to the next layer (the per-platform core, then the es_systems
default). No consumer ever sees a bogus `.so`.

## Application: baking `-e` into `launch_options`

Per
[ADR-0009](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0009-launcher-pure-exec-wrapper-baked-launch-options.md),
the launcher is a pure `exec "$@"` wrapper and the full launch command lives in the Steam shortcut's `launch_options`.
The pure seam `domain.shortcut_data.resolve_emulator_invocation(rom, emulator)` takes the resolved `EmulatorInvocation`
and renders the invocation:

- **libretro** invocation → the RetroArch `-e` form:

  ```text
  flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/<core>.so %ROM%"
  ```

- **standalone** invocation → the emulator's full ES-DE command baked verbatim:

  ```text
  flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RPCS3% --no-gui %ROM%"
  ```

- `emulator is None` → the plain `flatpak run net.retrodeck.retrodeck`.

`%EMULATOR_*%` and `%ROM%` stay as ES-DE placeholders — RetroDECK's `run_game.sh` resolves and single-quotes them at
launch, so a ROM path with spaces or parens is handled. For the libretro form, only the in-sandbox cores directory
(`/var/config/retroarch/cores`) is baked literally; ES-DE's `%CORE_RETROARCH%` variable is **not** expanded through
`-e`, so the plugin bakes the resolved path itself (a standalone command carries no `%CORE_RETROARCH%`, so it bakes
as-is). The `-e` flag makes RetroDECK skip its gamelist lookup entirely, which is why a baked emulator applies for any
filename (see
[Why the plugin always bakes the core, never the gamelist](#why-the-plugin-always-bakes-the-core-never-the-gamelist)).

**Always `-e`.** Per
[ADR-0012](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0012-plugin-owns-core-selection-always-e-no-gamelist.md),
every installed ROM bakes its **full resolved active emulator** through `-e` — the per-game pin, the per-platform core,
the es_systems libretro default, or a standalone emulator, whichever the resolver returns. The plain `flatpak run`
launch is **not** the "no override" case any more; it is reserved for the single fallback where the resolver yields
`None` (a platform with no resolvable default at all). Baking the default for every ROM is what lets the plugin own
launch selection completely: a launch that is _not_ `-e` would let RetroDECK consult the gamelist, re-coupling the
plugin to ES-DE's state. The cost is that a RetroDECK update changing a platform's default core needs a **Force Full
Sync** to re-bake — see [A frozen default needs a Force Full Sync](#a-frozen-default-needs-a-force-full-sync).

### The three bake sites

`launch_options` is written wherever a shortcut's command is (re)built. All three resolve the ROM's **full active
emulator** through the same `ActiveCoreResolver.active_emulator_for_rom` seam and pass the `EmulatorInvocation` into
`resolve_emulator_invocation`, so the read-path core and the launched emulator cannot diverge:

| Bake site                                        | When it runs                             | How it resolves the emulator                                                             |
| ------------------------------------------------ | ---------------------------------------- | ---------------------------------------------------------------------------------------- |
| `ShortcutBakeInputs` → `do_build_core_overrides` | every sync (preview + apply)             | each ROM through `active_emulator_for_rom` → `{rom_id: EmulatorInvocation}` for the bake |
| `DownloadService` → `_resolve_bound_app_id`      | on download-complete (install/reinstall) | the ROM through `active_emulator_for_rom` in the same flow                               |
| `MigrationService` → `_build_relaunch_items`     | on RetroDECK-home migration              | each relocated ROM through `active_emulator_for_rom`                                     |

The download-complete bake is the one that re-applies a pin after reinstall — the exact path `roms` storage was chosen
to protect. Each site bakes `-e` for every ROM that resolves to a concrete emulator (libretro or standalone), and the
plain launch only when the resolver returns `None`. A stale LABEL is handled inside the resolver (warn + degrade), so no
bake site ever emits `None.so`.

## Multi-disc selection

A multi-disc game (a PS1 RPG across four CDs, say) installs as a folder of disc images. The same bake that carries the
core also carries **which disc launches** — a second per-game deviation that follows the core override's structure point
for point. The decision record is
[ADR-0014](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md);
the user-facing guide is
[Picking a Disc for Multi-Disc Games](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/user-guide/managing-games.md#picking-a-disc-for-multi-disc-games).

### Storage: the disc pick is a basename on the `Rom` aggregate

`roms.selected_disc` is a nullable `TEXT` column added by migration `004_add_selected_disc.sql`. It holds the
**basename** of the disc the user pinned (e.g. `"Example Quest - Second Journey (USA) (Disc 2).cue"`), never a resolved
absolute path and never a disc index.

- **`NULL` = no selection** → the ROM follows the **default**: the install's `.m3u` when `file_path` is one (the
  in-emulator disc-swap default), else the first enumerated disc.
- It anchors on `roms`, not `rom_installs`, so the pick **survives uninstall/reinstall and RetroDECK-home migration**
  (per
  [ADR-0007](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0007-rom-retention-identity-anchor.md))
  — the disc folder is gone while uninstalled, but the basename re-resolves the moment it returns.
- Mutations go through the verb-named aggregate methods `Rom.pin_selected_disc(filename)` (rejects a blank filename) and
  `Rom.clear_selected_disc()`. Only `pin`/`clear` ever write the column (`SqliteRomRepository.set_selected_disc`); it is
  **excluded from the sync UPSERT `SET` clause** — the same `_SYNC_COLUMNS` tuple that omits `emulator_override` omits
  `selected_disc` — so a re-sync never wipes the pick.

The plugin stores the **basename** because the absolute path changes across uninstall/reinstall and home migration (a
stored path would go stale) and a positional index would silently re-point if a disc file were added, removed, or
renamed. The basename re-resolves to the same disc whenever it is present and cleanly registers as **stale** (→ default

- WARNING) when it is not.

### Disc identity vs the live accept-list

Enumerating a ROM's discs needs two different facts kept separate:

- **Disc identity is format-semantic and hardcoded** — `domain/disc_formats.py` defines
  `DISC_IMAGE_EXTENSIONS = {.cue, .chd, .iso}`, the irreducible set of launchable disc-image containers. A `.bin` is a
  **sidecar** (owned by its `.cue`, never launched directly) and an `.m3u` is a **playlist**; both are excluded simply
  by not being in the set. The disc unit is the `.cue`/`.chd`/`.iso` itself, never the `.bin`.
- **The per-system accept-list is a capability and read live** — `CoreResolver.get_supported_extensions(system)`
  (`adapters/es_de_config.py`) returns the system's es_systems `<extension>` set, threaded into the resolver through the
  `SystemSupportedExtensionsFn` Protocol, exactly as `system_supports_m3u` is read for the `.m3u` gate
  ([ADR-0013](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0013-platform-gated-m3u-via-es-systems.md)).

Enumeration keeps the files whose extension is in **the intersection** of the two, so a disc the emulator cannot launch
on this system is never offered. es_systems alone cannot supply disc identity — it is a flat accept-list with no
per-token role metadata, so it lists `.cue`, `.bin`, and `.m3u` identically and cannot say which is the disc. When
es_systems is unavailable, enumeration falls back to the full disc set rather than intersecting to nothing.

### The read seam: `DiscLaunchResolver`

`DiscLaunchResolver` (`py_modules/services/disc_launch_resolver.py`) is the single place that answers "which file will
this multi-disc ROM actually launch with?", mirroring `ActiveCoreResolver`. It scans the install directory recursively
(the file-lister Protocol), reads the live accept-list, enumerates the discs (`domain/disc_selection.enumerate_discs`),
and resolves the persisted `selected_disc` over them (`domain/disc_selection.resolve_launch_path`):

```text
resolve_for_install(install, selected_disc):
  discs = enumerate_discs(scan(install.rom_dir), supported_extensions(install.system))
  path =
    if len(discs) < 2:                    ── not multi-disc → file_path unchanged
        install.file_path
    elif selected_disc names a disc:      ── valid pin
        that disc's path
    else:                                 ── NULL, or a stale pin (warn + degrade)
        install.file_path if it ends .m3u else discs[0].path   ── the default
  return folder_boot_root(path, install.rom_dir) or path   ── folder-boot override (ADR-0019)
```

A **non-multi-disc ROM resolves to its own `file_path`** — zero behavior change for the overwhelming majority of games.
A **stale pin** (the selected disc no longer present) degrades to the default with a WARNING, never fatal, exactly like
`ActiveCoreResolver`'s stale-label handling. Crucially, the resolver **never rewrites `file_path`**: it returns the path
to bake, and `file_path`-derived values (save path, core, displayed filename) stay stable — the same bake-time
path-override layering the `-e` core override uses.

### Folder-boot launch target

Some systems boot a game **directory**, not the nested launch file. A PS3 game installs as a folder whose payload is
`…/PS3_GAME/USRDIR/EBOOT.BIN`, and `detect_launch_file` picks that EBOOT as `file_path` — correct as the launch _file_
identity, but RPCS3's directory-boot rejects it and wants the folder that contains `PS3_GAME`
([#1212](https://github.com/danielcopper/decky-romm-sync/issues/1212)). Two things change together for such a game — the
baked **path** and the baked **invocation form** — because they are decided from the same layout fact.

**The path.** A bake-time path override, layered **after** disc resolution in the same `resolve_bake_path` seam:
`folder_boot_root(path, install.rom_dir)` (`domain/rom_files.py`) strips a hardcoded `FOLDER_BOOT_MARKERS` run
(`PS3_GAME/USRDIR/EBOOT.BIN`, matched case-sensitively) from the resolved path and returns the game root, guarded so it
fires only when `rom_dir` is set and the derived root stays inside `rom_dir` (a bare EBOOT in the shared system dir
never bakes that shared dir). Because it triggers only when the resolved path still carries the marker, it composes with
disc resolution — a resolved disc path (`…(Disc 2).cue`) has no marker, so a multi-disc ROM is never folder-stripped,
and the folder rule applies only when disc resolution returned `file_path`.

**The invocation form.** RetroDECK's `run_game.sh` reinterprets any directory `%ROM%` as an ES-DE "directory as a file"
(`game="$game/$(basename "$game")"`, `run_game.sh:63-67`), so the standalone `-e "%EMULATOR_RPCS3% --no-gui %ROM%"` form
handed a folder points RPCS3 at a nonexistent `…/<Game>/<Game>` and never boots. A folder-boot standalone is therefore
baked as a **direct sandbox invocation** that bypasses `run_game.sh`:
`flatpak run --command=<launcher> net.retrodeck.retrodeck <args> "<folder>"`, running the emulator's own launcher inside
the sandbox. `ActiveCoreResolver.active_emulator_for_rom` makes this rewrite: when the resolved emulator is a standalone
and the ROM's install is a folder-boot layout (same `folder_boot_root` fact), it resolves the emulator's sandbox
launcher via the `es_find_rules.xml` probe (`CoreResolver.resolve_sandbox_launcher` → the
`/app/retrodeck/components/<x>/…` component path) and returns an `EmulatorInvocation.direct`;
`resolve_emulator_invocation` renders the `--command=` form, stripping `%EMULATOR_*%` + `%ROM%` down to the middle args
(`--no-gui`). A libretro emulator, a non-folder install, or an unresolvable launcher leave the standard `run_game` `-e`
form untouched.

`RomInstall.file_path` stays the EBOOT (the [ADR-0008](../adr/0008-rom-install-launch-file-and-rom-dir.md) anchor), so
save-path, core, and displayed-filename derivations are untouched. Both overrides live in the two shared seams
(`resolve_bake_path` for the path, `active_emulator_for_rom` for the invocation), so **every** bake site (the three
below plus the download-complete, core set/clear, and startup-reconcile bakes) inherits them with no call-site edits,
and a PS3 ROM synced before this change self-heals on its next bake. See
[ADR-0019](../adr/0019-folder-as-launch-target.md).

### The same three bake sites — disc path composes with the core

The three sites that re-bake the core override re-bake the disc path through this seam, and the two compose: the disc
resolver yields the **path**, `ActiveCoreResolver` yields the **`EmulatorInvocation`**, and
`resolve_emulator_invocation(rom, emulator)` + `build_launch_options(invocation, disc_path)` fold them into one command
— a per-game core (or standalone emulator) and a pinned disc on the same shortcut coexist.

| Bake site                                                                    | How it resolves the disc path                                                             |
| ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `ShortcutBakeInputs` (`do_scan_installed_paths` / `do_read_installed_paths`) | each installed ROM through `resolve_for_install` → `{rom_id: bake_path}` for the bake     |
| `DownloadService._resolve_bound_app_id`                                      | the freshly-installed ROM through `resolve_for_install` → re-applies the pin on reinstall |
| `MigrationService._build_relaunch_items`                                     | each relocated ROM through `resolve_for_install` against the moved install directory      |

### The picker callables

Two service methods on `DiscService` (`py_modules/services/disc.py`) drive the inline `DiscSelector` dropdown on the
game detail page:

- **`get_disc_selection(rom_id)`** reports `{multi_disc: false}` for an unknown, not-installed, single-file, or
  fewer-than-two-disc ROM (the frontend renders no picker), else `{multi_disc: true, discs: [...], selected, default}`.
  Read-only over the local filesystem; the no-picker answers are normal responses, not failures.
- **`select_disc(rom_id, filename)`** pins a disc (or clears to the default with `filename = null`). An unknown filename
  is a hard `not_found` failure and **nothing is written**; a non-multi-disc ROM is `unsupported`; a not-installed ROM
  is `not_installed` — all in the canonical `{success: false, reason, message}` shape. On success it persists the pick
  via the pin-only `set_selected_disc` write path, bakes the new disc path **folded over the ROM's full active core**,
  and returns the fresh `launch_options` + the now-effective `selected` for the frontend to confirm-set on the live
  shortcut. So the picker's selection and the baked launch command cannot diverge.

## Set, clear, and the confirm-before-toast flow

### Per-game (`CoreService`)

The frontend CPU-button menu on the game detail page drives two backend callables:

- **`set_game_core(rom_id, label)`** resolves the LABEL to a **bakeable `EmulatorInvocation` (libretro or standalone)
  first**, via `label_to_invocation` against the ROM platform's classified command list. A label that does not resolve
  to a bakeable emulator — unknown, `needs_setup`, or otherwise un-bakeable — is a **hard failure**: the canonical
  `{success: False, reason: "core_unavailable", message}` shape is returned and **nothing is written**, so the DB never
  holds a label no consumer can bake. On success it `pin`s the override, then re-bakes and returns the new
  `launch_options` (the `-e` override form) + the bound `app_id` for an installed ROM.
- **`clear_game_core(rom_id)`** (triggered by picking the **default-marked core** in the menu) `clear`s the override to
  `NULL`, then re-resolves the ROM's **full active core** through `ActiveCoreResolver` and bakes _that_ — the
  per-platform core or es_systems default, in `-e` form, **not** an unconditional plain launch. Because the plugin
  always bakes `-e`, "follow the default" still means baking a concrete core; the plain launch appears only when the
  platform resolves to `(None, None)`. There is no separate "Reset" item — selecting the default-marked entry is the
  clear path; any other entry pins that core.

For an installed + bound ROM the response carries `launch_options` + `app_id`; the frontend then **awaits
`setLaunchOptionsConfirmed`** (the fire-then-poll `AppDetails` confirm from
[ADR-0009](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0009-launcher-pure-exec-wrapper-baked-launch-options.md))
**before toasting success**. If the confirm fails, a distinct "Core saved — restart Steam to apply" toast shows and the
**DB row is kept** — the next migration/re-sync re-bakes from the pin. An uninstalled or unbound ROM has no live
shortcut to update: the pin still lands, `launch_options`/`app_id` come back `None`, and the override applies on the
next download/sync.

### Per-platform (`CoreService.set_system_core`)

The System-page **Emulator Core** picker — a menu button (`ButtonItem`) that opens the same context menu as the
game-detail picker, so standalone emulators and disabled un-bakeable entries render identically (#1210) — calls
**`set_system_core(platform_slug, core_label)`**:

1. It writes the choice into `settings["platform_cores"]` — storing the LABEL under the slug, or popping the slug when
   the label is empty (revert to the es_systems default) — and persists `settings.json` through the injected
   `SettingsPersister`. The es_systems cache is reset so the next resolution re-reads from disk.
2. It then **fans out a re-bake**: it iterates every ROM on the platform and, for each that is **installed and
   shortcut-bound** but does **not** carry a per-game `emulator_override` (the pin wins over the platform default),
   resolves the ROM's full active core and appends `{app_id, launch_options}` to a `rebake_items` list. ROMs with a
   per-game pin, uninstalled ROMs, and unbound ROMs are skipped — they have nothing live to rewrite, or their pin
   already wins.
3. It re-checks BIOS against the newly chosen core and returns `{success, bios_status, rebake_items}`.

The frontend confirm-sets each `rebake_items` entry on its live Steam shortcut the same way the per-game flow does, so a
per-platform core change applies **immediately** to every installed game on the platform — no sync required. Because the
`PlatformCoreReaderAdapter` holds the live settings dict, the fan-out resolves the value just written rather than a
stale snapshot.

The System page reads its per-platform data from the multi-platform `get_firmware_status` payload (not
`get_platform_core_info`, which is the game-detail path). Each entry's **`active_core_label`** is the resolved display
label the Emulator Core button shows: the per-platform override (`platform_cores`) when it is set and still resolves to
a bakeable emulator, else the es_systems **default emulator** label (the first bakeable command — libretro _or_
standalone). This is the platform-level projection of the read-path precedence (`FirmwareService`'s
`_resolve_platform_emulator_label`, injected the same `PlatformCoreReader` the resolver uses), so after a per-platform
pick the button reflects the applied selection on the next `refreshSystem` — the same way the game-detail menu does —
and a standalone default reads its standalone label rather than the libretro system default. It is intentionally
distinct from the payload's `active_core` (`core_so`), which stays the **libretro** system default the BIOS filter keys
on (standalone-default BIOS accuracy is deferred by ADR-0020).

## Why the plugin always bakes the core, never the gamelist

ES-DE stores core choices in `gamelist.xml` — a per-game `<altemulator>` element and a system-level
`<alternativeEmulator>`. The plugin does **not** use that file for its own launches at all (it neither reads nor writes
it), for reasons grounded in on-device testing:

1. **RetroDECK's gamelist lookup is metacharacter-fragile.** When the plugin's Steam shortcut launches a ROM with a
   plain `flatpak run` command, RetroDECK's `run_game.sh` matches the ROM path against the gamelist using an **awk `~`
   regex**. Any regex metacharacter in the filename (`(USA)`, `(Disc 1)`, `[!]`, …) breaks the match, and the per-game
   `<altemulator>` is silently dropped. This is upstream bug
   [#210](https://github.com/danielcopper/decky-romm-sync/issues/210) /
   [RetroDECK#1358](https://github.com/RetroDECK/RetroDECK/issues/1358). (ES-DE's _own_ UI resolves `<altemulator>`
   itself and bypasses the awk — which is why a core choice can look like it works when launched from ES-DE but not from
   the plugin's shortcut.)
2. **`-e` bypasses the lookup.** RetroDECK's `-e` flag sets the emulator invocation directly and skips the gamelist awk
   block, so the baked core applies regardless of filename. The plugin bakes the resolved core into `-e` for **every**
   installed ROM and is no longer coupled to either the awk bug or ES-DE's folder-collapse display quirks.
3. **A plain launch re-couples the plugin to ES-DE.** A non-`-e` launch lets RetroDECK consult the gamelist itself, so a
   core a user set inside ES-DE's UI would silently affect the plugin's launch — diverging from the BIOS badge, the
   per-core save path, and the core-change warning that all follow the plugin's resolver. Baking `-e` for every ROM
   ([ADR-0012](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0012-plugin-owns-core-selection-always-e-no-gamelist.md))
   closes that path: the plugin owns core selection end to end, and an ES-DE-set core never reaches a plugin launch.

Writing the gamelist is dropped for the same ownership reason: `gamelist.xml` is ES-DE's strict-parser-hostile,
multi-root-tolerant file, and the per-platform deviation that once lived there now lives in the plugin's own
`settings.json`. There is **no gamelist write** on any plugin path.

### No migration, re-apply once

There is **no migration** from any old gamelist model. A per-game core previously written to `gamelist.xml` is not
imported (per
[ADR-0011](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0011-per-game-core-override-in-db-applied-via-e-flag.md)),
and a per-platform core previously set as a system-level `<alternativeEmulator>` is **not** imported into
`platform_cores` either — `platform_cores` starts empty. This is by design: a gamelist-import path would revive the
multi-root-XML parse failures and folder-collapse ambiguity the plugin-owned model was chosen to avoid. Re-apply any
per-platform core once through the System-page Emulator Core picker (the menu button) and it sticks from then on.

### A frozen default needs a Force Full Sync

Because the es_systems default is baked literally into every shortcut, a RetroDECK update that ships a **new default
core for a platform** does **not** take effect on a normal sync — a normal sync skips platforms whose ROM set is
unchanged, so the previously-baked default survives. A **Force Full Sync** re-bakes every shortcut and picks up the new
default. A core the user sets through the plugin (per-game pin or per-platform picker) re-bakes immediately, so only an
externally-changed RetroDECK default carries this caveat.

## RetroDECK is the V1 target

The `-e` flag, the `%EMULATOR_RETROARCH%` / `%ROM%` placeholders, and the `/var/config/retroarch/cores` path are
**RetroDECK-adapter concerns**, isolated at the single seam `resolve_emulator_invocation`. RetroDECK is the supported
launcher for V1 — this is the correct V1 shape, not a placeholder. The per-ROM **selection** (which emulator does this
ROM resolve to?) is a service-layer read; the seam only **renders** the chosen `EmulatorInvocation` into a command
string. Standalone-emulator support ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129)) is the first
half of the multi-emulator lift: a standalone emulator is still launched **through RetroDECK's `-e`**, so the RetroDECK
flatpak invocation remains the single seam — only the `-e` payload changed (a verbatim ES-DE command instead of the
RetroArch `-L` form). The remaining lift ([#918](https://github.com/danielcopper/decky-romm-sync/issues/918)) — a
non-RetroDECK launcher behind a `Frontend`-style port — is net-new work and is not built until a second launcher is
concrete.

The **core picker now lists standalone emulators alongside libretro cores** (#1210 / ADR-0020) — you _can_ choose
standalone PCSX2 vs the LRPS2 libretro core in the UI, at both per-game and per-platform scope. One follow-up stays out
of scope for the standalone seam: **BIOS badge / save-sync** for a standalone system read `active_core = None` and
degrade — the launch works (BIOS already present on-device), but badge accuracy and standalone save-sync are separate
efforts.

---

**Related pages:**

- [Backend Architecture](backend-architecture.md) — service/adapter layering, dependency diagram
- [Config Source Parsers](config-source-parsers.md) — one-parser-per-source principle; how the live `es_systems.xml` is
  read as the sole core/emulator source (the gamelist and the bundled `core_defaults.json` are no longer read)
- [Steam Non-Steam Shortcuts](steam-non-steam-shortcuts.md) — AddShortcut API, `launch_options`, app-id derivation
- [Database Design](database-design.md) — the `Rom` aggregate and the `roms` table (incl. `selected_disc`)
- [BIOS and Emulator Cores](../user-guide/bios-management.md) — the user-facing core-selection guide
- [Managing Games](../user-guide/managing-games.md#picking-a-disc-for-multi-disc-games) — the user-facing disc picker
