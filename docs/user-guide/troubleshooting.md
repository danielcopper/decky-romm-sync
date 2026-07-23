# Troubleshooting

Common issues and how to fix them.

## Plugin Backend Won't Start

### Connection shows "Backend error"

**Symptom**: The RomM Sync QAM panel's **Connection** row shows a "Backend error" badge with the note "Plugin backend
failed to start — check Decky logs", and the Sync buttons are disabled. This state means the plugin's own Python backend
process never started — it is **not** the same as an unreachable RomM server, which shows **Not connected** instead.

**Fix**: The backend aborted during startup — most often a failed data migration after an update — so the UI can't reach
it. Open the Decky plugin log to find the underlying error, then reload RomM Sync from the Decky plugin list. If it
still fails after a reload, restart Steam (or the Steam Deck); if the error persists, include the log output when you
report it.

## Games Won't Launch

### BIOS files missing

**Symptom**: A game for a system that requires BIOS files (PlayStation, Saturn, Dreamcast, etc.) fails to launch or
shows a black screen.

**Fix**: Check the game's detail page — if the BIOS indicator is orange, you're missing required BIOS files. Go to the
**System** page in the QAM and tap **Download All** for the relevant platform. See [BIOS Management](bios-management.md)
for details.

### ROM not downloaded

**Symptom**: Pressing Play does nothing, or you get a toast saying the ROM needs to be downloaded.

**Fix**: Open the game's detail page and tap **Download** in the RomM Sync panel. The game will be playable once the
download completes.

### Controller doesn't work in RetroArch menus

**Symptom**: The game plays fine, but the RetroArch Quick Menu (L3+R3) can't be navigated with the controller — only
mouse/touch works.

**Fix**: This is caused by RetroArch using the `x` input driver on a Wayland system. If the plugin detects this, a
warning appears on the main QAM page with a **Change to sdl2** button. Tap it to fix the issue.

If the warning doesn't appear, you can manually change `input_driver = "x"` to `input_driver = "sdl2"` in your RetroArch
config file.

## Saves Not Syncing

### Auto-sync is disabled

**Fix**: Go to **Save Sync** in the QAM and make sure both "Sync before launch" and "Sync after exit" are toggled on.

### Server unreachable

**Symptom**: Toast notifications say "RomM unreachable" or sync operations show in the Failed Syncs list.

**Fix**: Check your network connection and verify the RomM server is running. The **Connection** row on the plugin's
main QAM panel shows the live status (and names the problem when it can't connect). Failed syncs are queued and retried
automatically when the server is reachable again.

### Offline detection and recovery

When the plugin can't reach RomM, it notices from the calls that fail (a connection probe, a save-status or slot load, a
version switch, or the first-time save-slot setup) and marks itself offline. On a game's detail page you'll see a **RomM
offline** badge in the play row, the **Download** button and slot switching are disabled (they need the server), and the
**Saves** tab shows a "RomM is offline" banner and renders straight away instead of hanging. The **Achievements** tab
does the same — it shows a short "RomM offline — achievements unavailable." line (keeping any list and counts it had
already loaded) instead of spinning on "Loading achievements…" forever. The first-time save-slot setup screen also shows
its "RomM server is not reachable" message and a **Retry** button immediately rather than working through the connection
attempts. Playtime keeps showing — it's tracked locally and doesn't depend on the connection.

While a save load is actually reaching the server, the **Saves** tab and the setup screen show a spinner labelled
**Connecting to RomM…**. If the server is briefly slow or flaky, the plugin retries a couple of times before giving up,
and the spinner shows which attempt is in progress — **Connecting to RomM… (attempt 2/3)** — so a slow connection reads
as busy rather than stuck.

You don't need to do anything: while a game page is open, the plugin checks the server roughly every 30 seconds in both
directions. If RomM goes away, the **RomM offline** badge appears on its own within that window; the moment RomM is
reachable again the badge clears, Download and slot switching re-enable, and the saves list, achievements, and setup
screen reload themselves on the spot — no need to leave and re-open the page.

Only a genuine connection failure marks the plugin offline. If RomM replies that it no longer has the thing being asked
about — most often because the game was deleted and re-added on the server, which gives it a brand-new id, or because
the RomM database was reset and this device's registration disappeared with it — that is the server _answering_, so the
**RomM offline** badge stays clear and the surface tells you what's missing instead. If you see such a message while the
badge is clear, the fix is on the RomM side (re-sync the library, or re-check the game), not with your network.

