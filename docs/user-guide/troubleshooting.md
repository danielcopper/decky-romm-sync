# Troubleshooting

Common issues and how to fix them.

## Games Won't Launch

### BIOS files missing

**Symptom**: A game for a system that requires BIOS files (PlayStation, Saturn, Dreamcast, etc.) fails to launch or shows a black screen.

**Fix**: Check the game's detail page — if the BIOS indicator is orange, you're missing required BIOS files. Go to **BIOS Files** in the QAM and tap **Download All** for the relevant platform. See [BIOS Management](bios-management.md) for details.

### ROM not downloaded

**Symptom**: Pressing Play does nothing, or you get a toast saying the ROM needs to be downloaded.

**Fix**: Open the game's detail page and tap **Download** in the RomM Sync panel. The game will be playable once the download completes.

### Controller doesn't work in RetroArch menus

**Symptom**: The game plays fine, but the RetroArch Quick Menu (L3+R3) can't be navigated with the controller — only mouse/touch works.

**Fix**: This is caused by RetroArch using the `x` input driver on a Wayland system. If the plugin detects this, a warning appears on the main QAM page with a **Change to sdl2** button. Tap it to fix the issue.

If the warning doesn't appear, you can manually change `input_driver = "x"` to `input_driver = "sdl2"` in your RetroArch config file.

## Saves Not Syncing

### Auto-sync is disabled

**Fix**: Go to **Save Sync** in the QAM and make sure both "Sync before launch" and "Sync after exit" are toggled on.

### Server unreachable

**Symptom**: Toast notifications say "RomM unreachable" or sync operations show in the Failed Syncs list.

**Fix**: Check your network connection and verify the RomM server is running. Go to **Connection Settings** and tap **Test Connection**. Failed syncs are queued and retried automatically when the server is reachable again.

### Save file not found

**Symptom**: The game detail page shows save status but no save file is being synced.

**Fix**: Save sync only works for RetroArch `.srm` save files. If you're using a standalone emulator (PCSX2, DuckStation, Dolphin, etc.), saves for those systems are not yet supported.

Also verify the game actually creates a `.srm` file — some games use in-game passwords instead of battery saves.

### Saves being overwritten unexpectedly

**Symptom**: Your save keeps reverting to an older version after syncing.

**Fix**: Check your conflict resolution mode in **Save Sync** settings. If set to "Always Download", the server version always overwrites your local save. Switch to "Newest Wins" or "Ask Me" if you play on multiple devices.

Also make sure each person using RomM has their own account — shared accounts cause saves to overwrite each other.

## Artwork Missing

### No SteamGridDB API key

**Symptom**: Games have cover art but no hero banner, logo, or wide grid image. The detail page background is blank.

**Fix**: Configure a SteamGridDB API key in [Connection Settings](configuration.md#steamgriddb-api-key). It's free — create an account at steamgriddb.com and copy your API key.

### Game not found on SteamGridDB

**Symptom**: Some games have full artwork while others are missing hero/logo/wide grid even with an API key configured.

**Explanation**: SteamGridDB relies on community submissions. If a game doesn't have artwork uploaded there, those slots will show Steam's defaults. You can contribute artwork to SteamGridDB to help the community.

### Artwork not appearing after sync

**Fix**: Open the game's detail page and tap **Refresh Metadata** in the RomM Sync panel. This re-fetches all artwork and metadata.

## Shortcuts Appear on Other Devices

**Symptom**: Games synced on your Steam Deck also appear on your HTPC (or vice versa), but without artwork. They disappear when the source device goes offline.

**Explanation**: This is Steam's Remote Play discovery protocol, not a plugin bug. Steam automatically advertises all non-Steam shortcuts to other Steam clients on the same network. These "phantom" shortcuts are ephemeral — they only exist while both devices are online.

The plugin cannot prevent this. Your options are:

- Disable Remote Play entirely in Steam Settings > Remote Play
- Ignore them — they show a "Stream" button instead of "Play" so they're distinguishable

For technical details, see [Steam Remote Play and Cross-Device Shortcuts](../architecture/steam-remote-play.md).

## Downloads Stuck or Failed

### Download shows no progress

**Fix**: Check your connection to the RomM server (Connection Settings > Test Connection). If the server is reachable, try cancelling and restarting the download from the game detail page.

### Download failed

**Fix**: Open the **Downloads** page from the QAM to see error details. Common causes include insufficient disk space, network interruption, or the ROM being unavailable on the server. Failed downloads can be retried from the game detail page.

## Danger Zone

The **Danger Zone** page provides options for removing shortcuts, ROM files, save files, and BIOS files. All destructive actions require confirmation (tap once to see the prompt, tap again to confirm).

### Remove by Platform

Removes all shortcuts for a specific platform (e.g. all Game Boy Advance games). The associated Steam collection is also cleaned up.

### Remove All RomM Shortcuts

Removes every shortcut that was created by the plugin, across all platforms. Collections are cleaned up. Does not delete downloaded ROM files.

### Uninstall All Installed ROMs

Deletes all downloaded ROM files from disk. Shortcuts remain in your library so you can re-download later. Use this to reclaim disk space.

### Remove Non-Steam Games

Removes ALL non-Steam shortcuts visible to Steam — including games not managed by this plugin. Use with extreme caution.

A **whitelist** system lets you protect specific shortcuts from removal:

1. Tap **Configure Whitelist**
2. Toggle on any games you want to protect (RetroDECK is auto-protected by default)
3. Use the search box to find specific games in long lists
4. Protected games are excluded from the removal count

The plugin shows extra warnings if RetroDECK is not whitelisted, since removing it would break all emulation.

### Delete Save Files

Deletes local `.srm` save files for a specific platform. Shows each platform that has synced games, with the number of games affected. This only removes local save files — saves already uploaded to your RomM server are not affected.

### Delete BIOS Files

Deletes downloaded BIOS files for a specific platform. Shows each platform that has BIOS files present locally. You can re-download them later from the BIOS Manager.

[Screenshot: Danger Zone page showing removal options and whitelist]

---

**Previous:** [Save Sync](save-sync.md)
