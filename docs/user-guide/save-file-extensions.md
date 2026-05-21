# Save File Extensions

Research results for which save file extensions RetroDECK cores produce, and what our plugin needs to support. This informs the implementation of [#196](https://github.com/danielcopper/decky-romm-sync/issues/196).

## How RetroArch Save Extensions Work

RetroArch has two save mechanisms:

1. **Standard libretro saves** (`libretro_saves = "true"` in core info): RetroArch handles all file I/O. The extension is **hardcoded to `.srm`** (SRAM) and **`.rtc`** (real-time clock). The core exposes memory via `RETRO_MEMORY_SAVE_RAM` / `RETRO_MEMORY_RTC`, and RetroArch writes the files. Every core using this mechanism produces identical extensions.

2. **Core-managed saves** (`libretro_saves = "false"` or absent): The core handles its own file I/O, writing to RetroArch's save directory with custom extensions. RetroArch has no control over these filenames.

This means `.srm`/`.rtc` covers the vast majority of cores. Only a handful of cores use custom extensions.

## RetroDECK Core Inventory

Checked against RetroDECK's installed cores (March 2026). Only cores relevant to save file sync are listed.

### Cores Using Standard `.srm`/`.rtc` (libretro_saves = true)

These cores all produce `.srm` (and optionally `.rtc`). No additional extensions needed.

| Platform | Cores | Notes |
| --- | --- | --- |
| **GB/GBC** | gambatte, sameboy, gearboy, tgbdual, fixgb, DoubleCherryGB | `.rtc` used by Pokemon Crystal/Gold/Silver etc. |
| **GBA** | mgba, vbam, vba_next, skyemu | All GBA save types (EEPROM, SRAM, Flash) packed into single `.srm` |
| **NES** | fceumm, nestopia, mesen | Battery-backed saves |
| **SNES** | snes9x (all variants), bsnes (all variants), supafaust, mednafen_snes, mesen-s | Standard SRAM |
| **N64** | mupen64plus_next, parallel_n64 | **Packed format**: EEPROM + 4 Memory Paks + SRAM + FlashRAM all in one `.srm` (290KB) |
| **Genesis/MD** | genesis_plus_gx, genesis_plus_gx_wide, picodrive, blastem, clownmdemu | Cartridge backup |
| **PSX** | mednafen_psx_hw, mednafen_psx, pcsx_rearmed, swanstation | Memory card 0 as raw 128KB `.srm` |
| **Saturn** | mednafen_saturn, kronos, yabasanshiro, yabause | Standard save |
| **DS** | desmume, desmume2015, melondsds, melonds | See DeSmuME special case below |
| **PC Engine** | mednafen_pce_fast, mednafen_pce, mednafen_supergrafx | Standard save |
| **Lynx** | handy | Standard save |
| **NGP/NGPC** | mednafen_ngp, race | Standard save |
| **Virtual Boy** | mednafen_vb | Standard save |
| **WonderSwan** | mednafen_wswan | Standard save |
| **3DO** | opera | NVRAM |
| **Jaguar** | virtualjaguar | Standard save |
| **Pokemon Mini** | pokemini | Standard save |

### Cores With Special Extension Behavior

#### DeSmuME (DS) -- `.dsv`

- `libretro_saves = "true"`, so **default is `.srm`**
- However, DeSmuME has a dual-mode: its `BackupDevice` class can write `.dsv` (DeSmuME's native format) when configured in Mednafen-compatibility mode
- The core will also fall back to loading `.sav` files if `.dsv` is not found
- **Action**: Add `.dsv` as platform override for DS, so we pick up saves from users who switched modes

#### Genesis Plus GX (Sega CD) -- `.brm`

- `libretro_saves = "true"` for **cartridge SRAM** (standard `.srm`)
- But Sega CD **BRAM** (backup RAM) is managed by the core separately as `.brm` files
- These include: `cart.brm` (cartridge backup RAM) and region-specific system BRAM (`scd_U.brm`, `scd_E.brm`, `scd_J.brm`)
- The per-game BRAM uses the standard rom-name `.brm` pattern
- **Action**: Add `.brm` as platform override for Sega CD

#### Flycast (Dreamcast) -- `.bin`

- No `libretro_saves` field in core info -- uses its own VMU format
- Produces `vmu_save_{A1-D1}.bin` files and `dc_nvmem.bin`
- Multi-slot VMU support (games can produce multiple `.bin` files)
- **Action**: Tracked separately in [#151](https://github.com/danielcopper/decky-romm-sync/issues/151). Not included in extension expansion.

#### MAME / FBNeo (Arcade) -- `.nv`

- `libretro_saves = "false"` for all MAME/FBNeo cores
- MAME manages its own NVRAM system (writes to `SAVEDIR/mame/NVRAM/`)
- Not using RetroArch's standard save path at all
- **Action**: Not relevant for save sync. MAME saves use a completely different directory structure.

#### gpsp (GBA) -- no libretro_saves

- `libretro_saves = "false"` -- manages its own saves
- In practice, gpsp writes `.sav` files in its own format
- However, mgba/vbam are the recommended GBA cores in RetroDECK
- **Action**: Add `.sav` defensively to GBA overrides for gpsp users

#### mednafen_gba (Beetle GBA) -- custom format

- No `libretro_saves` field -- uses Mednafen's native format
- Can write `.{md5}.sav` and `.{md5}.eep` with MD5 hash in filename
- Has a dual mode: Mednafen method (own files) vs libretro method (`.srm`)
- Rarely used in RetroDECK (mgba is the default)
- **Action**: Not worth supporting the MD5-hashed filenames. Users of this core should use libretro mode.

## Comparison With Other RomM Clients

### Grout (Go client)

Uses a **flat global allowlist** of 11 extensions with no per-platform mapping:

```text
.srm, .sav, .dsv, .mcr, .mcd, .brm, .eep, .sra, .fla, .mpk, .nv
```

This is intentionally broad -- Grout targets multiple CFWs (muOS, NextUI, MinUI) where standalone emulators produce these formats. For RetroArch-only setups like RetroDECK, most of these are unnecessary.

### Argosy (Android client)

Uses a **per-emulator** mapping:

| Emulator | Extensions |
| --- | --- |
| RetroArch | `.srm`, `.sav` |
| Mupen64Plus FZ (standalone) | `.sra`, `.eep`, `.fla`, `.mpk` |
| DraStic (standalone DS) | `.dsv`, `.sav` |
| melonDS (standalone) | `.sav` |
| DuckStation (standalone) | `.mcd` |
| MAME | `.nv` |

Again, standalone emulator formats that don't apply to RetroDECK's RetroArch-based setup.

## Implementation Decision

For decky-romm-sync (RetroDECK-only):

| Extension | Include? | Reason |
| --- | --- | --- |
| `.srm` | **Yes** (already supported) | Standard RetroArch SRAM |
| `.rtc` | **Yes** (already supported) | Standard RetroArch RTC |
| `.dsv` | **Yes** (add as DS override) | DeSmuME native format |
| `.brm` | **Yes** (add as Sega CD override) | Genesis Plus GX Sega CD BRAM |
| `.sav` | **Yes** (add to defaults) | gpsp fallback, DeSmuME fallback, defensive |
| `.bin` | **No** (separate ticket #151) | Flycast Dreamcast VMU -- needs special handling |
| `.eep`, `.sra`, `.fla`, `.mpk` | **No** | N64 standalone formats, Mupen64Plus packs into `.srm` |
| `.mcr`, `.mcd` | **No** | PSX Mednafen/DuckStation formats, RetroArch cores use `.srm` |
| `.nv` | **No** | MAME NVRAM, completely different directory structure |

### Platform Override Map

```python
_DEFAULT_EXTENSIONS = (".srm", ".rtc", ".sav")

_PLATFORM_OVERRIDES = {
    "nds": (".srm", ".rtc", ".sav", ".dsv"),
    "segacd": (".srm", ".rtc", ".sav", ".brm"),
}
```

Adding `.sav` to defaults is conservative but safe -- it ensures we pick up saves from any core that uses `.sav` as an alternative SRAM format, without false-matching on non-save files.