The first-time save-slot setup screen is the clearest example. If RomM can't find the save data that setup needs, it
pauses with "RomM couldn't find the save data for this setup" rather than the "server is not reachable" message, and the
offline badge stays clear. Setup deliberately stops there instead of picking a slot for you: the plugin has no
trustworthy view of what's on the server, and choosing a slot on that basis could overwrite real saves on the first
sync. Re-check the game in RomM (or, after a server database reset, reconnect in the plugin's Connection settings so
this device is registered again), then tap **Retry**.

### Save file not found

**Symptom**: The game detail page shows save status but no save file is being synced.

**Fix**: Save sync only works for RetroArch `.srm` save files. If you're using a standalone emulator (PCSX2,
DuckStation, Dolphin, etc.), saves for those systems are not yet supported.

Also verify the game actually creates a `.srm` file — some games use in-game passwords instead of battery saves.

### Saves being overwritten unexpectedly

**Symptom**: Your save keeps reverting to an older version after syncing.

**Fix**: Check your conflict resolution mode in **Save Sync** settings. If set to "Always Download", the server version
always overwrites your local save. Switch to "Newest Wins" or "Ask Me" if you play on multiple devices.

Also make sure each person using RomM has their own account — shared accounts cause saves to overwrite each other.

### Conflict prompt after a crash or power loss

**Symptom**: After an emulator crash, a full disk, or a power loss mid-save, the next sync shows a conflict prompt
instead of uploading — and the local save shown is 0 bytes or much smaller than expected.

**Why**: A crash can leave a 0-byte or truncated save file on disk. The plugin refuses to upload it over your good
server copy, because the server overwrites the existing save **in place** — there would be no older version left to
recover. So instead of silently destroying your progress, it asks you to choose.

**Fix**: In the conflict prompt, pick **Use Server** to restore the good copy from RomM (the bad local file is moved
aside into the `.romm-backup` folder next to your saves first, so nothing is lost). Only pick **Keep Local** if you are
certain the small/empty local file is the one you want to keep — that uploads it and replaces the server copy.

If you need to recover a save by hand, look in the `.romm-backup` folder inside your saves directory (e.g.
`<saves_path>/gba/.romm-backup/`): every file the plugin moves aside is timestamped there. The plugin moves a save aside
whenever it is about to be overwritten or removed — not just when resolving a conflict with **Use Server**, but also
when you switch save slots and when the setup wizard migrates a legacy save into a slot.

Backups are **not** kept forever. The folder keeps the **newest 10 backups per save file**; older ones are pruned the
next time that same save is moved aside. Backups of a different save file are never pruned to make room. If you want to
keep a particular recovery copy long-term, move it out of `.romm-backup` yourself.

## Artwork Missing

### No SteamGridDB API key

**Symptom**: Games have cover art but no hero banner, logo, or wide grid image. The detail page background is blank.

