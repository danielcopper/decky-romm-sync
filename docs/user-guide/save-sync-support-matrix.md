# Save sync support by platform

Save sync works **per game**: when a game is installed, its save is uploaded to RomM and pulled back before launch, so
you can carry on across devices. Standard cartridge saves work automatically. A few consoles store saves differently —
this page shows what syncs for each system today, and what's planned.

## Categories

|    | Meaning                                                                                                                                                                                                                                           |
| -- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ✅ | **Synced today.** Your saves for this system carry across devices automatically.                                                                                                                                                                  |
| 🔜 | **Planned.** This save type isn't synced yet, but it fits the model and is on the way in a future release.                                                                                                                                        |
| ❌ | **Not synced yet.** This system uses a _shared_ memory card (one card for many games) or stores saves outside the per-game save folder, so it doesn't fit per-game sync today — **planned for a later release**, handled differently (see below). |
| ⚪ | **No save data.** This system's emulator has no in-game save to sync (you can still use save states locally).                                                                                                                                     |

## What syncs today ✅

Standard per-game cartridge saves sync automatically. That covers the large majority of systems — Nintendo (NES, SNES,
Game Boy / Color / Advance, N64, DS), Sega (Master System, Game Gear, Genesis / Mega Drive, Sega CD), PC Engine /
TurboGrafx, WonderSwan, Atari Lynx, Virtual Boy, and more.

## Coming soon 🔜

Per-game saves for these systems fit the sync model and are planned for a future release:

| System                                  | Notes |
| --------------------------------------- | ----- |
| Sega Saturn — per-game backup-RAM saves |       |
| PlayStation — memory-card saves         |       |
| Neo Geo Pocket / Color                  |       |
| Pokémon Mini                            |       |
| Commodore Amiga                         |       |

A few less-common systems (some DOS, PICO-8, ST-V) may also gain support pending confirmation.

## Not synced yet ❌

These consoles use a **shared memory card** — a single card file holds the saves for _all_ your games — or keep saves
outside the per-game save folder. A shared card can't be split per game without risking other games' saves, so it
doesn't fit per-game sync today. We're looking at safe ways to handle these in a future release.

| System                                                     |
| ---------------------------------------------------------- |
| Dreamcast — shared VMU card                                |
| PlayStation 2 — shared memory card                         |
| GameCube — shared memory card                              |
| Neo Geo CD — shared save                                   |
| Nintendo 3DS — saves kept in a separate location           |
| PSP — saves kept in per-game folders, not single files     |
| Arcade (MAME) — NVRAM is stored separately by the emulator |

## No save data ⚪

Many computer, arcade, and homebrew systems have no in-game battery save at all — there's simply nothing to sync (save
states still work locally). See the full table for specifics.

## Full platform list

