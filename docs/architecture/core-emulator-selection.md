# Core and Emulator Selection

## Overview

A RomM game launches through RetroDECK on some **RetroArch core**. Most games use their platform's default core, but the
user can pin a different core for a single game (a **per-game** emulator override) or for a whole platform (a
**per-platform** emulator override). This page documents how the plugin decides which core a game uses, where that
decision is stored, and how it is applied at launch.

The central rule: **the read-path core equals the launched core.** Whatever core the plugin reports for a game — in the
BIOS-requirement filter, the save-directory name, the save-sync core tag, the core-change warning, the game-detail badge
— is the exact core that game will launch on. A single resolver guarantees that, and the launch command is baked from
the same resolved core. The plugin **owns core selection end to end**: it reads RetroDECK/ES-DE configuration for the
default core, but its own launches never depend on ES-DE's `gamelist.xml` — it neither reads nor writes that file. See
[ADR-0011](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0011-per-game-core-override-in-db-applied-via-e-flag.md)
(the per-game DB override + `-e`) and
[ADR-0012](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0012-plugin-owns-core-selection-always-e-no-gamelist.md)
(per-platform core in `settings.json`, always `-e`, gamelist dropped) for the decision records.

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

`ActiveCoreResolver.active_core_for_rom(rom_id) -> (core_so, label)` (`py_modules/services/active_core_resolver.py`) is
the one place that answers "which core will this ROM actually launch with?" Its precedence is the invariant:

> **per-game DB `emulator_override` (top) → per-platform `settings.json` `platform_cores` → es_systems default (live) →
> `core_defaults`.**

```text
active_core_for_rom(rom_id):
  rom = read roms row (platform_slug + emulator_override)  ── one UoW read
  system = resolve_system(rom.platform_slug)               ── platform→system (ADR-0010)
  available = get_available_cores(system)
  if rom.emulator_override is not None:                    ── layer 1: per-game pin
      core_so = label_to_core_so(available, override)
      if core_so is not None:
          return (core_so, override)
      # stale per-game label: warn, fall through
  platform_label = get_platform_core(rom.platform_slug)    ── layer 2: per-platform settings.json
  if platform_label is not None:
      core_so = label_to_core_so(available, platform_label)
      if core_so is not None:
          return (core_so, platform_label)
      # stale per-platform label: warn, fall through
  return get_active_core(system)                           ── layer 3/4: es_systems default → core_defaults
```

The system-layer fallback is `CoreResolver.get_active_core(system)` (`adapters/es_de_config.py`), which resolves the
live `es_systems.xml` default with the bundled `core_defaults.json` as a fallback. It **no longer reads any gamelist** —
neither a per-game `<altemulator>` nor a system-level `<alternativeEmulator>`; the gamelist is off every plugin code
path. The per-platform deviation that used to live in the gamelist is now the `settings.json` layer above. A pinned
per-game or per-platform LABEL that no longer resolves (a core a RetroDECK update removed) is **never fatal**: the
resolver logs a WARNING and degrades to the next layer, never returning a bogus `None.so`.

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
The pure seam `domain.shortcut_data.resolve_emulator_invocation(rom, active_core_so)` renders the invocation:

- `active_core_so` set → the `-e` form:

  ```text
  flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/<core>.so %ROM%"
  ```

- `active_core_so is None` → the plain `flatpak run net.retrodeck.retrodeck`.

`%EMULATOR_RETROARCH%` and `%ROM%` stay as ES-DE placeholders — RetroDECK's `run_game.sh` resolves and single-quotes
them at launch, so a ROM path with spaces or parens is handled. Only the in-sandbox cores directory
(`/var/config/retroarch/cores`) is baked literally; ES-DE's `%CORE_RETROARCH%` variable is **not** expanded through
`-e`, so the plugin bakes the resolved path itself. The `-e` flag makes RetroDECK skip its gamelist lookup entirely,
which is why a baked core applies for any filename (see
[Why the plugin always bakes the core, never the gamelist](#why-the-plugin-always-bakes-the-core-never-the-gamelist)).

**Always `-e`.** Per
[ADR-0012](https://github.com/danielcopper/decky-romm-sync/blob/main/docs/adr/0012-plugin-owns-core-selection-always-e-no-gamelist.md),
every installed ROM bakes its **full resolved active core** through `-e` — the per-game pin, the per-platform core, or
the es_systems default, whichever the resolver returns. The plain `flatpak run` launch is **not** the "no override" case
any more; it is reserved for the single fallback where the resolver yields `(None, None)` (a platform with no resolvable
default at all). Baking the default for every ROM is what lets the plugin own launch selection completely: a launch that
is _not_ `-e` would let RetroDECK consult the gamelist, re-coupling the plugin to ES-DE's state. The cost is that a
RetroDECK update changing a platform's default core needs a **Force Full Sync** to re-bake — see
[A frozen default needs a Force Full Sync](#a-frozen-default-needs-a-force-full-sync).

### The three bake sites

`launch_options` is written wherever a shortcut's command is (re)built. All three resolve the ROM's **full active core**
through the same `ActiveCoreResolver` seam and pass the `.so` into `resolve_emulator_invocation`, so the read-path core
and the launched core cannot diverge:

| Bake site                                    | When it runs                             | How it resolves the core                                                  |
| -------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `SyncOrchestrator` → `_build_core_overrides` | every sync (preview + apply)             | each ROM through `active_core_for_rom` → `{rom_id: core_so}` for the bake |
| `DownloadService` → `_resolve_bound_app_id`  | on download-complete (install/reinstall) | the ROM through `active_core_for_rom` in the same flow → `.so`            |
| `MigrationService` → `_build_relaunch_items` | on RetroDECK-home migration              | each relocated ROM through `active_core_for_rom` → `.so`                  |

The download-complete bake is the one that re-applies a pin after reinstall — the exact path `roms` storage was chosen
to protect. Each site bakes `-e` for every ROM that resolves to a concrete core, and the plain launch only when the
resolver returns `(None, None)`. A stale LABEL is handled inside the resolver (warn + degrade), so no bake site ever
emits `None.so`.

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
launcher for V1 — this is the correct V1 shape, not a placeholder. The per-ROM **selection** (does this ROM have an
override?) is a service-layer DB read; the seam only **renders** the chosen invocation into a command string. The
multi-emulator lift ([#129](https://github.com/danielcopper/decky-romm-sync/issues/129) /
[#918](https://github.com/danielcopper/decky-romm-sync/issues/918)) is a near-mechanical extraction of that one seam
into a RetroDECK adapter behind a `Frontend`-style port; a sibling emulator's launch argv is net-new work, so the port
is not built until a second emulator is concrete.

---

**Related pages:**

- [Backend Architecture](backend-architecture.md) — service/adapter layering, dependency diagram
- [Config Source Parsers](config-source-parsers.md) — one-parser-per-source principle; how `es_systems.xml` and
  `core_defaults.json` are read (the gamelist is no longer read)
- [Steam Non-Steam Shortcuts](steam-non-steam-shortcuts.md) — AddShortcut API, `launch_options`, app-id derivation
- [Database Design](database-design.md) — the `Rom` aggregate and the `roms` table
- [BIOS and Emulator Cores](../user-guide/bios-management.md) — the user-facing core-selection guide
