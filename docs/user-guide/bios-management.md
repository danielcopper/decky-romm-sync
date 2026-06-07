# BIOS and Emulator Core Management

Some emulated systems require BIOS files to run games. Without the correct BIOS files, games for those systems will fail
to launch. The plugin can download BIOS files directly from your RomM server.

Which BIOS files a system needs depends on the **emulator core** in use — some cores need BIOS, some don't. Because the
two concerns are related but independent, the plugin presents the **active core** and its **BIOS state** together in one
place: the **System** page, a top-level QAM destination. Core selection and BIOS file management can each be used on
their own — they share a screen only because the active core determines which BIOS files matter.

## What Are BIOS Files?

BIOS (Basic Input/Output System) files are firmware dumps from original hardware. Emulators need them to accurately
simulate the console's boot process. Common examples:

- **PlayStation** — `scph5501.bin` (and other regional variants)
- **Dreamcast** — `dc_boot.bin`, `dc_flash.bin`
- **Saturn** — `sega_101.bin`, `mpr-17933.bin`

Not all systems need BIOS files. Cartridge-based systems like Game Boy, SNES, and Genesis typically work without them.

## BIOS Status on the Game Detail Page

When you open a game that belongs to a platform with BIOS files on your RomM server, the game detail panel shows a BIOS
status indicator:

- **Green** — "BIOS ready (X files)" — all BIOS files are downloaded
- **Orange** — "BIOS required — X/Y downloaded" — some files are missing

The "BIOS missing" indicator is computed against the **active core** for that game — so switching to a core that needs
no BIOS (or that treats a file as optional) clears the warning, while switching to a core that requires a missing file
surfaces it.

Tap the BIOS status indicator to see a detailed list of individual files and which ones are present or missing.

<!-- Screenshot: Game detail page showing orange BIOS status with "3/5 downloaded" -->

![BIOS file list overlay showing individual required files with checkmarks and "Missing" labels](../assets/screenshot-bios.jpg)

## System Page

The **System** page is the per-system emulator settings page: for each platform it shows the **active emulator core**
first, then the BIOS files that core needs.

1. From the main QAM page, tap **System**
2. Platforms with synced games that still need required BIOS files appear first, marked with "BIOS needed"
3. For platforms with more than one available core, an **Emulator Core** dropdown is shown at the top of the platform's
   section — this is the primary per-system control
4. Below the core, each platform shows how many BIOS files are downloaded vs. available (e.g. "3 / 5 files")
5. Tap **Show Files** to see the individual file list for a platform
6. Tap **Download All** to download all missing BIOS files for a platform

<!-- Screenshot: System page showing per-platform Emulator Core dropdown above BIOS download counts -->

BIOS files are downloaded to your RetroDECK bios directory (e.g. `~/retrodeck/bios/`). Some platforms use subdirectories
— for example, Dreamcast BIOS goes into `bios/dc/` and PS2 BIOS goes into `bios/pcsx2/bios/`. The plugin handles the
correct placement automatically.

## Which Systems Need BIOS?

This depends on what's uploaded to your RomM server. Common systems that require BIOS files include PlayStation, PS2,
Saturn, Dreamcast, and some arcade systems. The plugin only shows BIOS status for platforms that have firmware files in
your RomM library.

## Per-Platform BIOS Filtering

The plugin only shows BIOS files that belong to the platform you're looking at. For example, a GBA game page shows
`gba_bios.bin` only — not Game Boy or Game Boy Color BIOS files, even though the emulator core (mGBA) supports all three
systems. This filtering is built into the BIOS registry and works automatically.

## Active Core Detection

Different emulator cores can have different BIOS requirements for the same platform. The plugin detects which core
RetroDECK is actually configured to use and filters the BIOS list accordingly, so you only see the files that matter for
your setup.

### Example: Game Boy Advance

- With **mGBA** (RetroDECK's default), `gba_bios.bin` is shown as _optional_ — mGBA has a built-in high-level BIOS
  replacement
- With **gpSP**, `gba_bios.bin` is shown as _required_ — gpSP cannot run without it

The active core name appears in both the game detail page (the **Emulator** column) and the **System** page. This tells
you at a glance which core the plugin is filtering for.

**How the core is determined:**

1. If a per-game override exists in ES-DE's `gamelist.xml` (via `<altemulator>`), the plugin uses that first
2. If no per-game override, the plugin checks for a per-system override in `gamelist.xml` (via `<alternativeEmulator>`)
3. The plugin reads RetroDECK's ES-DE configuration (`es_systems.xml`) from the flatpak installation to find the default
   emulator for each platform — the first listed RetroArch core is treated as the default
4. If the live configuration can't be read, the plugin falls back to a shipped `core_defaults.json` with RetroDECK's
   known defaults
5. If all detection fails, all BIOS files for the platform are shown — the safe default

The detection chain ensures BIOS filtering works even when RetroDECK's configuration files aren't accessible (e.g. after
an update changes paths). You'll see a "Core: mGBA" badge when detection is working, or no badge when falling back to
showing all files.

## Changing the Active Core

You can change the active emulator core directly from the plugin, without leaving Game Mode. Changes are written to
ES-DE's `gamelist.xml` so they persist across sessions and are picked up by both the plugin and ES-DE.

### Per-Platform (System Page)

On the **System** page, platforms with multiple available cores show an **Emulator Core** dropdown as the first control
in the platform's section, above the BIOS file list. Changing it sets the default core for all games on that platform. A
"Switching cores may affect save compatibility" note appears under the dropdown for platforms that offer a choice.

1. Open the **System** page from the main QAM page
2. Find the platform you want to change
3. Use the **Emulator Core** dropdown to select a different core
4. The BIOS file list below updates immediately to show files relevant to the new core

This writes a system-wide override to ES-DE's `gamelist.xml`. ES-DE will pick up the change on next launch. The System
page works even when your RomM server is offline — core switching and BIOS status are available, only download buttons
are disabled.

### Per-Game (Game Detail Page)

On the game detail page, a **CPU button** (microchip icon) appears between the RomM and Steam gear buttons when multiple
cores are available for the game's platform.

1. Open a game's detail page
2. Tap the **CPU button** (microchip icon)
3. Pick a core from the menu — the current core is marked with a checkmark
4. The BIOS status, core badge, and game info panel update immediately

A per-game override takes priority over the platform default. To reset back to the platform default, select the default
core (marked with "(default)") from the menu — this clears the per-game override.

### Non-Default Core Indicator

The CPU button changes color to indicate the active core status:

- **Gray** — the default core is active (no overrides)
- **Yellow** — a non-default core is active (per-game or per-platform override)

The game detail info panel shows the active core in a dedicated "Emulator" column alongside the BIOS status, using a
two-column layout.

---

**Previous:** [Managing Games](managing-games.md) | **Next:** [RetroDECK Path Migration](retrodeck-path-migration.md)