??? note "Every platform (149)"

    Status of each platform's default emulator core. Some platforms offer alternative cores that may behave differently.

    | Platform | Status | Notes |
    | --- | --- | --- |
    | `amstradcpc` | ❌ | Not synced |
    | `apple2` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `apple2gs` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `arcade` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `arcadia` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `astrocde` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `atomiswave` | ❌ | Not synced |
    | `consolearcade` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `cps` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `cps1` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `cps2` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `cps3` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `crvision` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `daphne` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `doom` | ❌ | Not synced |
    | `dreamcast` | ❌ | Not synced |
    | `easyrpg` | ❌ | Not synced |
    | `fmtowns` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `gamate` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `gameandwatch` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `gamecom` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `gc` | ❌ | Not synced |
    | `gmaster` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `gx4000` | ❌ | Not synced |
    | `laserdisc` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `lcdgames` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `mame` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `mess` | ❌ | Not synced |
    | `model2` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `n3ds` | ❌ | Not synced |
    | `naomi` | ❌ | Not synced |
    | `naomi2` | ❌ | Not synced |
    | `naomigd` | ❌ | Not synced |
    | `neogeocd` | ❌ | Not synced |
    | `neogeocdjp` | ❌ | Not synced |
    | `ps2` | ❌ | Not synced |
    | `psp` | ❌ | Not synced |
    | `pv1000` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `scummvm` | ❌ | Not synced |
    | `scv` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `supracan` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `vsmile` | ❌ | Saves are stored separately by the emulator (MAME) |
    | `wii` | ❌ | Not synced |
    | `x68000` | ❌ | Not synced |
    | `amiga` | 🔜 | Planned |
    | `amiga1200` | 🔜 | Planned |
    | `amiga600` | 🔜 | Planned |
    | `amigacd32` | 🔜 | Planned |
    | `atarijaguar` | 🔜 | Planned |
    | `cdimono1` | 🔜 | Under review |
    | `cdtv` | 🔜 | Planned |
    | `dos` | 🔜 | Planned |
    | `ngp` | 🔜 | Planned |
    | `ngpc` | 🔜 | Planned |
    | `pc` | 🔜 | Planned |
    | `pico8` | 🔜 | Planned |
    | `pokemini` | 🔜 | Planned |
    | `psx` | 🔜 | Planned |
    | `quake` | 🔜 | Planned |
    | `saturn` | 🔜 | Planned |
    | `saturnjp` | 🔜 | Planned |
    | `stv` | 🔜 | Planned |
    | `wasm4` | 🔜 | Under review |
    | `windows3x` | 🔜 | Planned |
    | `windows9x` | 🔜 | Planned |
    | `3do` | ✅ | Synced |
    | `atari2600` | ✅ | Synced |
    | `c64` | ✅ | Synced |
    | `famicom` | ✅ | Synced |
    | `fbneo` | ✅ | Synced |
    | `fds` | ✅ | Synced |
    | `gamegear` | ✅ | Synced |
    | `gb` | ✅ | Synced |
    | `gba` | ✅ | Synced |
    | `gbc` | ✅ | Synced |
    | `genesis` | ✅ | Synced |
    | `mark3` | ✅ | Synced |
    | `mastersystem` | ✅ | Synced |
    | `megacd` | ✅ | Synced |
    | `megacdjp` | ✅ | Synced |
    | `megadrive` | ✅ | Synced |
    | `megadrivejp` | ✅ | Synced |
    | `megaduck` | ✅ | Synced |
    | `multivision` | ✅ | Synced |
    | `n64` | ✅ | Synced |
    | `n64dd` | ✅ | Synced |
    | `nds` | ✅ | Synced |
    | `neogeo` | ✅ | Synced |
    | `nes` | ✅ | Synced |
    | `pc88` | ✅ | Synced |
    | `pcengine` | ✅ | Synced |
    | `pcenginecd` | ✅ | Synced |
    | `pcfx` | ✅ | Synced |
    | `plus4` | ✅ | Synced |
    | `satellaview` | ✅ | Synced |
    | `sega32x` | ✅ | Synced |
    | `sega32xjp` | ✅ | Synced |
    | `sega32xna` | ✅ | Synced |
    | `segacd` | ✅ | Synced |
    | `sfc` | ✅ | Synced |
    | `sg-1000` | ✅ | Synced |
    | `sgb` | ✅ | Synced |
    | `snes` | ✅ | Synced |
    | `snesna` | ✅ | Synced |
    | `sufami` | ✅ | Synced |
    | `supergrafx` | ✅ | Synced |
    | `supervision` | ✅ | Synced |
    | `tg-cd` | ✅ | Synced |
    | `tg16` | ✅ | Synced |
    | `tic80` | ✅ | Synced |
    | `vic20` | ✅ | Synced |
    | `virtualboy` | ✅ | Synced |
    | `wonderswan` | ✅ | Synced |
    | `wonderswancolor` | ✅ | Synced |
    | `arduboy` | ⚪ | No save data |
    | `atari5200` | ⚪ | No save data |
    | `atari7800` | ⚪ | No save data |
    | `atari800` | ⚪ | No save data |
    | `atarilynx` | ⚪ | No save data |
    | `atarist` | ⚪ | No save data |
    | `atarixe` | ⚪ | No save data |
    | `bbcmicro` | ⚪ | No save data |
    | `chailove` | ⚪ | No save data |
    | `channelf` | ⚪ | No save data |
    | `colecovision` | ⚪ | No save data |
    | `fba` | ⚪ | No save data |
    | `intellivision` | ⚪ | No save data |
    | `j2me` | ⚪ | No save data |
    | `lowresnx` | ⚪ | No save data |
    | `lutro` | ⚪ | No save data |
    | `moto` | ⚪ | No save data |
    | `msx` | ⚪ | No save data |
    | `msx1` | ⚪ | No save data |
    | `msx2` | ⚪ | No save data |
    | `msxturbor` | ⚪ | No save data |
    | `odyssey2` | ⚪ | No save data |
    | `palm` | ⚪ | No save data |
    | `pc98` | ⚪ | No save data |
    | `ports` | ⚪ | No save data |
    | `spectravideo` | ⚪ | No save data |
    | `to8` | ⚪ | No save data |
    | `uzebox` | ⚪ | No save data |
    | `vectrex` | ⚪ | No save data |
    | `videopac` | ⚪ | No save data |
    | `vircon32` | ⚪ | No save data |
    | `x1` | ⚪ | No save data |
    | `zmachine` | ⚪ | No save data |
    | `zx81` | ⚪ | No save data |
    | `zxspectrum` | ⚪ | No save data |

---

_Coverage is reviewed against the emulator cores RetroDECK ships. Some 🔜 entries are awaiting on-device confirmation.
Last reviewed 2026-06-04._
