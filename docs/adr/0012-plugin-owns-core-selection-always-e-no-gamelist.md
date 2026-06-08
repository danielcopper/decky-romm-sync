# The plugin owns core selection end to end: per-platform core in `settings.json`, every installed ROM baked with `-e`, the ES-DE gamelist never read or written

## Status

Accepted. **Partially supersedes [ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md):** §2
(`-e`-only-for-overrides) is reversed to **always `-e`** for every installed ROM, and §4 (the one remaining system-level
gamelist write is kept) is reversed to **all gamelist read and write dropped**. It **refines §3** — the precedence chain
loses its gamelist `<alternativeEmulator>` layer and gains a per-platform `settings.json` layer. ADR-0011 §1 (the
per-game override is a LABEL on the `Rom` aggregate), §5 (RetroDECK is the V1 target), and the `-e` mechanics it
established are unchanged. Tracked under [#947](https://github.com/danielcopper/decky-romm-sync/issues/947), epic
[#945](https://github.com/danielcopper/decky-romm-sync/issues/945).

## Context

ADR-0011 moved the **per-game** override out of ES-DE's `gamelist.xml` and into the plugin's own DB, applied through
RetroDECK's `-e` flag. It left two things touching the gamelist:

- the plugin still **read** the system-level `<alternativeEmulator>` as a precedence layer, and
- the plugin still **wrote** the system-level `<alternativeEmulator>` when the user picked a per-system core on the
  System page.

That residue forces a contradiction once two requirements are taken together:

1. **Stop writing another system's config.** `gamelist.xml` is ES-DE's file, in ES-DE's format, on ES-DE's update cycle.
   The system-level write reproduced the exact fragility ADR-0011 documented for the per-game write: when ES-DE has set
   a system-wide alternative emulator it writes `<alternativeEmulator>` as a sibling of `<gameList>` at the document
   root — multi-root XML that ES-DE tolerates but the plugin's strict expat parser rejects. A single remaining write
   into a foreign, strict-parser-hostile config is a standing liability with no upside the plugin can't get elsewhere.
2. **An ES-DE-set core choice must never affect a plugin launch.** A core a user picks **inside ES-DE's own UI** is
   ES-DE's state, not the plugin's. As long as any plugin launch path can read it, the plugin's resolved core can
   silently diverge from what its BIOS badge, per-core save path, and core-change warning assume — exactly the
   read-path-equals-launch-path invariant the resolver exists to protect.

The only launch mechanism that satisfies **both** at once is **baking the resolved core into `-e` for every installed
ROM**. A plain `flatpak run net.retrodeck.retrodeck "<rom>"` launch lets RetroDECK's `run_game.sh` consult the gamelist
itself — so any launch that is _not_ `-e` re-opens requirement 2 through the back door, regardless of whether the plugin
reads the gamelist. `-e` sets the emulator invocation directly and skips RetroDECK's gamelist lookup entirely (the same
property ADR-0011 relied on to dodge the metacharacter-fragile awk match,
[#210](https://github.com/danielcopper/decky-romm-sync/issues/210) /
[RetroDECK#1358](https://github.com/RetroDECK/RetroDECK/issues/1358)). Once every installed ROM launches via `-e`, the
gamelist is no longer on any plugin code path — neither read nor write — so requirement 1 falls out for free.

This makes the plugin the **sole owner of core selection** for its own launches. The system-level deviation that used to
live in the gamelist now needs a plugin-owned home, and `settings.json` is the natural one under
[ADR-0003](0003-json-sqlite-persistence-boundary.md): a per-platform core is a flat, user-set intent toggle with no
relationships — bucket 1.

## Decision

### 1. Per-platform core selection lives in `settings.json`, not the gamelist and not the DB

A `platform_cores` map (`{platform_slug: core_label}`) in `settings.json` holds the user's per-platform core choice as a
**LABEL**, exactly as the per-game override stores one. An absent key means "no per-platform deviation — follow the
es_systems default." This is an ADR-0003 **bucket-1** value (user-intent config, flat, no relationships), so it does
**not** get a SQLite aggregate and there is still **no `Platform` aggregate** (per
[ADR-0003](0003-json-sqlite-persistence-boundary.md) and the `platform_slug` glossary entry — reintroduced only when a
concrete relational need lands, not speculatively). The per-game override is unchanged: it stays a LABEL on the `Rom`
aggregate (`roms.emulator_override`, ADR-0011 §1), surviving uninstall/reinstall per
[ADR-0007](0007-rom-retention-identity-anchor.md).

### 2. Always `-e`: every installed ROM bakes its resolved active core

Every installed ROM's `launch_options` carries the `-e` form with the **full resolved active core** baked in. The plain
`flatpak run` launch is no longer the "no override" case — it is reserved for the single genuine fallback where the
resolver yields `(None, None)` (an unresolvable platform with no default at all). The pure seam
`domain.shortcut_data.resolve_emulator_invocation(rom, active_core_so)` already returns the plain launch on
`active_core_so is None`, so the always-`-e` rule is expressed entirely by the resolver returning a concrete `.so` for
every resolvable platform.

### 3. One precedence chain, gamelist layer removed, per-platform layer added

`ActiveCoreResolver.active_core_for_rom(rom_id) -> (core_so, label)` stays the single read seam; the launched core is
baked from the same resolution. The precedence is now:

> **per-game DB `emulator_override` → per-platform `settings.json` `platform_cores` → es_systems default (live) →
> `core_defaults.json`.**

The gamelist `<alternativeEmulator>` layer of ADR-0011 §3 is gone; the per-platform `settings.json` layer takes its
place between the per-game pin and the es_systems default. The system-layer fallback (`CoreResolver.get_active_core`)
now resolves the live es_systems default and the bundled `core_defaults.json` only — it reads no gamelist. A pinned
per-game or per-platform LABEL that no longer resolves (a core a RetroDECK update removed) degrades to the next layer
with a WARNING and never produces a bogus `None.so`.

### 4. `set_system_core` writes settings and fans out a re-bake; `clear_game_core` bakes the resolved core

Setting a per-platform core (the System-page dropdown) writes `platform_cores` to `settings.json` through the injected
`SettingsPersister`, then **fans out**: every installed **and** shortcut-bound ROM on that platform, except those
carrying a per-game `emulator_override` (the pin wins), is re-baked from the shared resolver and its
`{app_id, launch_options}` is returned for the frontend to confirm-set on the live Steam shortcut. Because always-`-e`
makes the "default" itself a concrete core, **`clear_game_core` now bakes the ROM's resolved active core** (the
per-platform or es_systems default, in `-e` form) rather than reverting to a plain launch — clearing the per-game pin
means "follow the platform default," and the platform default may itself be a per-platform core.

### 5. No migration, no seed; re-apply a per-platform core once

`platform_cores` starts empty. There is no import of any previously-set system-level core (from the gamelist or
anywhere). A user who had set a per-system core in the gamelist re-applies it once on the System page. This mirrors
ADR-0011's no-migration stance for per-game cores and avoids ever parsing the multi-root gamelist the design exists to
stop touching.

## Consequences

- **The plugin is the single owner of core selection for its launches.** No plugin launch path consults the gamelist, so
  an ES-DE-set core can never silently diverge from the plugin's BIOS badge, per-core save path, or core-change warning.
  Requirement 2 holds structurally, not by careful reading.
- **The plugin never writes `gamelist.xml`.** The multi-root-XML parse hazard, the silent-write-failure surface, and the
  ES-DE folder-collapse coupling ([#943](https://github.com/danielcopper/decky-romm-sync/issues/943)) are gone for
  per-platform state the same way ADR-0011 removed them for per-game state. The gamelist is a read-nothing,
  write-nothing file from the plugin's side.
- **A frozen default needs a Force Full Sync to re-bake.** Because the es_systems default is now baked literally into
  every shortcut, a RetroDECK update that ships a **new default core for a platform** does not take effect on a normal
  sync — a normal sync skips platforms whose ROM set is unchanged, so the stale baked core survives. A **Force Full
  Sync** re-bakes every shortcut and picks up the new default. This is the cost ADR-0011 §Alternatives flagged for
  always-`-e`, accepted here because owning launch selection is worth a one-time forced re-sync after a RetroDECK
  default change. A per-platform or per-game core the user sets through the plugin re-bakes immediately (the fan-out /
  the per-game set path), so only the externally-changed default carries this caveat.
- **Setting a per-platform core is instant on installed games.** The fan-out re-bakes every bound shortcut in the same
  call and the frontend confirm-sets them, so the choice applies without a sync. Uninstalled or unbound ROMs pick it up
  on their next download/sync.
- **Per-platform state is one flat config key.** It rides the existing `settings.json` load/save/migration machinery
  (schema bump + a `setdefault("platform_cores", {})` migration), adds no table, and keeps the persistence boundary
  honest under ADR-0003: a user-set, relationship-free toggle belongs in `settings.json`.
- **No `Platform` aggregate.** Storing the per-platform core as a flat map keeps the YAGNI posture of ADR-0003 — a
  platform-scoped aggregate is still introduced only when concrete relational state forces it.

## Alternatives considered

- **Keep reading the gamelist `<alternativeEmulator>` as a precedence layer.** Rejected: it keeps an ES-DE-owned value
  on the plugin's resolution path, so the plugin's resolved core can diverge from an ES-DE-set choice — a direct
  violation of requirement 2. The per-platform `settings.json` layer gives the user the same system-scoped control while
  keeping the value plugin-owned.
- **Minimal "stop the write only" — drop the system-level gamelist write but keep `-e`-only-for-overrides and keep
  reading the gamelist.** Rejected: it satisfies requirement 1 but not requirement 2. ROMs without an override would
  still launch plain, and a plain launch lets RetroDECK read the gamelist, so an ES-DE-set core would still affect the
  plugin's launch. Only always-`-e` closes that path.
- **Store the per-platform core in the DB (a new table or aggregate).** Rejected per
  [ADR-0003](0003-json-sqlite-persistence-boundary.md) on YAGNI grounds: a per-platform core is a flat, user-set,
  relationship-free intent toggle — bucket 1, `settings.json`. It has no invariants a `Platform` aggregate would enforce
  and no relational children, so a table would be storage ceremony without payoff.
- **Migrate the previously-set system-level core out of the gamelist into `settings.json`.** Rejected: a gamelist-import
  path revives the multi-root-XML parse failure this ADR exists to stop touching, for a one-time convenience.
  Re-applying a per-platform core once on the System page is cheap and parser-hazard-free.

See also: [ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md) (the per-game DB override + `-e` mechanics
this ADR builds on and partially reverses), [ADR-0003](0003-json-sqlite-persistence-boundary.md) (the persistence
boundary that places `platform_cores` in `settings.json` and keeps the `Platform` aggregate dropped),
[ADR-0007](0007-rom-retention-identity-anchor.md) (`roms` as the identity anchor the per-game override survives on),
[ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options` model and the
`resolve_emulator_invocation` seam every bake site shares),
[Core and Emulator Selection](../architecture/core-emulator-selection.md) (the resolver, precedence, and bake sites in
detail).
