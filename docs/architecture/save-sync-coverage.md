# Save Sync Coverage

Why some saves sync and others don't. The mechanics live in
[Save File Sync Architecture](save-file-sync-architecture.md); this page explains the **coverage envelope** — which save
files the per-game model can and cannot reach — and the strategy for the gaps. The user-facing result is the
[Save sync support matrix](../user-guide/save-sync-support-matrix.md).

## The per-game discovery model

Save discovery is **exact-name probing**, not a directory scan. For an installed ROM whose file stem is `rom_name`, the
sync looks for exactly `<saves_dir>/<rom_name><ext>` for each extension in a fixed list, and uploads the ones that
exist. There is no glob, no `listdir`, no pattern match.

This is a deliberate bijection: **one ROM → one set of `<rom_name>.<ext>` files in the save folder.** It maps perfectly
onto libretro's own SRAM convention, where the save file mirrors the ROM name. Everything that doesn't fit that shape is
invisible to the sync.

Two hard properties follow, and they define the entire coverage envelope:

1. **The filename must be the ROM stem.** A file named anything else — a fixed card name (`pcsx-card2.mcd`,
   `vmu_save_A1.bin`), or a name with a slot/unit infix (`game.1.mcr`) — is never probed.
2. **The file must live in the save folder.** A save in RetroArch's _system_ directory (Flycast VMUs), in a per-emulator
   subdirectory (`mame/nvram/`), or next to the ROM (`savefiles_in_content_dir`) is never seen.

The extension list is a small static map: a default of `.srm` / `.rtc` / `.sav`, plus per-platform overrides (today only
`nds` → `.dsv` and `segacd` → `.brm`).

## How RomM stores saves

RomM treats a save as a file blob keyed by `(rom_id, slot)`, with an `emulator` tag (which becomes a storage
subdirectory) and an MD5 `content_hash`. It stores the bytes the client sends and leaves their meaning to the client —
there is no `save_type` or memory-card concept, and each save belongs to a single ROM. This format-agnostic design keeps
the server simple and works for any client; RomM's own in-browser player (EmulatorJS) uses the same path, uploading one
save blob per `(game, core)`.

Because both the server and our discovery are organised **per game**, two things follow. Supporting a new _per-game_
save format is a client-side change — the server already accepts the file as-is. And a _shared_ card has no per-game
identity to map onto a `(rom_id, slot)` record, so any shared-card handling is a client-side modelling decision (see
below), not something the server provides or prevents.

## The three coverage classes

Every core's save behavior falls into one of these (plus "no save"):

| Class   | Shape                                                                                  | What it needs                                                                                                                 |
| ------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **(a)** | Per-game, single-token `<rom>.<ext>`, save folder                                      | Just add `<ext>` to the override map. No architecture change.                                                                 |
| **(b)** | Per-game, but a **slot/unit infix** (`<rom>.1.mcr`, `<rom>.A1.bin`)                    | Infix-aware discovery **and** download-target derivation (the current code drops the infix), plus multi-file-per-ROM support. |
| **(c)** | **Shared** card (one file, many games), **fixed** name, or **outside** the save folder | Breaks the per-game model. Not solvable by an extension — see strategy below.                                                 |

Class (a) is pure upside. Class (b) is bounded engineering that stays inside the per-game model. Class (c) is the
genuinely hard one.

## Strategy for class (c)

The emulation ecosystem has already converged on the answer, and it aligns with this project's "no assumptions, the user
decides on ambiguity" stance:

- **No tool merges binary card images.** Block-level merge of a shared `.mcr`/`.ps2`/`.raw` is a confirmed dead end. The
  only options are _isolate_ (per-game) or _pick-one_ (whole-file).
- **Prefer per-game mode.** Modern emulators default to it (DuckStation per-game cards; Beetle PSX / PCSX ReARMed slot
  0; PCSX2 Folder Memory Cards; Dolphin GCI folders). Where the launch core supports per-game cards, that path collapses
  class (c) into class (a)/(b) and maps cleanly onto per-game sync.
- **For an unavoidable shared card, treat it as one device-global blob:** whole-file, last-writer-wins, with conflict
  **detection that warns instead of clobbering** (the Ludusavi model), and rely on the existing version history as the
  recovery net. Never present it as per-game; never merge.
- **Sync SRAM, not save states.** Save states are core- and version-coupled and not portable across devices; in-game
  saves are.
- **The per-game card format is emulator-specific.** A per-game card written by the RetroArch PCSX2 core is not
  byte-compatible with standalone PCSX2. The sync key/format must match the core the user actually launches with — which
  is exactly what RomM's `emulator` subdirectory captures.

## Roadmap mapping

- **(a)** — extension-map additions, candidate for incremental delivery; research/verification tracked in
  [#237](https://github.com/danielcopper/decky-romm-sync/issues/237).
- **(b)** — infix-aware per-game discovery (PS1 multi-card, Flycast per-game VMU); research in
  [#237](https://github.com/danielcopper/decky-romm-sync/issues/237), implementation under the save-format epic
  [#255](https://github.com/danielcopper/decky-romm-sync/issues/255).
- **(c)** — shared/system-dir handling under [#255](https://github.com/danielcopper/decky-romm-sync/issues/255) (save
  formats), [#151](https://github.com/danielcopper/decky-romm-sync/issues/151) (Dreamcast VMU), and
  [#129](https://github.com/danielcopper/decky-romm-sync/issues/129) (standalone emulators) — all v2.0.
