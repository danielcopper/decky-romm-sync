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
file. See
[ADR-0011](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0011-per-game-core-override-in-db-applied-via-e-flag.md)
(the per-game DB override + `-e`) and
[ADR-0012](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0012-plugin-owns-core-selection-always-e-no-gamelist.md)
(per-platform core in `settings.json`, always `-e`, gamelist dropped) for the decision records.

### Two emulator kinds: libretro core and standalone

The resolved emulator is a pure value object — `domain.shortcut_data.EmulatorInvocation` — that carries **exactly one**
of two payloads:

- **`kind == "libretro"`** — a RetroArch core, identified by its bare `.so` name (`core_so`). Rendered as the
  `-e "%EMULATOR_RETROARCH% -L <coresdir>/<so>.so %ROM%"` form. This is the overwhelming-majority path; per-game and
  per-platform overrides only ever produce libretro invocations (the picker offers libretro cores).
- **`kind == "standalone"`** — a standalone emulator, identified by its full ES-DE `<command>` text (already ending in
  `%ROM%`, e.g. `%EMULATOR_RPCS3% --no-gui %ROM%`). Baked verbatim into `-e`. RetroDECK resolves `%EMULATOR_*%` and
  substitutes `%ROM%` at launch, the same as the libretro form. This is the standalone-emulator seam
  ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129)): a system whose working ES-DE default is a
  standalone emulator (PS2 → PCSX2, PS3 → RPCS3) launches on that emulator instead of a deprecated/absent libretro core.

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
own (the es_systems default core and the available-cores list), but the per-game and per-platform deviations are layered
on top of that read by the plugin itself.

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
by RetroDECK/ES-DE and change externally — a RetroDECK update can ship a new default core — so a stored resolved value
would go stale. Storing only the deviation keeps the plugin authoritative over exactly the slice it owns and re-resolves
the rest live. The LABEL is turned into a `.so` through the es_systems `available_cores` map at use time
(`domain.shortcut_data.label_to_core_so`).

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
this ROM actually launch with?" It exposes two methods over the **same** four-layer resolution:

- **`active_emulator_for_rom(rom_id) -> EmulatorInvocation | None`** — the **launch-bake seam**. Returns the full
  resolved emulator (libretro core OR standalone), or `None` when the platform has no resolvable emulator at all.
- **`active_core_for_rom(rom_id) -> (core_so, label)`** — the **read-path projection** of the above, kept for the
  `.so`-space consumers. A libretro emulator yields `(core_so, label)`; a **standalone** emulator yields
  `(None, label)`; an unresolvable platform yields `(None, None)`. Consumers already degrade on a `None` core, so a
  standalone launch never breaks them.

The precedence is the invariant:

> **per-game DB `emulator_override` (top) → per-platform `settings.json` `platform_cores` → es_systems default (live,
> standalone-aware) → `core_defaults`.**

```text
active_emulator_for_rom(rom_id):
  rom = read roms row (platform_slug + emulator_override)  ── one UoW read
  system = resolve_system(rom.platform_slug)               ── platform→system (ADR-0010)
  available = get_available_cores(system)
  if rom.emulator_override is not None:                    ── layer 1: per-game pin (always libretro)
      core_so = label_to_core_so(available, override)
      if core_so is not None:
          return EmulatorInvocation.libretro(core_so, override)
      # stale per-game label: warn, fall through
  platform_label = get_platform_core(rom.platform_slug)    ── layer 2: per-platform settings.json (always libretro)
  if platform_label is not None:
      core_so = label_to_core_so(available, platform_label)
      if core_so is not None:
          return EmulatorInvocation.libretro(core_so, platform_label)
      # stale per-platform label: warn, fall through
  return get_default_emulator(system)                      ── layer 3/4: standalone-aware es_systems default → core_defaults

active_core_for_rom(rom_id):                               ── the .so-space projection
  e = active_emulator_for_rom(rom_id)
  return (None, None) if e is None else (e.core_so, e.label)
```

