# `.m3u` playlists are gated on ES-DE's own `es_systems.xml` extension list, never on file extension alone

## Status

Accepted. Fixes [#1111](https://github.com/danielcopper/decky-romm-sync/issues/1111). Refines the multi-file download
layout of [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (`file_path` launch target + `rom_dir` folder) by
correcting how the launch file is chosen and how the collapse folder is named for non-disc systems.

## Context

A multi-file ROM is extracted into a per-game folder that ES-DE collapses into a single library entry only when the
folder name matches the launch file's full name with extension (ADR-0008). The plugin both (a) prefers a `.m3u` as the
launch file in `detect_launch_file` and (b) auto-generates a game-named `.m3u` in `needs_m3u` when the disc-file count
warrants it — so the collapse folder ends up named `<Game>.m3u/` and the emulator can switch discs.

Two facts broke this for systems that have no playlist concept:

1. **RomM bundles a platform-blind `.m3u` into the ZIP for _every_ multi-file game** — Switch and Xbox 360 included. The
   plugin then preferred that `.m3u` as the launch file and named the folder `<Game>.m3u/`.
2. **The decision was made purely on file extension, with no platform awareness.** Switch (`.nsp`) and Xbox 360
   (`.iso`/`.zar`) therefore got an `.m3u` launch file and a `<Game>.m3u/` folder — but ES-DE does not list `.m3u` as a
   supported extension for those systems, so the folder never collapsed and the launch pointed at a file the emulator
   cannot read. Verified on-device.

The core ambiguity is that **`.iso` is platform-ambiguous**: a PS2 `.iso` (or GameCube/Wii `.iso`) belongs to a system
ES-DE lists `.m3u` for, while an Xbox 360 `.iso` belongs to one it does not. No rule that looks only at the extension
can separate them — the same byte sequence wants opposite handling depending on the platform. The decision needs
platform input, and it needs an authority on which platforms accept an `.m3u` that can never disagree with the program
that actually does the collapsing.

ES-DE already publishes exactly that authority. Its `es_systems.xml` carries a per-system `<extension>` list, and ES-DE
reads that same list to decide directory-collapse. The plugin already parses `es_systems.xml` in `CoreResolver`
(`adapters/es_de_config.py`) for the system-layer default core, so the extension data is one parser extension away.

## Decision

- **Gate `.m3u` on whether ES-DE lists `.m3u` as a supported extension for the ROM's system**, read from the same
  `es_systems.xml` ES-DE uses to decide collapse. `CoreResolver` parses the `<extension>` list per system and exposes
  `system_supports_m3u(system_name) -> bool`. The answer can never disagree with ES-DE because it _is_ ES-DE's own data.
- **The capability crosses into the domain as a plain `bool`.** A new call-shaped Protocol `SystemM3uSupportFn`
  (`services/protocols/`) is threaded into `DownloadService` from bootstrap (bound to
  `core_resolver.system_supports_m3u`); `DownloadService` resolves it once per download from the ROM's `system` and
  passes the bool down. The domain functions `needs_m3u(disc_files, m3u_supported)` and
  `detect_launch_file(files, m3u_supported)` stay pure — they receive the bool, never a system name and never an
  adapter.
- **When unsupported, no `.m3u` is generated and the bundled one is never chosen as the launch file.** `needs_m3u`
  short-circuits to `False`; `detect_launch_file` drops its `.m3u` preference, so selection falls through to the real
  game file (`.cue` / … / largest) and the collapse folder is named after that file (`<Game>.nsp/`, `<Game>.iso/`).
- **The bundled `.m3u` is left inert on disk, never deleted.** Ignoring it is non-destructive; removing a file the user
  downloaded is not warranted (no-destructive-op rule).
- **Default-safe when `es_systems.xml` is absent.** `system_supports_m3u` returns `False` for an unknown system or when
  the file cannot be found. A missing `.m3u` only degrades (no auto-disc-switch); a wrong one breaks the launch — so the
  safe default is "no m3u."

On-device the supported set includes `psx`/`ps2`/`saturn`/`segacd`/`pcenginecd`/`tg-cd`/`dreamcast`/`gc`/`wii`; `switch`
and `xbox360` are not in it.

## Consequences

- Switch and Xbox 360 multi-file games collapse to their real game file with a launch that works; the spurious
  `<Game>.m3u/` folder is gone. Disc-swapping consoles keep their generated `.m3u` and disc switching unchanged.
- The supported-system set is **maintenance-free**: it is ES-DE's own extension list, not a hand-kept allowlist in this
  repo, so it tracks ES-DE automatically and cannot drift out of agreement with the collapse it drives.
- The `.iso` ambiguity is resolved by construction — a PS2 `.iso` and an Xbox 360 `.iso` get opposite handling because
  their systems carry different extension lists, with no extension-level special-casing.
- **Actually switching discs on emulators that have no `.m3u` concept is a separate, deferred feature.** This ADR only
  stops the broken `.m3u`; a multi-disc Xbox 360 game still launches a single disc. Cross-emulator disc switching — a
  disc picker on the Play button — is tracked in [#865](https://github.com/danielcopper/decky-romm-sync/issues/865).
- Existing installs from before the fix keep their old layout (and any stray `.m3u`) until re-downloaded; the fix only
  affects new downloads.
- The static `core_defaults.json` fallback carries no extension data, so when `es_systems.xml` is absent every system
  reads as unsupported. That is the intended default-safe behavior, not a gap to backfill — inventing extension data
  there would reintroduce a hand-maintained allowlist.

## Alternatives considered

- **Hand-maintained platform allowlist** (a set of systems that get an `.m3u`, kept in this repo). Rejected: it is a
  second source of truth that must be kept in sync with ES-DE's collapse behavior by hand. Every new or renamed system,
  and every ES-DE change, is a chance for the allowlist to disagree with the program that actually collapses the folder
  — the exact class of drift this fix removes. ES-DE's own `<extension>` list is the authority; mirroring it by hand
  only adds a way to be wrong.
- **Extension-only heuristic** (decide from the file extensions alone, e.g. "`.iso` is never an m3u system"). Rejected:
  `.iso` is platform-ambiguous — a PS2 / GameCube / Wii `.iso` wants an `.m3u`, an Xbox 360 `.iso` must not get one. No
  rule over extensions alone can tell them apart, because the same extension maps to opposite handling depending on the
  platform. The decision is fundamentally platform-scoped.
- **Strip / delete the RomM-bundled `.m3u` for unsupported systems.** Rejected: deleting a downloaded file is a
  destructive op for no benefit. Ignoring it (never choosing it as the launch file) is sufficient and reversible.

See also: [ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (launch file / `rom_dir`),
[ADR-0010](0010-normalize-romm-slug-to-retrodeck-system.md) (system identity that keys this lookup),
[#865](https://github.com/danielcopper/decky-romm-sync/issues/865) (deferred cross-emulator disc picker).
