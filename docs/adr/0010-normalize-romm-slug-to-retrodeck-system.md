# Platform identity is normalized to a RetroDECK `system` at the RomM seam; local resolution keys by `system`, never by the raw RomM slug

## Status

Accepted. Implemented incrementally: the save-extension keying lands with this ADR
([#899](https://github.com/danielcopper/decky-romm-sync/issues/899)); the core / firmware / game-detail normalization is
a follow-up (the slug→system leak issue). The full multi-vocabulary reference table under `docs/architecture/` is
completed after the naming research (ES-DE / RetroArch / standalone-emulator naming for
[#129](https://github.com/danielcopper/decky-romm-sync/issues/129)).

## Context

The plugin ingests games from RomM and then resolves, **locally**, where each game's saves live, which file extensions
to probe for them, which emulator core launches the game, and which BIOS it needs. Every one of those is a decision
about the **local RetroDECK / RetroArch environment**, not about RomM.

A platform is named by **different vocabularies at different layers**, and they do not always agree:

| Vocabulary                       | Example (Dreamcast / NGP)         | Source                                                       | Used for                                          |
| -------------------------------- | --------------------------------- | ------------------------------------------------------------ | ------------------------------------------------- |
| RomM `slug`                      | `dc` / `neo-geo-pocket`           | RomM API (canonical)                                         | **input only** — must be normalized               |
| RomM `fs_slug`                   | `dc`                              | RomM library scanner (igir `{romm}` token produces it)       | RomM's on-disk folder name                        |
| RomM `igdb_slug`                 | `dreamcast`                       | RomM metadata                                                | BIOS (`known_bios_files.json`)                    |
| RetroDECK / ES-DE `system`       | `dreamcast` / `ngp`               | `platform_map` value, `es_systems.xml`, `core_defaults.json` | **all local decisions** (saves, cores, gamelists) |
| RetroArch `corename` / `core_so` | `Flycast` / `flycast_libretro.so` | core `.info` / `.so`                                         | active core, sort-by-core save subdirs            |
| BIOS-folder slug                 | `pcsx2/bios` → `ps2`              | RetroArch cores                                              | BIOS placement (its own space)                    |

RomM holds `slug`, `fs_slug` and `igdb_slug` as **independent** fields that may diverge, and RomM **changed its slug
scheme over versions** (PR #1674 / migration 0046 standardized short "Universal Platform Slugs" — `dreamcast → dc`,
`playstation → psx` — while other platforms keep long IGDB-style slugs such as `neo-geo-pocket`, `pokemon-mini`,
`commodore-cdtv`) yet still accepts the old folder names for its scanner. So **`slug == system` cannot be assumed**.

The plugin already normalizes RomM `slug` / `fs_slug` → RetroDECK `system` in exactly one place: `resolve_system`
(`adapters/romm/http.py`) backed by `platform_map` (`defaults/config.json`), at download time. The resolved `system` is
stored on the install (`rom_installs.system`) alongside the raw `platform_slug`.

The bug that surfaced this (during #899): `get_save_extensions` was keyed by the **raw RomM `platform_slug`**, while the
save **directory** in the very same function (`resolve_save_dir`) was keyed by the normalized `system`. Because
`slug != system` for many platforms (`dc != dreamcast`), the override silently missed and core-specific saves (`.bkr`,
`.dsv`, …) were never probed → silent save loss. The **same raw-slug leak** exists in core / firmware / game-detail
resolution: the system-keyed seam in `es_de_config.py` (parameters literally named `system_name`) is fed the raw slug,
so e.g. a core override is written to `ES-DE/gamelists/dc/` while RetroDECK's launcher reads
`ES-DE/gamelists/dreamcast/` — the core choice is silently lost.

## Decision

1. **RomM's platform identity (`slug` / `fs_slug`) is input only.** It is normalized to a RetroDECK `system` **exactly
   once**, at the RomM seam (`resolve_system` / `platform_map`); the resolved `system` is the identity carried
   downstream and stored on the install.
2. **Every local decision keys by the normalized `system`** — save directory, save extension, core selection, gamelist /
   `<altemulator>` writes — **never** by the raw RomM slug. Where a value is genuinely **core-specific**, it keys by the
   **active core on top of `system`** (see §5 of the save-sync-coverage doc), not by the platform alone.
3. **The save-extension override map (`_PLATFORM_OVERRIDES`) is keyed by `system`.** Keys are RetroDECK system names,
   not RomM slugs. (Implemented in #899.)
4. **BIOS / firmware resolution is a separate vocabulary** (the RomM / BIOS-folder slug space — `known_bios_files.json`
   IGDB slugs, folder overrides like `pcsx2/bios → ps2`) and is **deliberately NOT** normalized to `system`. Those
   lookups are correct in their own slug space; normalizing them to `system` would break BIOS resolution. When a service
   does both a core lookup (system space) and a BIOS lookup (BIOS space) on the same slug, only the core lookup is
   normalized.
5. **`resolve_system` keeps its verbatim fallback** (unknown slug → returned as-is); it is **not** changed to
   normalize-or-fail. An unmapped / new platform must still launch with default behavior. Robustness is therefore
   bounded by `platform_map` coverage, which is maintained as a standing concern (map-coverage audit follow-up).

## Consequences

- Save-extension lookup is now consistent with the save directory and **robust to RomM slug variants**: short and long
  forms collapse to one `system` through `platform_map` (`ngp ← {ngp, neo-geo-pocket}`,
  `saturn ← {saturn, sega-saturn}`, …). Lands in #899.
- Override keys that no RomM slug can ever resolve to (`saturnjp`, `amiga1200`, `amiga600`, `cdtv`) are **unreachable**
  under system-keying and were dropped: a JP-Saturn / Amiga-600/1200 ROM resolves to `saturn` / `amiga` and is covered
  by those keys; `cdtv` needs a `platform_map` entry first (`commodore-cdtv → cdtv`) before its `.nvr` override can
  return — deferred to the map-coverage audit.
- The **same decision must be applied to core / firmware / game-detail resolution** (the live slug→system leak). Done as
  a separate follow-up: inject a `SystemResolver`, normalize the raw slug before the system-keyed seams
  (`get_active_core` / `get_available_cores` / `set_*_override`), and **leave the BIOS-folder lookups untouched**.
  Uninstalled ROMs (game-detail) have no `installed.system`, so those services call
  `resolve_system(rom.platform_slug, …)` themselves.
- A `system` value not present in `platform_map` (e.g. `commodore-cdtv` today) degrades to the same safe default as
  before (no regression) but cannot reach a system-keyed override until mapped.
- The vocabulary boundaries above become the **foundation for multi-emulator support** (#129): each standalone emulator
  carries its own save-path / naming convention, layered on top of `system` + active core. The full reference table
  (including the ES-DE / RetroArch / standalone specifics still being researched) lives under `docs/architecture/`.

## Alternatives considered

- **Keep keying by the raw RomM slug; add long-form aliases to the override map.** Rejected: incomplete (which aliases,
  per platform, unbounded as RomM evolves) and it duplicates the normalization `platform_map` already performs.
  System-keying reuses the single existing seam.
- **Normalize everything, including BIOS lookups, to `system`.** Rejected: BIOS resolution is correct in the RomM /
  BIOS-folder slug space; normalizing it to `system` would break it. The vocabularies are genuinely distinct and must
  not be conflated — a blanket `platform_slug → system` rename in `firmware.py` is a bug, not a fix.
- **Make `resolve_system` fail on an unknown slug (normalize-or-die).** Rejected: an unmapped / new platform must still
  launch with default behavior; failing would break it. Verbatim fallback plus active map-coverage maintenance is the
  safer posture.

See also: [ADR-0003](0003-json-sqlite-persistence-boundary.md) (persistence boundary),
[ADR-0008](0008-rom-install-launch-file-and-rom-dir.md) (`RomInstall` fields incl. `system`),
[#129](https://github.com/danielcopper/decky-romm-sync/issues/129) (multi-emulator seam).
