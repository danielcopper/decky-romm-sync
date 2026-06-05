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

The extension list is a small static map keyed by the **RetroDECK system** (the normalized value from `resolve_system` /
`platform_map`, not the raw RomM platform slug — keying by system keeps the lookup aligned with the save directory,
cores, and gamelists, which are all system-keyed): a default of `.srm` / `.rtc` / `.sav`, plus per-system overrides
(`nds` → `.dsv`, `segacd` → `.brm`, `saturn` → `.bkr`/`.bcr`/`.smpc`, `ngp`/`ngpc` → `.flash`/`.ngf`, `pokemini` →
`.eep`, and `amiga`/`amigacd32` → `.nvr`).

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

## Full core reference

Per-core classification from the audit of every RetroArch core RetroDECK can launch. `✅` syncs today · `🟡 (a)`
per-game, extension not listed yet · `🟠 (b)` per-game with a slot/unit infix · `🔴 (c)` shared / out-of-folder · `⚪`
no battery save · `❓` unverified. Non-`.srm` rows are libretro-documented and await on-device confirmation. Snapshot:
2026-06-04.

??? note "All 155 cores, classified"

    | Core | Status | Save file(s) | Naming / dir | Used by (slugs) |
    | --- | --- | --- | --- | --- |
    | `boom3_libretro` | 🔴 (c) | `.save` | system_dir / content | doom |
    | `boom3_xp_libretro` | 🔴 (c) | `.save` | system_dir / content | doom |
    | `cannonball_libretro` | 🔴 (c) | `.xml` | shared_fixed_name / system | ports |
    | `cap32_libretro` | 🔴 (c) | `.sna` | single_token_nonstandard / content | amstradcpc, gx4000 |
    | `citra2018_libretro` | 🔴 (c) | — | system_dir / system | n3ds |
    | `citra_libretro` | 🔴 (c) | — | system_dir / system | n3ds |
    | `dolphin_libretro` | 🔴 (c) | `.gci`, `.gcs` | system_dir / saves | gc, wii |
    | `dosbox_svn_libretro` | 🔴 (c) | — | system_dir / saves | dos, pc |
    | `easyrpg_libretro` | 🔴 (c) | `.lsd`, `.lyn`, `.dyn`, `.lgs` | single_token_nonstandard / content | easyrpg |
    | `flycast_libretro` | 🔴 (c) | `.bin` | shared_fixed_name / system | arcade, atomiswave, consolearcade, dreamcast, mame +3 |
    | `mame2000_libretro` | 🔴 (c) | `.nv` | shared_fixed_name / system | arcade, cps, cps1, cps2, cps3 +1 |
    | `mame2003_libretro` | 🔴 (c) | `.nv` | shared_fixed_name / saves | arcade, cps, cps1, cps2, cps3 +1 |
    | `mame2003_plus_libretro` | 🔴 (c) | — | single_token_nonstandard / saves | arcade, cps, cps1, cps2, cps3 +1 |
    | `mame2010_libretro` | 🔴 (c) | `.nv` | system_dir / saves | arcade, cps, cps1, cps2, cps3 +1 |
    | `mame_libretro` | 🔴 (c) | `.nv` | system_dir / saves | apple2, apple2gs, arcade, arcadia, astrocde +29 |
    | `mess2015_libretro` | 🔴 (c) | — | system_dir / system | mess |
    | `neocd_libretro` | 🔴 (c) | `.srm` | shared_fixed_name / saves | neogeocd, neogeocdjp |
    | `openlara_libretro` | 🔴 (c) | `.dat` | shared_fixed_name / saves | ports |
    | `pcsx2_libretro` | 🔴 (c) | `.ps2` | shared_fixed_name / system | ps2 |
    | `ppsspp_libretro` | 🔴 (c) | — | shared_fixed_name / saves | psp |
    | `prboom_libretro` | 🔴 (c) | `.dsg` | shared_fixed_name / saves | doom |
    | `px68k_libretro` | 🔴 (c) | — | system_dir / system | x68000 |
    | `scummvm_libretro` | 🔴 (c) | — | system_dir / system | scummvm |
    | `vitaquake2-rogue_libretro` | 🔴 (c) | — | shared_fixed_name / content | quake |
    | `vitaquake2-xatrix_libretro` | 🔴 (c) | — | shared_fixed_name / content | quake |
    | `vitaquake2-zaero_libretro` | 🔴 (c) | — | shared_fixed_name / content | quake |
    | `vitaquake2_libretro` | 🔴 (c) | — | shared_fixed_name / content | quake |
    | `mednafen_psx_hw_libretro` | 🟠 (b) | `.srm`, `.mcr` | infix / saves | psx |
    | `mednafen_psx_libretro` | 🟠 (b) | `.srm`, `.mcr` | infix / saves | psx |
    | `pcsx_rearmed_libretro` | 🟠 (b) | `.srm`, `.mcd` | infix / saves | psx |
    | `swanstation_libretro` | 🟠 (b) | `.mcd`, `.mcr` | infix / saves | psx |
    | `tyrquake_libretro` | 🟠 (b) | `.sav` | infix / saves | quake |
    | `virtualjaguar_libretro` | 🟠 (b) | `.srm`, `.cdrom.srm` | infix / saves | atarijaguar |
    | `dosbox_pure_libretro` | 🟡 (a) | `.pure.zip`, `.save.zip` | single_token_nonstandard / saves | dos, pc, windows3x, windows9x |
    | `kronos_libretro` | 🟡 (a) | `.ram` | single_token_nonstandard / saves | arcade, consolearcade, mame, saturn, saturnjp +1 |
    | `nxengine_libretro` | 🟡 (a) | `.dat` | single_token_nonstandard / saves | ports |
    | `retro8_libretro` | 🟡 (a) | `.p8d.txt` | single_token_nonstandard / saves | pico8 |
    | `ardens_libretro` | ❓ | — | unknown / unknown | arduboy |
    | `panda3ds_libretro` | ❓ | — | unknown / unknown | n3ds |
    | `same_cdi_libretro` | ❓ | — | unknown / unknown | cdimono1 |
    | `wasm4_libretro` | ❓ | — | unknown / unknown | wasm4 |
    | `DoubleCherryGB_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | gb, gbc |
    | `blastem_libretro` | ✅ | `.srm` | single_token_default / saves | genesis, megadrive, megadrivejp |
    | `bsnes-jg_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | satellaview, sfc, snes, snesna, sufami |
    | `bsnes_hd_beta_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | satellaview, sfc, snes, snesna, sufami |
    | `bsnes_libretro` | ✅ | `.srm` | single_token_default / saves | gb, gbc, satellaview, sfc, snes +2 |
    | `bsnes_mercury_accuracy_libretro` | ✅ | `.srm` | single_token_default / saves | satellaview, sfc, snes, snesna, sufami |
    | `desmume2015_libretro` | ✅ | `.dsv` | single_token_default / saves | nds |
    | `desmume_libretro` | ✅ | `.dsv` | single_token_default / saves | nds |
    | `fbneo_libretro` | ✅ | `.nv`, `.fs` | single_token_default / saves | arcade, cps, cps1, cps2, cps3 +5 |
    | `fceumm_libretro` | ✅ | `.srm` | single_token_default / saves | famicom, fds, nes |
    | `gambatte_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | gb, gbc |
    | `gearboy_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | gb, gbc |
    | `gearcoleco_libretro` | ✅ | `.srm` | single_token_default / saves | colecovision |
    | `geargrafx_libretro` | ✅ | `.srm` | single_token_default / saves | supergrafx |
    | `gearsystem_libretro` | ✅ | `.srm` | single_token_default / saves | gamegear, mark3, mastersystem, multivision, sg-1000 |
    | `genesis-plus-gx-expanded-rom-size-paprium_libretro` | ✅ | `.srm` | single_token_default / saves | megadrive, megadrivejp |
    | `genesis_plus_gx_libretro` | ✅ | `.srm`, `.brm` | single_token_default / saves | gamegear, genesis, mark3, mastersystem, megacd +5 |
    | `genesis_plus_gx_wide_libretro` | ✅ | `.srm`, `.brm` | single_token_default / saves | gamegear, genesis, mark3, mastersystem, megacd +5 |
    | `geolith_libretro` | ✅ | `.srm`, `.nv`, `.mcr` | single_token_default / saves | arcade, mame, neogeo |
    | `gpsp_libretro` | ✅ | `.srm` | single_token_default / saves | gba |
    | `mednafen_ngp_libretro` | ✅ | `.flash` | single_token_default / saves | ngp, ngpc |
    | `mednafen_pce_fast_libretro` | ✅ | `.srm` | single_token_default / saves | pcengine, pcenginecd, tg16, tg-cd |
    | `mednafen_pce_libretro` | ✅ | `.srm` | single_token_default / saves | pcengine, pcenginecd, supergrafx, tg16, tg-cd |
    | `mednafen_pcfx_libretro` | ✅ | `.srm` | single_token_default / saves | pcfx |
    | `mednafen_saturn_libretro` | ✅ | `.bkr`, `.bcr`, `.smpc` | single_token_default / saves | saturn, saturnjp |
    | `mednafen_supafaust_libretro` | ✅ | `.srm` | single_token_default / saves | sfc, snes, snesna |
    | `mednafen_supergrafx_libretro` | ✅ | `.srm` | single_token_default / saves | supergrafx, tg16 |
    | `mednafen_vb_libretro` | ✅ | `.srm` | single_token_default / saves | virtualboy |
    | `mednafen_wswan_libretro` | ✅ | `.srm` | single_token_default / saves | wonderswan, wonderswancolor |
    | `melonds_libretro` | ✅ | `.sav` | single_token_default / content | nds |
    | `melondsds_libretro` | ✅ | `.srm` | single_token_default / saves | nds |
    | `mesen-s_libretro` | ✅ | `.srm` | single_token_default / saves | gb, gbc, satellaview, sfc, sgb +2 |
    | `mesen_libretro` | ✅ | `.srm` | single_token_default / saves | famicom, fds, nes |
    | `mgba_libretro` | ✅ | `.srm` | single_token_default / saves | gb, gba, gbc, sgb |
    | `mupen64plus_next_libretro` | ✅ | `.srm` | single_token_default / saves | n64, n64dd |
    | `nestopia_libretro` | ✅ | `.srm` | single_token_default / saves | famicom, fds, nes |
    | `noods_libretro` | ✅ | `.srm` | single_token_default / saves | gba |
    | `opera_libretro` | ✅ | `.srm` | single_token_default / saves | 3do |
    | `parallel_n64_libretro` | ✅ | `.srm` | single_token_default / saves | n64, n64dd |
    | `picodrive_libretro` | ✅ | `.srm` | single_token_default / saves | gamegear, genesis, mark3, mastersystem, megacd +7 |
    | `pokemini_libretro` | ✅ | `.eep` | single_token_default / saves | pokemini |
    | `potator_libretro` | ✅ | `.srm` | single_token_default / saves | supervision |
    | `puae2021_libretro` | ✅ | `.nvr` | single_token_default / saves | amiga, amiga1200, amiga600, amigacd32, cdtv |
    | `puae_libretro` | ✅ | `.nvr` | single_token_default / saves | amiga, amiga1200, amiga600, amigacd32, cdtv |
    | `quasi88_libretro` | ✅ | `.srm` | single_token_default / saves | pc88 |
    | `quicknes_libretro` | ✅ | `.srm` | single_token_default / saves | famicom, nes |
    | `race_libretro` | ✅ | `.ngf` | single_token_default / saves | ngp, ngpc |
    | `sameboy_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | gb, gbc, sgb |
    | `sameduck_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | megaduck |
    | `smsplus_libretro` | ✅ | `.srm` | single_token_default / saves | gamegear, mark3, mastersystem |
    | `snes9x2005_plus_libretro` | ✅ | `.srm` | single_token_default / saves | satellaview, sfc, snes, snesna, sufami |
    | `snes9x2010_libretro` | ✅ | `.srm` | single_token_default / saves | satellaview, sfc, snes, snesna, sufami |
    | `snes9x_libretro` | ✅ | `.srm` | single_token_default / saves | satellaview, sfc, snes, snesna, sufami |
    | `stella2014_libretro` | ✅ | `.srm` | single_token_default / saves | atari2600 |
    | `stella2023_libretro` | ✅ | `.srm` | single_token_default / saves | atari2600 |
    | `stella_libretro` | ✅ | `.srm` | single_token_default / saves | atari2600 |
    | `tgbdual_libretro` | ✅ | `.srm`, `.rtc` | single_token_default / saves | gb, gbc |
    | `tic80_libretro` | ✅ | `.srm` | single_token_default / saves | tic80 |
    | `vba_next_libretro` | ✅ | `.srm` | single_token_default / saves | gba |
    | `vbam_libretro` | ✅ | `.srm` | single_token_default / saves | gb, gba, gbc |
    | `vice_x128_libretro` | ✅ | `.nvr`, `.d64`, `.d71`, `.d81` | single_token_default / saves | c64 |
    | `vice_x64_libretro` | ✅ | `.nvr`, `.d64`, `.d71`, `.d81` | single_token_default / saves | c64 |
    | `vice_x64sc_libretro` | ✅ | `.nvr`, `.d64`, `.d71`, `.d81` | single_token_default / saves | c64 |
    | `vice_xplus4_libretro` | ✅ | `.nvr`, `.d64`, `.d71`, `.d81` | single_token_default / saves | plus4 |
    | `vice_xscpu64_libretro` | ✅ | `.nvr`, `.d64`, `.d71`, `.d81` | single_token_default / saves | c64 |
    | `vice_xvic_libretro` | ✅ | `.nvr`, `.d64`, `.d71`, `.d81` | single_token_default / saves | vic20 |
    | `yabasanshiro_libretro` | ✅ | `.bkr`, `.bcr`, `.smpc` | single_token_default / saves | saturn, saturnjp |
    | `yabause_libretro` | ✅ | `.srm` | single_token_default / saves | saturn, saturnjp |
    | `81_libretro` | ⚪ | — | none / none | zx81 |
    | `a5200_libretro` | ⚪ | — | none / none | atari5200 |
    | `arduous_libretro` | ⚪ | — | none / none | arduboy |
    | `atari800_libretro` | ⚪ | — | none / none | atari5200, atari800, atarixe |
    | `b2_libretro` | ⚪ | — | none / none | bbcmicro |
    | `bluemsx_libretro` | ⚪ | — | none / none | colecovision, msx, msx1, msx2, msxturbor +2 |
    | `cdi2015_libretro` | ⚪ | — | none / none | cdimono1 |
    | `chailove_libretro` | ⚪ | — | none / none | chailove |
    | `crocods_libretro` | ⚪ | — | none / none | amstradcpc, gx4000 |
    | `dice_libretro` | ⚪ | — | none / none | arcade, mame |
    | `dirksimple_libretro` | ⚪ | — | none / none | daphne, laserdisc |
    | `dosbox_core_libretro` | ⚪ | — | none / none | dos, pc |
    | `ecwolf_libretro` | ⚪ | — | none / none | ports |
    | `fbalpha2012_cps1_libretro` | ⚪ | — | none / none | cps, cps1, fba |
    | `fbalpha2012_cps2_libretro` | ⚪ | — | none / none | cps, cps2, fba |
    | `fbalpha2012_cps3_libretro` | ⚪ | — | none / none | cps, cps3, fba |
    | `fbalpha2012_libretro` | ⚪ | — | none / none | arcade, cps, cps1, cps2, cps3 +2 |
    | `fbalpha2012_neogeo_libretro` | ⚪ | — | none / none | fba |
    | `fmsx_libretro` | ⚪ | — | none / none | msx, msx1, msx2 |
    | `freechaf_libretro` | ⚪ | — | none / none | channelf |
    | `freeintv_libretro` | ⚪ | — | none / none | intellivision |
    | `frodo_libretro` | ⚪ | — | none / none | c64 |
    | `fuse_libretro` | ⚪ | — | none / none | zxspectrum |
    | `gw_libretro` | ⚪ | — | none / none | gameandwatch, lcdgames |
    | `handy_libretro` | ⚪ | — | none / none | atarilynx |
    | `hatari_libretro` | ⚪ | — | none / none | atarist |
    | `holani_libretro` | ⚪ | — | none / none | atarilynx |
    | `lowresnx_libretro` | ⚪ | — | none / none | lowresnx |
    | `lutro_libretro` | ⚪ | — | none / none | lutro |
    | `mednafen_lynx_libretro` | ⚪ | — | none / none | atarilynx |
    | `mojozork_libretro` | ⚪ | — | none / none | zmachine |
    | `mrboom_libretro` | ⚪ | — | none / none | ports |
    | `mu_libretro` | ⚪ | — | none / none | palm |
    | `nekop2_libretro` | ⚪ | — | none / none | pc98 |
    | `np2kai_libretro` | ⚪ | — | none / none | pc98 |
    | `o2em_libretro` | ⚪ | — | none / none | odyssey2, videopac |
    | `prosystem_libretro` | ⚪ | — | none / none | atari7800 |
    | `squirreljme_libretro` | ⚪ | — | none / none | j2me |
    | `superbroswar_libretro` | ⚪ | — | none / none | ports |
    | `theodore_libretro` | ⚪ | — | none / none | moto, to8 |
    | `uzem_libretro` | ⚪ | — | none / none | uzebox |
    | `vecx_libretro` | ⚪ | — | none / none | vectrex |
    | `vircon32_libretro` | ⚪ | — | none / none | vircon32 |
    | `virtualxt_libretro` | ⚪ | — | none / none | dos, pc |
    | `vitaquake3_libretro` | ⚪ | — | none / none | quake |
    | `x1_libretro` | ⚪ | — | none / none | x1 |
