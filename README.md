![decky-romm-sync](assets/banner.png)

[![CI](https://github.com/danielcopper/decky-romm-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/danielcopper/decky-romm-sync/actions/workflows/ci.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=danielcopper_decky-romm-sync&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=danielcopper_decky-romm-sync)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=danielcopper_decky-romm-sync&metric=coverage)](https://sonarcloud.io/summary/new_code?id=danielcopper_decky-romm-sync)
[![Maintainability](https://sonarcloud.io/api/project_badges/measure?project=danielcopper_decky-romm-sync&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=danielcopper_decky-romm-sync)
[![Reliability](https://sonarcloud.io/api/project_badges/measure?project=danielcopper_decky-romm-sync&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=danielcopper_decky-romm-sync)
[![Security](https://sonarcloud.io/api/project_badges/measure?project=danielcopper_decky-romm-sync&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=danielcopper_decky-romm-sync)
[![License](https://img.shields.io/github/license/danielcopper/decky-romm-sync)](LICENSE)

# decky-romm-sync

A [Decky Loader](https://decky.xyz/) plugin that syncs your [RomM](https://github.com/rommapp/romm) game library to
Steam as non-steam shortcuts. Games appear directly in your Steam library and launch through [RetroDECK](https://retrodeck.net/).

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

Both methods below require **Developer Mode** in Decky Loader:

1. Open the Quick Access Menu (QAM) and select **Decky**
2. Click the **gear icon** (top right) to open Decky settings
3. Toggle on **Developer Mode** — a **Developer** tab will appear

### Install from ZIP

1. Download the latest `decky-romm-sync.zip` from the [releases page](https://github.com/danielcopper/decky-romm-sync/releases)
   to your Steam Deck
2. In Decky settings, go to the **Developer** tab
3. Select **Install Plugin from ZIP**
4. Choose the downloaded file and confirm

### Install from URL

1. In Decky settings, go to the **Developer** tab
2. Select **Install Plugin from URL**
3. Paste the direct link to the latest release zip (e.g. `https://github.com/danielcopper/decky-romm-sync/releases/latest/download/decky-romm-sync.zip`)
4. Confirm installation

### From source

Requires [mise](https://mise.jdx.dev/) (or Node LTS + pnpm manually).

```bash
git clone https://github.com/danielcopper/decky-romm-sync.git
cd decky-romm-sync
mise install          # installs Node, pnpm, Python
pnpm install
pnpm build
```

Deploy to your device:

```bash
# Symlink for development (rebuilds take effect immediately after Decky restart)
sudo ln -sf "$(pwd)" ~/homebrew/plugins/decky-romm-sync
sudo systemctl restart plugin_loader
```

## Setup

1. Open the Quick Access Menu (QAM) and select **RomM Sync**
2. Go to **Settings** and enter your RomM server URL and credentials
3. Hit **Test Connection** to verify
4. Go to **Platforms** and enable the platforms you want to sync
5. Go back and hit **Sync Library**
6. Your ROMs will appear as non-steam shortcuts in your Steam library

## Running tests

```bash
python -m pytest tests/ -q
```

## Acknowledgments

This plugin stands on the shoulders of some great projects:

- [RomM](https://github.com/rommapp/romm) — the self-hosted ROM manager at the heart of this plugin. RomM provides
  the library, metadata, cover art, and save file storage that makes the entire sync experience possible
- [RetroDECK](https://retrodeck.net/) — the all-in-one emulation solution for Steam Deck that bundles ES-DE,
  RetroArch, and standalone emulators into a single flatpak. Our entire launch chain runs through RetroDECK
- [Decky Loader](https://decky.xyz/) — the plugin framework that makes all of this possible
- [Valve](https://www.valvesoftware.com/) — for the Steam Deck, SteamOS, and an open enough platform to build on
- [Unifideck](https://github.com/ma3ke/unifideck) — inspiration for game detail page injection techniques and gamepad
  navigation patterns
- [MetaDeck](https://github.com/EmuDeck/MetaDeck) — inspiration for store patching patterns used in metadata display
  on non-Steam shortcuts

## License

GPL-3.0