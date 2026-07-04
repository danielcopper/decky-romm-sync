# Folder-boot systems bake the game directory as the launch target; `file_path` stays the launch FILE anchor

## Status

Accepted, **revised after on-device testing** (see "Empirical revision" below). Extends
[ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options` model) and
[ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md) (the bake-time launch-path
override layer) with a folder-as-target branch, and **refines** [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md)
(`file_path` = launch file, `rom_dir` = dedicated folder) by recording that for a folder-boot system the baked launch
**target** deliberately differs from `file_path`. Tracked under
[#1212](https://github.com/danielcopper/decky-romm-sync/issues/1212) (part of the #914 standalone-emulator epic,
surfaced by the #1208 PS3-standalone routing).

### Empirical revision (on-device, RPCS3 0.0.40 via RetroDECK)

The original decision (§1–§3) baked the game folder into the **standard `run_game.sh` `-e` form**
(`… -e "%EMULATOR_RPCS3% --no-gui %ROM%" "<folder>"`) on the assumption that `run_game.sh` passes a directory `%ROM%`
through to the emulator. On-device testing **falsified that assumption** and surfaced three more dump-specific quirks.
The folder-as-target decision (§1–§3, the `resolve_bake_path` path override) **stands**; how the folder is handed to the
emulator changes, and two download-path heals are added:

1. **`run_game.sh` can never launch a bare game folder.** `files/libexec/run_game.sh:63-67` reinterprets **any**
   directory argument as ES-DE's "directory as a file": `game="$game/$(basename "$game")"`. Our folder bake `<dir>`
   becomes `<dir>/<dirname>`, which does not exist — RPCS3 gets a bogus path. So the `-e` standalone form cannot deliver
   a folder target (§4 replaces it with a direct sandbox invocation).
2. **RPCS3 rejects the nested EBOOT too** (`Invalid file or folder`) — confirming the folder, not the EBOOT, is the only
   valid target, so there is no working `run_game` fallback.
3. **The disc dump ships `PS3_DISC.SFB` renamed `PS3_DISC.SFB.txt`.** RPCS3 identifies a disc-dump folder by
   `PS3_DISC.SFB` at the game root; the game does not boot until it is restored (§5).
4. **The multi-file download path generated a junk M3U playlist** listing the folder's payload files as "discs" (PS3
   counts as `.m3u`-supported). It did not break the launch but is wrong and must stop (§6).

The direct command below was hardware-verified to boot the game:

```text
flatpak run --command=/app/retrodeck/components/rpcs3/component_launcher.sh net.retrodeck.retrodeck \
  --no-gui "/run/media/deck/Emulation/retrodeck/roms/ps3/Metal Gear Solid 4"
```

(The component launcher just sets `LD_LIBRARY_PATH` and `exec`s `bin/rpcs3`; its `log:` not-found stderr lines are
cosmetic.)

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

### 4. Folder-boot targets bake a direct sandbox invocation, bypassing `run_game.sh`

Because `run_game.sh` cannot launch a bare folder (Empirical revision #1), a folder-boot standalone is baked as a
**direct sandbox invocation** that runs the emulator's own launcher inside the RetroDECK flatpak, bypassing
`run_game.sh` entirely:

```text
flatpak run --command=<sandbox-launcher> net.retrodeck.retrodeck <template-args> "<folder>"
```

The decision is made on the **invocation** seam, mirroring how §3 makes the **path** decision on the `resolve_bake_path`
seam — both keyed on the same `folder_boot_root` fact, so a folder path and a direct invocation are always chosen
together. `ActiveCoreResolver.active_emulator_for_rom`, after the per-game/per-platform/system precedence resolves an
`EmulatorInvocation`, rewrites it when the emulator is a **standalone** and the ROM's install is a folder-boot layout
(`folder_boot_root(install.file_path, install.rom_dir)` is set — the same fact, provably consistent with the bake path
because a folder-boot layout is never multi-disc):

- **`<sandbox-launcher>`** is resolved by `CoreResolver.resolve_sandbox_launcher(command)` (`adapters/es_de_config.py`),
  reusing the `es_find_rules.xml` parse the standalone existence probe already reads: it takes the command's
  `%EMULATOR_<NAME>%` token, looks up the find-rule `staticpath` entries, and returns the sandbox-absolute RetroDECK
  **component** launcher (`/app/retrodeck/components/rpcs3/component_launcher.sh` for RPCS3; the `/app` bundled entry is
  preferred over a `/var/data` external one). Host-native entries (`~/…` AppImages, host flatpak exports) are skipped —
  they are not reachable as a sandbox `--command`. The `/app` path is what `flatpak run --command=` execs inside the
  sandbox.
- **`<template-args>`** are the standalone command's middle: `%EMULATOR_*%` and the trailing `%ROM%` stripped
  (`%EMULATOR_RPCS3% --no-gui %ROM%` → `--no-gui`). The game folder is appended by `build_launch_options`.

The result is a new `EmulatorInvocation` variant, `kind="direct"` (`domain/shortcut_data.py`), carrying the standalone
command plus the resolved launcher; `resolve_emulator_invocation` renders the `--command=` form. Every other system
keeps the `run_game` `-e` form **byte-identical** — the rewrite fires only for a standalone over a folder-boot install.
If the sandbox launcher cannot be resolved (find rules absent, host-only rule), the standalone `-e` form is kept and a
warning is logged; the launch will fail (no working fallback exists for a folder target) until a re-bake with readable
find rules heals it. Because the rewrite lives on the shared `active_emulator_for_rom` seam, all seven bake sites
inherit it with **zero call-site edits**, exactly as the path override does.

### 5. Heal a `.txt`-suffixed `PS3_DISC.SFB` at extract

After a multi-file extraction (`DownloadService._post_download_multi_io`), a folder-boot layout is healed for the known
dump quirk (Empirical revision #3): if `<game-root>/PS3_DISC.SFB.txt` exists and `<game-root>/PS3_DISC.SFB` does not,
the `.txt` is **copied** to the correct name (the original is kept), with one INFO log. The game root is located by
`domain.folder_boot_layout_root` (the marker-stripped dir that holds `PS3_GAME`); the copy goes through a new
`DownloadFileStore.copy_file` seam (no raw I/O in the service). Scoped to this exact quirk only — a real `PS3_DISC.SFB`
already present, no `.txt`, or a non-folder-boot layout are all left untouched. A stray SFB from an older download is
not re-created; a re-download heals it.

### 6. Suppress M3U generation for a folder-boot layout

`DownloadService._maybe_generate_m3u_io` returns early when `folder_boot_layout_root` finds a folder-boot marker in the
extract, even though ES-DE lists `.m3u` for PS3 (Empirical revision #4). A folder-boot game launches its directory
directly and never a playlist, and its many payload files must never be misread as discs. A genuine multi-disc set (no
marker) still generates its playlist.

- **PS3 folder games launch.** RPCS3 receives the game folder it expects, via a direct sandbox invocation that bypasses
  `run_game.sh`'s directory-as-a-file reinterpretation — not the nested EBOOT it rejects, and not the `-e` form
  `run_game.sh` would mishandle.
- **A new launch mechanism for one case, no `file_path` rewrite.** Folder-boot standalones bake the `--command=`
  direct-sandbox form (§4); every other system keeps the `run_game` `-e` form byte-identical. The launcher stays a pure
  `exec "$@"` wrapper ([ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md)) — the whole command,
  `--command=` and all, still rides `launch_options`; every `file_path`-derived value (save path, core, filename) is
  unchanged.
- **The hardcoded surface is minimal.** Exactly one marker, matched case-sensitively; a new folder-boot system is a
  data-only tuple entry. The two guards (`rom_dir` set, root inside `rom_dir`) keep a stray EBOOT from ever baking the
  shared system directory. The sandbox-launcher resolution reuses the existing `es_find_rules.xml` probe — no new I/O
  surface.
- **Existing installs self-heal.** Both overrides live in the bake resolution (path) and the invocation resolution, so
  no migration or re-download is needed — the next sync/re-bake emits the folder target and the direct invocation
  together. (An SFB `.txt` quirk or a stray junk M3U from an earlier download is healed/avoided on the next re-download,
  §5–§6.)
- **One documented degrade path.** The `downloads.py` raw-`file_path` fallback edge (used only in the rare race where
  the install row is not yet readable at download-complete) bakes the raw EBOOT path; `active_emulator_for_rom` may
  still read the just-committed install and emit the direct form, so the race can transiently pair the direct invocation
  with the EBOOT path. Both the folder-with-`-e` and the direct-with-EBOOT shapes are broken for that one launch and
  heal on the next sync — an acceptable, transient degrade, not worth threading the override through a second, edge-only
  site.

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
- **A RetroDECK `.desktop` shortcut mechanism.** Rejected. A `.desktop` file is `run_game.sh`'s **own** mechanism for a
  directory game (`run_game.sh:42-61` reads its `Exec=` line and `eval`s it, with a hardcoded `%%RPCS3_GAMEID%%`
  workaround), so routing through it means re-entering `run_game.sh` — the layer we must bypass — and depending on ES-DE
  having generated the sidecar. We instead bake a **direct sandbox command** (§4) that runs the same emulator launcher
  `run_game.sh` would ultimately reach, without its directory-as-a-file rewrite and without a generated sidecar. Same
  baked-`launch_options` model, one fewer moving part.
- **Keep the `-e` standalone form and let `run_game.sh` handle the folder.** Falsified on-device (Empirical revision
  #1): `run_game.sh:63-67` rewrites a directory argument to `<dir>/<dirname>`, which does not exist, so the `-e` form
  can never deliver a folder target. The direct `--command=` invocation (§4) is the bypass.

## Deferred (deliberately)

- **The PSN `<serial>/USRDIR` layout.** Digital PSN PS3 titles can install under a `<title-id>/USRDIR/…` shape without a
  `PS3_GAME` directory. No real library case has surfaced, and the marker table is a data-only extension point, so a
  second marker is added when (if) such a ROM appears — not speculatively.
- **Renaming `DiscLaunchResolver`.** The seam is now a general "which path does this ROM launch with?" resolver (disc +
  folder-boot), so its disc-specific name is slightly narrow. A rename is cosmetic and touches every wiring site; it is
  left for a follow-up rather than bundled here.

## On-device verification (gates the merge)

- **The direct command boots the game** (verified): the baked
  `flatpak run --command=<launcher> net.retrodeck.retrodeck --no-gui "<folder>"` starts RPCS3 on the game folder.
  (`run_game.sh` does **not** accept a directory — the earlier assumption — which is exactly why the direct command
  bypasses it.)
- **A previously-synced PS3 ROM re-bakes to the direct invocation** on the next sync (Force Full Sync → the shortcut's
  `launch_options` becomes the `--command=` form), and MGS4 boots from the Steam shortcut. Its saves / core badge /
  displayed filename are unchanged (proving `file_path` was not perturbed).
- **A fresh multi-file PS3 download** heals a `.txt`-suffixed `PS3_DISC.SFB` and generates **no** M3U (§5–§6), and the
  installed game boots from its shortcut.

See also: [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (`file_path` = launch file, `rom_dir` = dedicated
folder — refined here), [ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md) (the baked-`launch_options`
model the folder path rides),
[ADR-0014](0014-per-game-disc-selection-in-db-applied-as-bake-time-launch-path-override.md) (the bake-time launch-path
override this extends — same seam, same "never rewrite `file_path`" discipline),
[Core and Emulator Selection](../architecture/core-emulator-selection.md#folder-boot-launch-target) (the resolver, the
bake sites, and how the folder target composes with the disc path and the core).
