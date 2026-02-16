# decky-romm-sync

A [Decky Loader](https://decky.xyz/) plugin that syncs your [RomM](https://github.com/rommapp/romm) game library to Steam as non-steam shortcuts. Games appear directly in your Steam library and launch through [RetroDECK](https://retrodeck.net/).

## Features

- **Library sync** — Fetches platforms and ROMs from your RomM server, creates Steam shortcuts with artwork
- **Per-platform control** — Enable/disable which platforms to sync
- **ROM downloads** — Download ROMs on demand with progress tracking and queue management
- **BIOS management** — Download firmware/BIOS files from RomM for systems that need them (PSX, Dreamcast, PS2, etc.)
- **Game detail page** — Shows install status, BIOS status, download/uninstall actions directly on each game's Steam page
- **Controller friendly** — Full gamepad navigation throughout the plugin UI
- **Steam Input config** — Per-shortcut Steam Input mode (Default / Force On / Force Off)
- **RetroArch diagnostics** — Detects misconfigured input drivers that break menu navigation

## Requirements

- [Decky Loader](https://decky.xyz/) installed on your Steam Deck or Linux HTPC
- A running [RomM](https://github.com/rommapp/romm) server with ROM files
- [RetroDECK](https://retrodeck.net/) (for launching games)

## Installation

1. Open Decky Loader in Steam Gaming Mode
2. Go to the Decky Store
3. Search for "RomM Sync" and install

### Manual installation

1. Download the latest release from the [releases page](https://github.com/danielcopper/decky-romm-sync/releases)
2. Extract to `~/homebrew/plugins/decky-romm-sync/`
3. Restart Decky Loader

## Setup

1. Open the plugin from the Quick Access Menu (QAM)
2. Go to **Settings** and enter your RomM server URL and credentials
3. Test the connection
4. Go to **Platforms** and enable the platforms you want to sync
5. Hit **Sync Library** on the main page

## Building from source

```bash
pnpm install
pnpm build
```

## Running tests

```bash
python -m pytest tests/ -q
```

## License

GPL-3.0
