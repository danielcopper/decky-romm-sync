# Folder-boot systems bake the game directory as the launch target; `file_path` stays the launch FILE anchor

## Status

Accepted. Extends [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options` model)
and [ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md) (the bake-time
launch-path override layer) with a folder-as-target branch, and **refines**
[ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (`file_path` = launch file, `rom_dir` = dedicated folder) by
recording that for a folder-boot system the baked launch **target** deliberately differs from `file_path`. Tracked under
[#1212](https://github.com/danielcopper/decky-romm-sync/issues/1212) (part of the #914 standalone-emulator epic,
surfaced by the #1208 PS3-standalone routing).

## Context

With standalone routing ([#1208](https://github.com/danielcopper/decky-romm-sync/issues/1208)) a PS3 ROM launches via
`%EMULATOR_RPCS3% --no-gui %ROM%`. A PS3 game installs as a folder whose launchable payload is the nested
`…/PS3_GAME/USRDIR/EBOOT.BIN`, and `detect_launch_file` ([ADR-0008](0008-rom-install-launch-file-and-rom-dir.md)) picks
exactly that EBOOT as the install's `file_path` — correct as the launch **file** identity (the payload that "is" the
game), and load-bearing for save-path resolution, core resolution, and the displayed filename.

But RPCS3's directory-boot rejects the nested EBOOT and wants the game **root folder** — the directory that _contains_
`PS3_GAME` (hardware-confirmed, RPCS3 0.0.40):

```text
Booting '…/PS3_GAME/USRDIR/EBOOT.BIN' failed
Reason: Invalid file or folder
Emulation is stopped
```

So the launch **target** (what gets baked into the argv the emulator receives) and the launch **file** (`file_path`, the
payload identity) are two different axes for a folder-boot system. #1208 got the _invocation_ right (`--no-gui`); the
remaining gap is the _target_: the baked `%ROM%` must be the game directory, not the EBOOT.

This is the exact shape [ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md)
already established for the disc picker — a per-game deviation the plugin owns, applied as a **bake-time launch-path
override** that changes only the argument baked into `launch_options` and never rewrites `file_path`. A folder-boot
system is another value of the same axis: "which path does this ROM actually launch with?" The answer is the game
folder, not the file `detect_launch_file` chose.

## Decision

### 1. Folder-boot identity is a small hardcoded marker table in `domain/`

`domain/rom_files.py` (next to `detect_launch_file`, which owns the `EBOOT.BIN` literal) hardcodes the format-semantic
fact es_systems cannot express — that a PS3 layout's trailing `PS3_GAME/USRDIR/EBOOT.BIN` run identifies a
launch-the-folder game:

```python
FOLDER_BOOT_MARKERS = (("PS3_GAME", "USRDIR", "EBOOT.BIN"),)
```

Matched **case-sensitively** — the on-disk layout is standardised uppercase, so a lowercase `ps3_game` is not a
folder-boot marker. A future folder-boot system is a **data-only** addition to this tuple, never new control flow.

### 2. `folder_boot_root(launch_path, rom_dir)` — pure path algebra, `domain/`

A pure function derives the game root from a launch path: when the path's trailing components match a marker, it strips
those components and returns the remaining game root. This also collapses a one-level-deeper extract
(`rom_dir/<Game>/PS3_GAME/USRDIR/EBOOT.BIN` → `rom_dir/<Game>`). It returns the root **only** when both guards hold:

- **`rom_dir` is set.** A single-file ROM (`rom_dir is None`, [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md))
  owns no folder and is never a folder-boot game.
- **The derived root is inside-or-equal `rom_dir`.** A pathological bare `<roms>/<system>/PS3_GAME/USRDIR/EBOOT.BIN`
  sitting directly in the shared system directory would strip to that shared directory; the containment guard rejects it
  (the stripped root is `rom_dir`'s parent, not inside it), so the shared directory is never baked as a launch target.

Otherwise it returns `None` and the caller keeps the launch file unchanged. Stdlib-only, no I/O — it belongs in
`domain/` beside the other pure launch-file logic.

### 3. Application: one seam in `DiscLaunchResolver`, `file_path` never rewritten

The override is applied at exactly one place: `DiscLaunchResolver.resolve_bake_path`
([ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md)'s single read seam), after
the existing disc resolution:

```python
path, stale = resolve_launch_path(install.file_path, discs, selected_disc)
# …warn on a stale pin…
root = folder_boot_root(path, install.rom_dir)
return root or path
```

Because it fires only when the resolved path still carries a folder-boot marker, it composes cleanly with disc
resolution: a resolved disc path (`…/Game (Disc 2).cue`) carries no marker, so a multi-disc ROM is never
folder-stripped; the folder rule applies only when disc resolution returned `file_path` unchanged.
**`RomInstall.file_path` is never rewritten** — it stays the EBOOT
([ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) anchor), so save-path resolution, core resolution, and the
displayed filename are untouched, exactly as the disc override and the `-e` core override leave them.

All **seven** launch-bake sites draw their path from this one seam (`resolve_bake_path`, reached directly or through
`resolve_for_install`): sync scan/apply, download-complete, per-game core set/clear, migration relaunch, the startup
relaunch reconcile, and the disc picker. They inherit the folder-boot target with **zero call-site edits**. A ROM
installed before this change self-heals on its next sync (or any re-bake), because every bake now resolves through the
override.

## Consequences

- **PS3 folder games launch.** RPCS3's directory-boot receives the game folder it expects instead of the nested EBOOT it
  rejects.
- **No new launch mechanism, no `file_path` rewrite.** The folder path rides the existing baked-`launch_options` layer;
  the launcher stays a pure `exec "$@"` wrapper ([ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md));
  every `file_path`-derived value (save path, core, filename) is unchanged.
- **The hardcoded surface is minimal.** Exactly one marker, matched case-sensitively; a new folder-boot system is a
  data-only tuple entry. The two guards (`rom_dir` set, root inside `rom_dir`) keep a stray EBOOT from ever baking the
  shared system directory.
- **Existing installs self-heal.** The override lives entirely in the bake resolution, so no migration or re-download is
  needed — the next sync/re-bake emits the folder target.
- **One documented degrade path.** The `downloads.py` raw-`file_path` fallback edge (used only in the rare race where
  the install row is not yet readable at download-complete) does not run through the resolver, so it bakes the raw path.
  This is an acceptable, transient degrade — the next sync heals it — and is not worth threading the override through a
  second, edge-only site.

## Alternatives considered

- **Re-point `RomInstall.file_path` to the game folder.** Rejected, same reason
  [ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md) rejected it for the disc
  pick: `file_path` is the launch **file** identity that save-path resolution, core resolution, and the displayed
  filename all derive from ([ADR-0008](0008-rom-install-launch-file-and-rom-dir.md)). None of those should change
  because the emulator wants a directory argument. A bake-time path override changes only the launch command and leaves
  every `file_path`-derived value stable — the layering the disc and core overrides already use.
- **ES-DE's `.ps3` / `.ps3dir` folder-rename convention.** ES-DE collapses a PS3 game folder into a single entry when it
  is renamed `<Game>.ps3dir` (or paired with a `.ps3` file), which also signals "launch the directory." Rejected: the
  plugin does not launch through ES-DE's gamelist for owned shortcuts — it bakes the full command into a Steam
  shortcut's `launch_options` ([ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md)), so an ES-DE naming
  convention buys nothing here and would add a folder-rename step (and the ES-DE-collapse hazard ADR-0013/#1111 already
  navigated) for a mechanism the plugin does not use.
- **A RetroDECK `.desktop` shortcut mechanism.** Rejected for the same structural reason: it is a different launch
  channel than the plugin's baked-`launch_options` model, adding a second launch path to maintain when the existing
  bake-time override answers the question directly.

## Deferred (deliberately)

- **The PSN `<serial>/USRDIR` layout.** Digital PSN PS3 titles can install under a `<title-id>/USRDIR/…` shape without a
  `PS3_GAME` directory. No real library case has surfaced, and the marker table is a data-only extension point, so a
  second marker is added when (if) such a ROM appears — not speculatively.
- **Renaming `DiscLaunchResolver`.** The seam is now a general "which path does this ROM launch with?" resolver (disc +
  folder-boot), so its disc-specific name is slightly narrow. A rename is cosmetic and touches every wiring site; it is
  left for a follow-up rather than bundled here.

## On-device verification (gates the merge)

- RPCS3 (via RetroDECK) boots a PS3 game when handed the **game root folder** as `%ROM%` (the `--no-gui <dir>` form).
- RetroDECK's `run_game.sh` accepts a **directory** argument for the RPCS3 path (the standalone invocation from #1208
  passes the baked folder through unchanged).
- A previously-synced PS3 ROM re-bakes to the folder target on the next sync (the self-heal path), and its saves / core
  badge / displayed filename are unchanged (proving `file_path` was not perturbed).

See also: [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (`file_path` = launch file, `rom_dir` = dedicated
folder — refined here), [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options`
model the folder path rides),
[ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md) (the bake-time launch-path
override this extends — same seam, same "never rewrite `file_path`" discipline),
[Core and Emulator Selection](../architecture/core-emulator-selection.md#folder-boot-launch-target) (the resolver, the
bake sites, and how the folder target composes with the disc path and the core).
