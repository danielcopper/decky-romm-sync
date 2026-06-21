# Per-game disc selection lives in the plugin DB and is applied as a bake-time launch-path override; disc identity is format-semantic, the per-system accept-list is read live from es_systems

## Status

Accepted. Implements the cross-emulator disc picker deferred by [ADR-0013](0013-platform-gated-m3u-via-es-systems.md)
§Consequences and tracked under [#865](https://github.com/danielcopper/decky-romm-sync/issues/865). Extends
[ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options` model) with a disc-aware
launch-path layer, and **mirrors [ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md) almost exactly** —
the per-game emulator override is the near-identical precedent (a persisted per-game attribute, anchored on `roms`,
excluded from the sync UPSERT, applied at bake time without touching `file_path`). Where this ADR says "mirrors the core
override," that is the structure being reused, not coincidence.

## Context

A multi-disc game (a PS1 RPG that shipped on four CDs, say) installs as a folder of disc images plus, on disc-swapping
systems, an auto-generated `.m3u` playlist that the emulator follows to switch discs in-game
([ADR-0013](0013-platform-gated-m3u-via-es-systems.md)). That `.m3u` is the launch target (`RomInstall.file_path`), and
the emulator's own disc-swap UI handles the rest — on systems that have an `.m3u` concept.

Two gaps remained:

- **Systems with no `.m3u` concept cannot disc-swap at all.** ADR-0013 deliberately stopped generating an `.m3u` for
  systems ES-DE does not list it for (a multi-disc Xbox 360 game launches a single disc and stays there). It explicitly
  deferred "a disc picker on the Play button" to #865 — this ADR.
- **Even on `.m3u` systems, a user may want to launch a specific disc directly** rather than enter through the playlist.

The plugin already owns the full launch command (it bakes `flatpak run net.retrodeck.retrodeck "<file_path>"` into the
Steam shortcut's `launch_options`, [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md)) and already
re-bakes that command through one resolver at three sites for the per-game core override
([ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md)). A disc picker is the **same shape**: a per-game
deviation the plugin owns, resolved into the baked launch path. It does not need a new launch mechanism — it needs to
choose which path gets baked.

The one genuinely new problem is **disc identity**: given a folder of files, which are launchable discs, which are
sidecars, which is the playlist?

### Why es_systems alone cannot supply disc identity

ADR-0013 established that ES-DE's `es_systems.xml` per-system `<extension>` list is the authority on **which extensions
a system accepts**, and that the plugin must read it live rather than hand-maintain an allowlist. It is the right
authority for "can this emulator launch a `.cue` on this system." It is the **wrong** authority for "is a `.cue` a
disc?" — because it carries no per-token role metadata. `es_systems.xml` is a **flat accept-list**: for `psx` it lists
`.cue .chd .iso .bin .m3u …` as a single undifferentiated set. Within that set:

- a `.bin` is a **sidecar** — raw track data owned by, and referenced from, its `.cue`; never launched directly;
- a `.cue` / `.chd` / `.iso` is a **disc** — a single-disc container the emulator opens directly;
- an `.m3u` is a **playlist** — it points at several discs.

es_systems lists all three identically. Nothing in it says the `.bin` is the `.cue`'s sidecar or that the `.m3u` is a
playlist over the others. (A PS1 bin/cue game ships **both** the `.bin` and the `.cue` on disk — verified — so "list
every accepted extension" would offer the user a `.bin` that is not independently launchable.) Disc identity is a
**format-semantic** fact about file shapes, independent of any emulator or system; the accept-list is a **per-system
capability** fact. They are different questions with different owners, and only the second lives in es_systems.

## Decision

### 1. Disc identity is three irreducible hardcoded facts; the per-system accept-list stays live

The plugin hardcodes exactly the format-semantic knowledge es_systems cannot express, and nothing more:

1. **The disc-image set is `{.cue, .chd, .iso}`** (`domain/disc_formats.py`, `DISC_IMAGE_EXTENSIONS`) — the extensions
   that denote a launchable single-disc container.
2. **`.bin` is a sidecar** — excluded simply by not being in the set (a `.cue` references it; it is never the disc
   unit).
3. **`.m3u` is a playlist** — also excluded from the disc set; it is the multi-disc default target, not one disc.

These three are emulator-independent and do not change with the system. Everything system-specific — _which_ of
`{.cue, .chd, .iso}` a given emulator can actually launch — stays **live**, read from es_systems exactly as ADR-0013
reads it. The hardcoded set answers "is this file shape a disc image?"; es_systems answers "does this system accept it?"

### 2. Enumeration = `DISC_IMAGE_EXTENSIONS ∩ es_systems accept-list (live)`

A ROM's discs are enumerated by recursively scanning its install directory (`RomInstall.rom_dir`) and keeping the files
whose extension is in **the intersection** of the hardcoded disc-image set and the system's live es_systems
`<extension>` list (`adapters/es_de_config.py` → `get_supported_extensions(system)`, exposed through the
`SystemSupportedExtensionsFn` Protocol). The intersection means a disc the emulator cannot launch on this system is
never offered. When es_systems is unavailable (an unknown system, the file absent), enumeration **falls back to the full
disc set** rather than intersecting to nothing — listing a disc that might not launch is recoverable; listing none hides
a working picker.

The pure enumeration (`domain/disc_selection.py` → `enumerate_discs`) parses a disc number from each basename
(`(Disc 1)`, `[Disk 02]`, `(Disc 1 of 2)` — case-insensitive, zero-padding-tolerant), orders numbered discs numerically
(so `Disc 2` precedes `Disc 10`) with unparseable basenames last lexicographically, and labels each `"Disc N"` (or the
basename stem when no number parsed). This is one rule for both `.m3u` and no-`.m3u` systems — the disc picker does not
care whether a playlist exists.

### 3. The selection lives on `roms.selected_disc`, mirroring `emulator_override` exactly

A nullable `roms.selected_disc TEXT` column (migration
[`004_add_selected_disc.sql`](https://github.com/danielcopper/decky-romm-sync/blob/main/py_modules/db/migrations/004_add_selected_disc.sql))
holds the **disc basename** the user pinned (e.g. `"Final Fantasy VII (USA) (Disc 2).cue"`). It follows the
`emulator_override` template point for point:

- **`NULL` = no selection** → follow the default (see §4). The column starts NULL with no backfill.
- It anchors on **`roms`, not `rom_installs`**, so the choice **survives uninstall → reinstall** and RetroDECK-home
  migration, per [ADR-0007](0007-rom-retention-identity-anchor.md). The disc folder is gone while uninstalled, but the
  pinned basename re-resolves to a path the moment the folder returns.
- Mutations go through verb-named aggregate methods `Rom.pin_selected_disc(filename)` (rejects a blank filename) and
  `Rom.clear_selected_disc()`. Only `pin`/`clear` ever write the column (via `SqliteRomRepository.set_selected_disc`);
  it is **excluded from the sync UPSERT `SET` clause**, so a re-sync — which builds a fresh `Rom` with
  `selected_disc =
  None` — never wipes a user's pick. The repository drives its `SET` clause from a single
  `_SYNC_COLUMNS` tuple that deliberately omits both `emulator_override` and `selected_disc`, so a subset-omission slip
  is impossible.

We store the **basename**, not a resolved absolute path and not a disc index. The path is re-derived live at bake time
(it changes across uninstall/reinstall and home migration); a stored path would go stale. A stored index would silently
re-point to a different disc if enumeration order changed (a disc file added, removed, or renamed). The basename is the
stable identity that re-resolves correctly as long as that disc is present, and a stale pin (the file genuinely gone)
degrades to the default with a WARNING — never fatal.

### 4. Application: a bake-time launch-path override; `file_path` is never rewritten

The disc selection is a **bake-time path-override layer**, structurally identical to how the `-e` core override
overrides the invocation without touching `file_path`
([ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md) §2). One service-level seam, `DiscLaunchResolver`
(`services/disc_launch_resolver.py`), answers "which file will this multi-disc ROM actually launch with?" by enumerating
the discs (§2), reading `roms.selected_disc`, and resolving (`domain/disc_selection.py` → `resolve_launch_path`):

- **fewer than two discs** (single-disc install, or a folder with one disc) → the ROM's own `file_path` unchanged — zero
  behavior change for the overwhelming majority of games;
- **a valid pin** → that disc's path;
- **no usable selection (NULL, or a stale pin)** → the **default**: the install's `.m3u` when `file_path` is one (the
  in-emulator disc-swap default), else the first enumerated disc.

The resolved path is what each bake site folds into `build_launch_options`. **`RomInstall.file_path` is never
rewritten** — save-path resolution, core resolution, and the displayed filename all derive from `file_path`
([ADR-0008](0008-rom-install-launch-file-and-rom-dir.md), `CONTEXT.md`), and the disc override must not perturb any of
them. It changes only the path argument in the baked launch command.

### 5. One read seam, three bake sites — composes with the core override

`DiscLaunchResolver` is the single read seam, mirroring `ActiveCoreResolver`. The same three sites that re-bake the core
override re-bake the disc path, and at each site the two compose: the resolver yields the **disc path**, the active-core
resolver yields the **core `.so`**, and `resolve_emulator_invocation(rom, core_so)` +
`build_launch_options(invocation,
disc_path)` fold them into one command — a per-game core and a pinned disc on the same
shortcut coexist.

| Bake site                                                              | When it runs                             | How it resolves the disc path                                                          |
| ---------------------------------------------------------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------- |
| `SyncOrchestrator` (`_scan_installed_paths` / `_read_installed_paths`) | every sync (preview + apply)             | each installed ROM through `disc_resolver.resolve_for_install` → `{rom_id: bake_path}` |
| `DownloadService._resolve_bound_app_id`                                | on download-complete (install/reinstall) | the freshly-installed ROM through `resolve_for_install` → `bake_path`                  |
| `MigrationService._build_relaunch_items`                               | on RetroDECK-home migration              | each relocated ROM through `resolve_for_install` against the moved dir                 |

The download-complete bake is the one that re-applies a pin after reinstall — the exact path `roms` storage was chosen
to protect. A stale pin is handled inside the resolver (warn + degrade to default), so no bake site ever emits a
missing-disc path. The `select_disc` callable bakes the same way through the same resolver and returns the new
`launch_options` for the frontend to confirm-set on the live shortcut, so the picker's selection and the baked command
cannot diverge.

## Consequences

- **Cross-emulator disc switching works on every system**, closing the gap ADR-0013 left open. On no-`.m3u` systems the
  picker is the disc-swap mechanism (relaunch the chosen disc); on `.m3u` systems the default still enters through the
  playlist (in-emulator swap) while a user can jump straight to a single disc. One UI, one rule.
- **The selection survives uninstall/reinstall and home migration.** Anchoring on `roms` means the download-complete
  re-bake (the third bake site) re-applies the pin without the user re-picking — the same property `roms` storage buys
  the core override.
- **No new launch mechanism, no `file_path` rewrite.** The disc path rides the existing baked-`launch_options` path; the
  launcher stays a pure `exec "$@"` wrapper ([ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md)); every
  `file_path`-derived value (save path, core, filename) is untouched.
- **The hardcoded surface is minimal and honest.** Exactly three format-semantic facts are baked in; the per-system
  capability stays live in es_systems, so the picker tracks ES-DE's accept-list automatically and never offers a disc
  the emulator cannot launch on that system. The intersection makes the `.iso`-ambiguity ADR-0013 resolved a non-issue
  here too — a PS2 `.iso` is a disc, an Xbox 360 `.iso` is a disc, and each system's accept-list decides launchability.
- **A stale pin is never fatal.** The selected disc no longer present degrades to the default with a WARNING on the bake
  path; the set path hard-fails before writing (an unknown filename is never persisted), so the DB never holds a pin no
  enumeration can resolve.
- **Single-disc games are entirely unaffected.** The resolver returns their own `file_path`, the `get_disc_selection`
  callable answers `{multi_disc: false}`, and the frontend renders no picker — zero footprint.

## Alternatives considered

- **Source disc identity from es_systems alone** (treat every accepted extension as a disc). Rejected: es_systems is a
  flat accept-list with no per-token role metadata, so it cannot tell a `.cue` (disc) from its `.bin` (sidecar) or from
  an `.m3u` (playlist) — all three are listed identically. It would offer the user a `.bin` that is not independently
  launchable. Disc identity is format-semantic; the accept-list is a per-system capability. Only the second belongs in
  es_systems — which is exactly why the design **keeps** the live accept-list (for launchability) and adds the three
  hardcoded facts (for identity), rather than choosing one source for both.
- **Anchor the selection on `rom_installs`.** Rejected: `rom_installs` exists only while the ROM is downloaded, so an
  uninstall → reinstall would erase the pick — and the reinstall re-bake is precisely the site that should re-apply it.
  Anchoring on `roms` (the permanent identity row, [ADR-0007](0007-rom-retention-identity-anchor.md)) survives the
  uninstall window, identical to the core override.
- **Rewrite `RomInstall.file_path` to the selected disc.** Rejected: `file_path` is load-bearing for save-path
  resolution, core resolution, and the displayed filename ([ADR-0008](0008-rom-install-launch-file-and-rom-dir.md)),
  none of which should change because the user picked disc 2. A bake-time path override changes only the launch command,
  leaving every `file_path`-derived value stable — exactly the layering the core override already uses.
- **Store a disc index instead of the basename.** Rejected: an index is positional, so adding, removing, or renaming a
  disc file silently re-points the pin at a different disc. The basename is a stable identity that re-resolves to the
  same disc whenever it is present and cleanly registers as stale (→ default + WARNING) when it is not.

See also: [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (`file_path` = launch target, `rom_dir` = the disc
folder this scans), [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options`
model the disc path rides), [ADR-0011](0011-per-game-core-override-in-db-applied-via-e-flag.md) (the per-game DB
override this mirrors — storage, UPSERT exclusion, one-seam-three-bake-sites),
[ADR-0013](0013-platform-gated-m3u-via-es-systems.md) (platform-gated `.m3u`, which deferred this picker and supplies
the live es_systems accept-list it intersects with),
[Core and Emulator Selection](../architecture/core-emulator-selection.md#multi-disc-selection) (the resolver, the bake
sites, and how the disc path composes with the core in the launch command).
