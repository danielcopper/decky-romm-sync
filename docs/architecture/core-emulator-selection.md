# Core and Emulator Selection

## Overview

A RomM game launches through RetroDECK on some **RetroArch core**. Most games use their platform's default core, but the
user can pin a different core for a single game (a **per-game override**) or for a whole platform (a **per-system
override**). This page documents how the plugin decides which core a game uses, where that decision is stored, and how
it is applied at launch.

The central rule: **the read-path core equals the launched core.** Whatever core the plugin reports for a game — in the
BIOS-requirement filter, the save-directory name, the save-sync core tag, the core-change warning, the game-detail badge
— is the exact core that game will launch on. A single resolver guarantees that, and the launch command is baked from
the same resolved core. See
[ADR-0011](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0011-per-game-core-override-in-db-applied-via-e-flag.md)
for the decision record.

## The two override scopes

| Scope          | Stored where                                          | Applies to              | Written by                                     |
| -------------- | ----------------------------------------------------- | ----------------------- | ---------------------------------------------- |
| **Per-game**   | Plugin DB — `roms.emulator_override` (nullable LABEL) | one ROM (by `rom_id`)   | the plugin (`pin`/`clear`), never the gamelist |
| **Per-system** | ES-DE `gamelist.xml` — `<alternativeEmulator>`        | every ROM on a platform | the plugin's `set_system_override`, and ES-DE  |

The per-game override is the plugin's own state and lives in the plugin's own database. The per-system override is
genuinely an ES-DE-level setting (ES-DE-native launches must honour it too), so it is written into ES-DE's config — a
single, system-scoped element. **The plugin never writes a per-game override into the gamelist.** It reads
RetroDECK/ES-DE config it does not own (defaults, available cores, the system-level `<alternativeEmulator>`); it does
not write that config for per-game state.

## Storage: the override is a LABEL on the `Rom` aggregate

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

## The single read seam: `ActiveCoreResolver`

`ActiveCoreResolver.active_core_for_rom(rom_id) -> (core_so, label)` (`py_modules/services/active_core_resolver.py`) is
the one place that answers "which core will this ROM actually launch with?" Its precedence is the invariant:

> **DB `emulator_override` (top) → system `<alternativeEmulator>` → es_systems default → `core_defaults`.**

```text
active_core_for_rom(rom_id):
  rom = read roms row (platform_slug + emulator_override)  ── one UoW read
  system = resolve_system(rom.platform_slug)               ── platform→system (ADR-0010)
  if rom.emulator_override is not None:
      core_so = label_to_core_so(available_cores(system), override)
      if core_so is not None:
          return (core_so, override)                       ── the pin wins
      # stale label (core removed by a RetroDECK update): warn, fall through
  return get_active_core(system)                           ── system <altemulator> → default → core_defaults
```

The system-layer fallback is `CoreResolver.get_active_core(system)` (`adapters/es_de_config.py`), which still resolves
the per-system `<alternativeEmulator>`, the live `es_systems.xml` default, and the bundled `core_defaults.json` — but it
no longer takes a `rom_filename` and no longer reads any per-game gamelist entry. It is the system-level layer only.

**Every per-game core read consumer draws from this one seam**, so the launch core cannot diverge from any derived
value:

| Consumer                                      | What it uses the core for                                   |
| --------------------------------------------- | ----------------------------------------------------------- |
| `FirmwareService` / game-detail BIOS check    | which BIOS files the active core requires (optional vs req) |
| `RomInfoService` (saves) → RetroArch corename | the sort-by-core save subdirectory name                     |
| `SyncEngine` (saves) core tag                 | the per-core save-sync identity                             |
| `StatusService.check_core_change`             | detect a core change since the last save sync               |
| `GameDetailService` → CPU badge / Active Core | the core shown on the game detail page                      |

A pinned label that no longer resolves (the core was removed by a RetroDECK update) is **never fatal**: the resolver
logs a WARNING and degrades to the system-layer result. No consumer ever sees a bogus `.so`.

## Application: baking `-e` into `launch_options`

Per
[ADR-0009](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0009-launcher-pure-exec-wrapper-baked-launch-options.md),
the launcher is a pure `exec "$@"` wrapper and the full launch command lives in the Steam shortcut's `launch_options`.
The pure seam `domain.shortcut_data.resolve_emulator_invocation(rom, active_core_so)` renders the invocation:

- `active_core_so is None` (no override) → the plain `flatpak run net.retrodeck.retrodeck`.
- `active_core_so` set (override) → the `-e` form:

  ```text
  flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/<core>.so %ROM%"
  ```

