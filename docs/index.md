# Tender { .hero-title }

A [Decky Loader](https://decky.xyz/) plugin that syncs your self-hosted [RomM](https://github.com/rommapp/romm) ROM
library into Steam as Non-Steam shortcuts. Games launch through [RetroDECK](https://retrodeck.net/).

- Browse your entire RomM library directly from Steam's Gaming Mode
- Download ROMs on-demand with cover art, hero banners, logos, and metadata
- Manage BIOS files for systems that require them
- Sync save files between devices through your RomM server

![A RomM game's detail page in Steam showing the injected Tender panel](assets/screenshot-game-detail.jpg)

!!! note "Pre-1.0 (v0.x)"

    The feature set isn't complete yet. Save sync covers standard cartridge saves; the memory-card systems —
    PlayStation, PS2, Dreamcast, GameCube, PSP, 3DS — don't sync their saves yet
    ([support matrix](user-guide/save-sync-support-matrix.md)). The Decky Store listing comes with v1.0; until then
    it's a manual install.

## User Guide

1. **[Getting Started](user-guide/getting-started.md)** — Prerequisites, installation, and first-time setup
2. **[Configuration](user-guide/configuration.md)** — Connection settings, SteamGridDB API key, Steam Input, debug
   options
3. **[Syncing Your Library](user-guide/syncing-your-library.md)** — How sync works, per-platform toggles, collections,
   artwork
4. **[Managing Games](user-guide/managing-games.md)** — Game detail panel, downloading ROMs, uninstalling, refreshing
   metadata
5. **[BIOS Management](user-guide/bios-management.md)** — What BIOS files are, checking status, downloading per-platform
6. **[Save Sync](user-guide/save-sync.md)** — Auto-sync, conflict resolution modes, manual sync, failed sync retries
7. **[Save File Extensions](user-guide/save-file-extensions.md)** — Which save file extensions RetroDECK's cores
   produce, and the record of the original extension decision
8. **[Save Sync Support Matrix](user-guide/save-sync-support-matrix.md)** — What syncs today, what's planned, and what
   doesn't fit the per-game model
9. **[RetroDECK Path Migration](user-guide/retrodeck-path-migration.md)** — Moving your RetroDECK installation between
   storage locations
10. **[Troubleshooting](user-guide/troubleshooting.md)** — Common issues, fixes, Danger Zone explained

## Technical Reference

Developer-oriented documentation for contributors and those interested in the internals.

- **[Steam Non-Steam Shortcuts](architecture/steam-non-steam-shortcuts.md)** — AddShortcut API, VDF format, app ID
  generation
- **[Backend Architecture](architecture/backend-architecture.md)** — Service/adapter architecture, dependency diagram,
  boundary enforcement
- **[Config Source Parsers](architecture/config-source-parsers.md)** — One-parser-per-source principle, source catalog,
  parser layout template for local config/metadata files
- **[Core and Emulator Selection](architecture/core-emulator-selection.md)** — Per-game vs per-system core overrides,
  the single read seam, and how the chosen core is baked into the launch command
- **[Development](contributing/development.md)** — Developer setup, building, testing, dev reload
- **[Save File Sync Architecture](architecture/save-file-sync-architecture.md)** — Three-way conflict detection, session
  tracking, state schema, device registration
- **[Steam Remote Play and Cross-Device Shortcuts](architecture/steam-remote-play.md)** — Remote Play discovery
  protocol, phantom shortcuts, detection APIs
