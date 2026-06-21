# Managing Games

After syncing, each game in your Steam Library that came from RomM has an injected **RomM Sync** panel on its detail
page. This panel handles downloads, artwork, BIOS status, save sync, and more.

## The Game Detail Panel

When you open a RomM game in the Steam Library, you'll see the RomM Sync panel below the standard Steam content. It
shows:

- **Status badge** — "Installed", "Downloading", or "Not Installed"
- **Platform name** — which system the game belongs to (e.g. "Game Boy Advance")
- **BIOS status** — whether required BIOS files are present (see [BIOS Management](bios-management.md))
- **Save sync status** — last sync time, conflict count, and playtime (see [Save Sync](save-sync.md))
- **Action buttons** — Download, Uninstall, Cancel, or Refresh Metadata depending on state

![Game detail page showing the RomM Sync panel for an installed game](../assets/screenshot-game-detail.jpg)

## Downloading ROMs

Games appear as shortcuts in your library even before the ROM file is downloaded. To download:

1. Open the game's detail page in the Steam Library
2. In the RomM Sync panel, tap **Download**
3. A progress bar shows download status with bytes transferred
4. When complete, the status changes to "Installed" and the game is ready to play

<!-- Screenshot: Game detail page during a download with progress bar -->

You can also abort a download in progress — tap the **X** that appears on the right of the download button on the game's
detail page, or use **Cancel** in the QAM download queue. Only the partial transfer files are cleaned up — an
already-installed copy of the game is never removed, so cancelling a re-download (or a download that fails partway)
leaves your existing install intact. If the cancel happens to land just as the download finishes, the game is kept as
**Installed** rather than torn down.

Downloaded ROMs are stored in your RetroDECK roms directory (e.g. `~/retrodeck/roms/gba/`).

### Multi-Disc and Multi-File Games

Multi-disc games (e.g. multi-disc PS1 titles) are downloaded as a single ZIP from RomM, extracted automatically, and an
M3U playlist file is used for disc switching. This is handled transparently — just download and play.

The game's folder is named after a game-named M3U playlist (e.g. `Final Fantasy VII (USA).m3u/`) so that ES-DE collapses
it into a single game entry instead of showing a folder plus loose disc files. This applies to:

- **Multi-disc** ROMs (two or more disc files — any of `.cue`, `.chd`, `.iso`): the M3U is generated for disc switching.
- **Single-disc bin/cue** ROMs (one `.cue`): a game-named M3U is generated too, because the `.cue` itself is often
  generically named (`disc1.cue`, `track01.cue`) and would otherwise produce a `disc1.cue/` folder. bin/cue systems
  (PS1/PS2/Saturn/Sega CD/PC Engine CD, etc.) play cleanly from the playlist.

Single-disc `.chd`/`.iso` titles download as a single file (no folder), so they need no playlist.

!!! note "Known limitation"

    Games installed **before** this version keep their old folder layout (named without the extension), so ES-DE still
    shows them as a folder plus loose disc files. Re-download the game to get the single clean ES-DE entry.

## Uninstalling ROMs

To remove a downloaded ROM file:

1. Open the game's detail page
2. Tap **Uninstall** in the RomM Sync panel
3. The ROM file is deleted from disk
4. The shortcut remains in your library so you can re-download later

This only removes the ROM file — the Steam shortcut, artwork, and metadata are preserved.

## Refreshing Artwork and Metadata

Tap **Refresh Metadata** in the game detail panel to:

- Re-fetch hero banner, logo, wide grid, and icon from SteamGridDB
- Re-fetch game metadata (description, developer, genres, release date) from RomM
- Update the native Steam display with the latest information

This is useful if artwork was missing on first sync (SteamGridDB may have added new images since) or if metadata has
changed on your RomM server.

When you tap **Refresh Artwork**, the plugin asks your RomM server which SteamGridDB game the ROM maps to and applies
the hero banner, logo, wide grid, and icon for that game. **RomM is the source of truth**: whenever your server has a
SteamGridDB id for a game, that id wins — on both sync and refresh. If RomM has no id, the plugin tries to derive one
from the game's IGDB id. Only when neither resolves a SteamGridDB game does a picker open, where you search SteamGridDB
by name and choose from the results (with thumbnails). A name pick is applied immediately but is **not permanent** —
once your RomM server has a SteamGridDB id for that game, that id takes over. Because a manual pick isn't stored as the
resolved id, you can change it any time: just tap **Refresh Artwork** again and the picker reopens. To pin a specific
match for good, set the SteamGridDB id on the game in RomM.

The full set of per-game actions — refresh artwork, refresh metadata, sync save files, download BIOS, and uninstall — is
available from the RomM Actions menu in the game detail panel.

![RomM Actions context menu with Refresh Artwork, Sync Save Files, Download BIOS, and Uninstall entries](../assets/screenshot-actions.jpg)

## Download Queue

The **Downloads** page (accessible from the main QAM panel) shows all active and completed downloads:

- Active downloads with progress bars and cancel buttons
- Completed, failed, and cancelled downloads with status details
- **Clear Completed** button to clean up the list

At most **two** ROMs download at the same time. If you start more, the extra ones wait their turn and begin
automatically as soon as a slot frees up. Before a download starts, the plugin checks there's enough free disk space for
everything already in flight, so a batch of downloads won't overcommit the SD card.

<!-- Screenshot: Download Queue page with an active download and completed entries -->

## Launching Games

Select any installed game in the Steam Library and press **Play**. The full launch command is baked into the Steam
shortcut when the game is synced or downloaded, so launching just runs that command:

1. The shortcut launches RetroDECK with the correct ROM path
2. RetroDECK auto-detects the system from the ROM's directory path and uses the appropriate emulator
3. If you picked a [per-game core](bios-management.md#per-game-game-detail-page), the chosen core is baked into the
   command and used directly

If the ROM is not downloaded, pressing Play won't launch a game — download it first from the game's detail panel; the
shortcut's command is filled in automatically when the download completes.

---

**Previous:** [Syncing Your Library](syncing-your-library.md) | **Next:** [BIOS Management](bios-management.md)