`%EMULATOR_RETROARCH%` and `%ROM%` stay as ES-DE placeholders — RetroDECK's `run_game.sh` resolves and single-quotes
them at launch, so a ROM path with spaces or parens is handled. Only the in-sandbox cores directory
(`/var/config/retroarch/cores`) is baked literally; ES-DE's `%CORE_RETROARCH%` variable is **not** expanded through
`-e`, so the plugin bakes the resolved path itself. The `-e` flag makes RetroDECK skip its gamelist lookup entirely,
which is why the override applies for any filename (see [Why `-e`, not the gamelist](#why--e-not-the-gamelist)).

`-e` is added **only** for ROMs that have an override. A ROM with no override keeps the plain command, so RetroDECK's
default-emulator behaviour stays live for the common case and the blast radius is minimal.

### The three bake sites

`launch_options` is written wherever a shortcut's command is (re)built. All three resolve the override LABEL to its
`.so` through the same path and pass it into `resolve_emulator_invocation`:

| Bake site                                    | When it runs                             | How it reads the override                                            |
| -------------------------------------------- | ---------------------------------------- | -------------------------------------------------------------------- |
| `SyncOrchestrator` → `build_shortcuts_data`  | every sync (preview + apply)             | `get_all_emulator_overrides()` once → resolves each LABEL → `.so`    |
| `DownloadService` → `_resolve_bound_app_id`  | on download-complete (install/reinstall) | reads the ROM's `emulator_override` in the same UoW → resolves `.so` |
| `MigrationService` → `_build_relaunch_items` | on RetroDECK-home migration              | reads each relocated ROM's `emulator_override` → resolves `.so`      |

The download-complete bake is the one that re-applies a pin after reinstall — the exact path `roms` storage was chosen
to protect. A stale LABEL at any bake site degrades to the plain launch with a WARNING; it never bakes `None.so`.

## Set, clear, and the confirm-before-toast flow

The frontend CPU-button menu on the game detail page drives two backend callables:

- **`set_game_core(rom_id, label)`** (`CoreService`) resolves the LABEL to its `.so` **first**. An unresolvable label is
  a **hard failure** — the canonical `{success: False, reason, message}` shape is returned and **nothing is written**,
  so the DB never holds a label no consumer can resolve. On success it `pin`s the override, then re-bakes and returns
  the new `launch_options` + the bound `app_id` for an installed ROM.
- **`clear_game_core(rom_id)`** (triggered by picking the **default-marked core** in the menu) `clear`s the override to
  `NULL` and returns the recomputed **plain** `launch_options` (no `-e`). There is no separate "Reset" item — selecting
  the default-marked entry is the clear path; any other entry pins that core.

For an installed + bound ROM the response carries `launch_options` + `app_id`; the frontend then **awaits
`setLaunchOptionsConfirmed`** (the fire-then-poll `AppDetails` confirm from
[ADR-0009](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0009-launcher-pure-exec-wrapper-baked-launch-options.md))
**before toasting success**. If the confirm fails, a distinct "Core saved — restart Steam to apply" toast shows and the
**DB row is kept** — the next migration/re-sync re-bakes from the pin. An uninstalled or unbound ROM has no live
shortcut to update: the pin still lands, `launch_options`/`app_id` come back `None`, and the override applies on the
next download/sync.

## Why `-e`, not the gamelist

ES-DE stores a per-game override as an `<altemulator>` element in `gamelist.xml`. The plugin does **not** use that path
for its own launches, for two reasons grounded in on-device testing:

1. **RetroDECK's gamelist lookup is metacharacter-fragile.** When the plugin's Steam shortcut launches a ROM,
   RetroDECK's `run_game.sh` matches the ROM path against the gamelist using an **awk `~` regex**. Any regex
   metacharacter in the filename (`(USA)`, `(Disc 1)`, `[!]`, …) breaks the match, and the per-game override is silently
   dropped — the game launches on the default core. This is upstream bug
   [#210](https://github.com/danielcopper/decky-romm-sync/issues/210) /
   [RetroDECK#1358](https://github.com/RetroDECK/RetroDECK/issues/1358). (ES-DE's _own_ UI resolves `<altemulator>`
   itself and bypasses the awk — which is why an override can look like it works when launched from ES-DE but not from
   the plugin's shortcut.)
2. **`-e` bypasses the lookup.** RetroDECK's `-e` flag sets the emulator invocation directly and skips the gamelist awk
   block, so the override applies regardless of filename. The plugin bakes the resolved core into `-e` and is no longer
   coupled to either the awk bug or ES-DE's folder-collapse display quirks.

There is **no migration** from the old gamelist model (a per-game override previously written to `gamelist.xml` is not
imported) — by design, because a gamelist-import path would revive the multi-root-XML parse failures and folder-collapse
ambiguity the DB-storage model was chosen to avoid.

## RetroDECK is the V1 target

The `-e` flag, the `%EMULATOR_RETROARCH%` / `%ROM%` placeholders, and the `/var/config/retroarch/cores` path are
**RetroDECK-adapter concerns**, isolated at the single seam `resolve_emulator_invocation`. RetroDECK is the supported
launcher for V1 — this is the correct V1 shape, not a placeholder. The per-ROM **selection** (does this ROM have an
override?) is a service-layer DB read; the seam only **renders** the chosen invocation into a command string. The
multi-emulator lift ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129) /
[#918](https://github.com/danielcopper/decky-romm-sync/issues/918)) is a near-mechanical extraction of that one seam
into a RetroDECK adapter behind a `Frontend`-style port; a sibling emulator's launch argv is net-new work, so the port
is not built until a second emulator is concrete.

---

**Related pages:**

- [Backend Architecture](backend-architecture.md) — service/adapter layering, dependency diagram
- [Config Source Parsers](config-source-parsers.md) — one-parser-per-source principle; how `es_systems.xml` /
  `gamelist.xml` / `core_defaults.json` are read
- [Steam Non-Steam Shortcuts](steam-non-steam-shortcuts.md) — AddShortcut API, `launch_options`, app-id derivation
- [Database Design](database-design.md) — the `Rom` aggregate and the `roms` table
- [BIOS and Emulator Cores](../user-guide/bios-management.md) — the user-facing core-selection guide