The system-layer fallback is `CoreResolver.get_default_emulator(system)` (`adapters/es_de_config.py`), the
**standalone-aware** layer: if `core_defaults.json` marks the system with a curated `standalone` block it bakes that
emulator (see [Standalone-emulator selection](#standalone-emulator-selection-the-curated-default)), otherwise it
projects the libretro `get_active_core(system)` default (live `es_systems.xml` default with bundled `core_defaults.json`
as fallback). It **no longer reads any gamelist** — neither a per-game `<altemulator>` nor a system-level
`<alternativeEmulator>`; the gamelist is off every plugin code path. The per-platform deviation that used to live in the
gamelist is now the `settings.json` layer above. A pinned per-game or per-platform LABEL that no longer resolves (a core
a RetroDECK update removed) is **never fatal**: the resolver logs a WARNING and degrades to the next layer, never
returning a bogus `None.so`.

### Standalone-emulator selection: the curated default

Selection of a standalone emulator is **data-driven**. A system gains a `standalone` block in
`defaults/core_defaults.json` naming the preferred ES-DE command **label**; the live `es_systems.xml` supplies the
command text for that label (the bundled string is the offline fallback):

```json
"ps2": { "...": "...", "standalone": { "label": "PCSX2 (Standalone)",        "command": "%EMULATOR_PCSX2% -batch %ROM%" } },
"ps3": { "...": "...", "standalone": { "label": "RPCS3 Directory (Standalone)", "command": "%EMULATOR_RPCS3% --no-gui %ROM%" } }
```

A curated label is needed because ES-DE's **first** `<command>` is not always the one to bake — PS3's first command is a
fragile shortcut form (`%ENABLESHORTCUTS% %EMULATOR_OS-SHELL% %ROM%`), not the direct `--no-gui` launch. The parser
captures **every** `<command>` per system as `{label: command_text}` (plus the first label as ES-DE's true default), so
`get_default_emulator` can resolve the curated label against the live file and pick up a RetroDECK update. Adding a new
standalone system (Switch/Xbox/Vita/Wii U) once its ES-DE labels are confirmed is a **data-only** change — one
`standalone` block, no code.

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

| Bake site                                    | When it runs                             | How it resolves the emulator                                                             |
| -------------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------- |
| `SyncOrchestrator` → `_build_core_overrides` | every sync (preview + apply)             | each ROM through `active_emulator_for_rom` → `{rom_id: EmulatorInvocation}` for the bake |
| `DownloadService` → `_resolve_bound_app_id`  | on download-complete (install/reinstall) | the ROM through `active_emulator_for_rom` in the same flow                               |
| `MigrationService` → `_build_relaunch_items` | on RetroDECK-home migration              | each relocated ROM through `active_emulator_for_rom`                                     |

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
**basename** of the disc the user pinned (e.g. `"Final Fantasy VII (USA) (Disc 2).cue"`), never a resolved absolute path
and never a disc index.

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
([#1212](https://github.com/danielcopper/decky-romm-sync/issues/1212)). This is a second bake-time path override,
layered **after** disc resolution in the same `resolve_bake_path` seam: `folder_boot_root(path, install.rom_dir)`
(`domain/rom_files.py`) strips a hardcoded `FOLDER_BOOT_MARKERS` run (`PS3_GAME/USRDIR/EBOOT.BIN`, matched
case-sensitively) from the resolved path and returns the game root, guarded so it fires only when `rom_dir` is set and
the derived root stays inside `rom_dir` (a bare EBOOT in the shared system dir never bakes that shared dir). Because it
triggers only when the resolved path still carries the marker, it composes with disc resolution — a resolved disc path
(`…(Disc 2).cue`) has no marker, so a multi-disc ROM is never folder-stripped, and the folder rule applies only when
disc resolution returned `file_path`. `RomInstall.file_path` stays the EBOOT (the
[ADR-0008](../adr/0008-rom-install-launch-file-and-rom-dir.md) anchor), so save-path, core, and displayed-filename
derivations are untouched; only the baked argument becomes the folder. The override lives in the one seam, so **every**
bake site (the three below plus the download-complete, core set/clear, and startup-reconcile bakes) inherits it, and a
PS3 ROM synced before this change self-heals on its next bake. See [ADR-0019](../adr/0019-folder-as-launch-target.md).

### The same three bake sites — disc path composes with the core

The three sites that re-bake the core override re-bake the disc path through this seam, and the two compose: the disc
resolver yields the **path**, `ActiveCoreResolver` yields the **`EmulatorInvocation`**, and
`resolve_emulator_invocation(rom, emulator)` + `build_launch_options(invocation, disc_path)` fold them into one command
— a per-game core (or standalone emulator) and a pinned disc on the same shortcut coexist.

| Bake site                                                              | How it resolves the disc path                                                             |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `SyncOrchestrator` (`_scan_installed_paths` / `_read_installed_paths`) | each installed ROM through `resolve_for_install` → `{rom_id: bake_path}` for the bake     |
| `DownloadService._resolve_bound_app_id`                                | the freshly-installed ROM through `resolve_for_install` → re-applies the pin on reinstall |
| `MigrationService._build_relaunch_items`                               | each relocated ROM through `resolve_for_install` against the moved install directory      |

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

- **`set_game_core(rom_id, label)`** resolves the LABEL to its `.so` **first**. An unresolvable label is a **hard
  failure** — the canonical `{success: False, reason, message}` shape is returned and **nothing is written**, so the DB
  never holds a label no consumer can resolve. On success it `pin`s the override, then re-bakes and returns the new
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

The System-page **Emulator Core** dropdown calls **`set_system_core(platform_slug, core_label)`**:

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
per-platform core once through the System-page dropdown and it sticks from then on.

### A frozen default needs a Force Full Sync

Because the es_systems default is baked literally into every shortcut, a RetroDECK update that ships a **new default
core for a platform** does **not** take effect on a normal sync — a normal sync skips platforms whose ROM set is
unchanged, so the previously-baked default survives. A **Force Full Sync** re-bakes every shortcut and picks up the new
default. A core the user sets through the plugin (per-game pin or per-platform dropdown) re-bakes immediately, so only
an externally-changed RetroDECK default carries this caveat.

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

Two follow-ups stay out of scope for the standalone seam as it stands: the per-game / per-platform **core picker** still
lists only libretro cores (you cannot choose standalone PCSX2 vs the LRPS2 libretro core in the UI), and **BIOS badge /
save-sync** for a standalone system read `active_core = None` and degrade — the launch works (BIOS already present
on-device), but badge accuracy and standalone save-sync are separate efforts.

---

**Related pages:**

- [Backend Architecture](backend-architecture.md) — service/adapter layering, dependency diagram
- [Config Source Parsers](config-source-parsers.md) — one-parser-per-source principle; how `es_systems.xml` and
  `core_defaults.json` are read (the gamelist is no longer read)
- [Steam Non-Steam Shortcuts](steam-non-steam-shortcuts.md) — AddShortcut API, `launch_options`, app-id derivation
- [Database Design](database-design.md) — the `Rom` aggregate and the `roms` table (incl. `selected_disc`)
- [BIOS and Emulator Cores](../user-guide/bios-management.md) — the user-facing core-selection guide
- [Managing Games](../user-guide/managing-games.md#picking-a-disc-for-multi-disc-games) — the user-facing disc picker
