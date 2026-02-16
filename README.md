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

### From a release zip

1. Download the latest `decky-romm-sync.zip` from the [releases page](https://github.com/danielcopper/decky-romm-sync/releases)
2. Open Decky Loader's settings (gear icon in the QAM)
3. Use **Install Plugin From URL** and paste the direct link to the zip file
4. Alternatively, extract the zip manually to `~/homebrew/plugins/` on your device (via SSH, file manager, or USB)
5. Restart Decky Loader — either reboot, or run `sudo systemctl restart plugin_loader` via SSH

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

## License

GPL-3.0