**Fix**: Configure a SteamGridDB API key in [Connection Settings](configuration.md#steamgriddb-api-key). It's free —
create an account at steamgriddb.com and copy your API key.

### Game not found on SteamGridDB

**Symptom**: Some games have full artwork while others are missing hero/logo/wide grid even with an API key configured.

**Explanation**: Automatic matching links your ROM to a SteamGridDB game via its IGDB id, and that cross-reference is
sometimes missing — especially for obscure ports and regional releases — even when SteamGridDB _does_ have artwork under
a differently-linked entry.

**Fix**: The durable fix is on your RomM server — set the correct SteamGridDB id on the game (or enable the SteamGridDB
metadata source and rescan). RomM is the source of truth for artwork matching, so the plugin picks up the corrected id
on the next sync or **Refresh Artwork**. When RomM _and_ the IGDB cross-reference both come up empty, **Refresh
Artwork** opens a picker where you can search SteamGridDB by name and apply a match on the spot — but that pick is
temporary and is replaced once RomM provides an id for the game. If a game genuinely has no artwork uploaded to
SteamGridDB, those slots fall back to Steam's defaults — you can contribute artwork there to help the community.

### Artwork not appearing after sync

**Symptom**: After a sync, some library tiles show Steam's default placeholder instead of the cover art.

**Explanation**: Covers are applied while the sync runs and fill in progressively, but Steam caches a brand-new
shortcut's tile image and does not always refresh it right away. A tile that is still gray usually resolves on its own
the first time you open that game's detail page, scroll it back into view, or the next time Steam restarts — the cover
is already on disk; Steam just hasn't re-rendered the tile yet.

**Fix**: If a tile stays blank after that, open the game's detail page and tap **Refresh Metadata** in the RomM Sync
panel. This re-fetches all artwork and metadata. (Hero banners, logos, and wide grid images additionally need a
[SteamGridDB API key](configuration.md#steamgriddb-api-key) — see above.)

## Shortcuts Appear on Other Devices

**Symptom**: Games synced on your Steam Deck also appear on your HTPC (or vice versa), but without artwork. They
disappear when the source device goes offline.

**Explanation**: This is Steam's Remote Play discovery protocol, not a plugin bug. Steam automatically advertises all
non-Steam shortcuts to other Steam clients on the same network. These "phantom" shortcuts are ephemeral — they only
exist while both devices are online.

The plugin cannot prevent this. Your options are:

- Disable Remote Play entirely in Steam Settings > Remote Play
- Ignore them — they show a "Stream" button instead of "Play" so they're distinguishable

For technical details, see [Steam Remote Play and Cross-Device Shortcuts](../architecture/steam-remote-play.md).

## Downloads Stuck or Failed

### Download shows no progress

**Fix**: Check your connection to the RomM server (the **Connection** row on the plugin's main QAM panel). If the server
is reachable, try cancelling and restarting the download from the game detail page.

### Download failed

**Fix**: Open the **Downloads** page from the QAM to see error details. Common causes include insufficient disk space,
network interruption, or the ROM being unavailable on the server. Failed downloads can be retried from the game detail
page.

## Danger Zone

The **Danger Zone** page provides options for removing shortcuts, ROM files, save files, and BIOS files. All destructive
actions require confirmation (tap once to see the prompt, tap again to confirm).

While a library sync is running (or cancelling), the shortcut and ROM removal actions and the grid-image cleanup are
unavailable — the buttons are disabled with a short hint, and the backend refuses the request too. Wait for the sync to
finish, or cancel it, before removing shortcuts or ROMs. Save-file and BIOS deletions are not affected.

A bulk shortcut removal is paced so it never freezes the interface, so on a large library it can take a few seconds.
While one is running, all the removal buttons are disabled and a spinner with a live **Removing x of y** counter shows
the progress; the buttons re-enable and the final count appears once it finishes. You can't start a second removal (or a
new one from another button) until the current one completes.

### Remove by Platform

Removes all shortcuts for a specific platform (e.g. all Game Boy Advance games). The associated Steam collection is also
cleaned up.

### Remove All RomM Shortcuts

Removes every shortcut that was created by the plugin, across all platforms. Collections are cleaned up. Does not delete
downloaded ROM files.

### Uninstall All Installed ROMs

Deletes all downloaded ROM files from disk. Shortcuts remain in your library so you can re-download later. Use this to
reclaim disk space.

### Remove Orphaned Grid Images

Deletes leftover Steam grid artwork (`grid/` cover, hero, logo, icon, and wide images) whose shortcut no longer exists.
Removing or re-creating shortcuts leaves these image files behind, and they accumulate over time.

An image counts as orphaned only when **all** of these hold:

- its filename is a Steam grid-image name for a **non-Steam shortcut** appId — custom artwork you saved for regular
  Steam games is never touched,
- that appId belongs to **no live shortcut** — the plugin scans your full shortcut list first, so artwork of shortcuts
  from other tools (Heroic, Lutris, manually added games, …) is protected too, not just RomM's.

The first tap runs a dry scan and shows the count in the confirm label (`Confirm: remove N orphaned images?`); the
second tap deletes. Deletion is permanent — there is no backup. If the shortcut scan can't run, or any synced RomM
shortcut is missing from it, the cleanup refuses and deletes nothing.

### Remove Non-Steam Games

Removes ALL non-Steam shortcuts visible to Steam — including games not managed by this plugin. Use with extreme caution.

A **whitelist** system lets you protect specific shortcuts from removal:

1. Tap **Configure Whitelist**
2. Toggle on any games you want to protect (RetroDECK is auto-protected by default)
3. Use the search box to find specific games in long lists
4. Protected games are excluded from the removal count

The plugin shows extra warnings if RetroDECK is not whitelisted, since removing it would break all emulation.

### Delete Save Files

Deletes local `.srm` save files for a specific platform. Shows each platform that has synced games, with the number of
games affected. This only removes local save files — saves already uploaded to your RomM server are not affected.

### Delete BIOS Files

Deletes downloaded BIOS files for a specific platform. Shows each platform that has BIOS files present locally. You can
re-download them later from the **System** page.

<!-- Screenshot: Danger Zone page showing removal options and whitelist -->

---

**Previous:** [Save Sync](save-sync.md)
