# Managing Games

After syncing, each game in your Steam Library that came from RomM has an injected **Tender** panel on its detail page.
This panel handles downloads, artwork, BIOS status, save sync, and more.

## The Game Detail Panel

When you open a RomM game in the Steam Library, you'll see the Tender panel below the standard Steam content. It shows:

- **Status badge** — "Installed", "Downloading", or "Not Installed"
- **Platform name** — which system the game belongs to (e.g. "Game Boy Advance")
- **Version switcher** — a compact control next to the Play button (beside the disc picker) to pick which version of the
  game is active, when RomM has more than one (see below)
- **Region / Languages** — attributes of the active version, shown in the Game Info tab when RomM has them (see below)
- **BIOS status** — whether required BIOS files are present (see [BIOS Management](bios-management.md))
- **Save sync status** — last sync time, conflict count, and playtime (see [Save Sync](save-sync.md))
- **Space required** — for a game that isn't downloaded yet, a "Space Required" cell next to the Play button shows how
  much disk space the download needs. It disappears once the game is installed (mirroring how Steam hides it for
  installed games); it's also omitted when RomM doesn't report the size.
- **Action buttons** — Download, Pause/Resume, Uninstall, Cancel, or Refresh Metadata depending on state

![Game detail page showing the Tender panel for an installed game](../assets/screenshot-game-detail.jpg)

## Versions

Many games exist in a RomM library as several **versions** — different dumps of the same title: a `(USA)` release and a
`(Europe)` one, a multi-language `(En,Fr,De)` dump, a `(Rev 1)` revision, a `(Demo)`. RomM groups these as a **sibling
group** (one game, many versions), and the plugin represents the whole group with a **single Steam shortcut**. The
version currently bound to that shortcut is the **active version** — the one the Download button fetches, and the one
that launches and syncs saves.

### The Switch-version control

When a game has more than one version, a compact **version control** appears next to the Play button — right beside the
disc picker, and only for a multi-version game. Open it to see every version in the group, each showing **its own cover
art** (so two regions of the same game are told apart at a glance; a version whose cover can't be fetched shows a disc
icon), with markers for:

- **✓ (active)** — the version currently bound to the shortcut. This is exactly what the Download button will fetch.
- **Default** — the version the plugin would pick on its own, following RomM's "SET DEFAULT" choice (`is_main_sibling`)
  and, failing that, your [Preferred region](configuration.md) setting. It's a suggestion, not a lock.
- **Downloaded** — a version you already have on disk.
- **not synced** — a version RomM has that isn't in your local library yet. Selecting it records it on the spot — unless
  it's dimmed as a conflicting metadata match (see below).

Some rows are **dimmed and can't be selected**, labelled _"conflicting metadata match in RomM"_. Versions of one game
are grouped by the metadata match they agree on — even when their coverage is uneven. A US and a EU dump that scrapers
matched differently (say one carries an IGDB id, the other only a ScreenScraper id) still group together and switch
freely, as long as the highest-priority match they share agrees. A row is dimmed only when its match genuinely
**conflicts** — it carries a _different_ id at that shared match, so RomM really treats it as a different game. This
holds whether or not the version is already in your library: a dump you've already synced under a conflicting match, and
a not-yet-synced version whose match conflicts, are both shown (so you know they exist) but can't be switched to —
either would move the shortcut onto a different game. To make one selectable, fix its metadata match in RomM so it
agrees with this game's, then re-sync.

A retained local version that RomM has positively confirmed it no longer has is also shown dimmed and disabled, with the
separate label _"No longer available on RomM"_. Its **active** and **Downloaded** markers remain visible so it is clear
what the shortcut and local files still refer to, but it never receives the **Default** badge. If the shortcut is still
active on that unavailable id, choose any live alternative in the same list to recover the game.

When the version the shortcut is bound to is the one confirmed gone, the **Download** button is greyed out as well — it
stays visible, like the version row, but downloading it could only ever fail. Pick a live version in the version list,
or remove the local data, to get the button back.

This availability check is fresh each time the version list loads and is not saved in the plugin database. Only a 404
for that exact ROM id establishes that it is gone. Timeouts, sign-in/permission errors, server errors, and malformed
responses fail open: the plugin does not disable a version merely because it could not get a trustworthy answer. A
ROM-specific 404 also does not make the rest of the plugin report that RomM is offline.

Selecting a different version **rebinds** the game to it: the Download button now fetches that version, the panel's
title and Region/Languages rows update to reflect it, and its cover refreshes to the new version. The Steam shortcut
keeps its name, its place in your collections, and its playtime — all tied to the shortcut, which never changes. A
single-version game shows no Version control.

The plugin checks the selected version again immediately before moving the shortcut. If RomM now answers that the exact
version no longer exists, the switch is refused with **Could not switch version**, nothing about the shortcut changes,
and the picker refreshes directly so the row becomes unavailable without starting a library sync. This protection also
applies after **Sync now & switch** and **Switch anyway**; the latter bypasses only the unsynced-save warning. If RomM
cannot provide a definitive answer because of a timeout, connection/sign-in/server error, or malformed response, the
local switch is allowed to continue. In particular, the offline **Switch anyway** path does not wait through the normal
retry sequence. A target-specific refusal does not by itself change the plugin's global online/offline status. When the
refusal follows a **Sync now & switch** whose upload already succeeded, the **Saves** tab is refreshed as well, so it
shows the saves that were just uploaded rather than the state from before the sync.

Switching **never deletes anything.** ROM files already on disk stay put, and save files are never moved or deleted by a
switch. What happens depends on whether the version you're leaving is downloaded and whether its saves are synced:

- **Not downloaded, or downloaded with all saves already synced** — the switch is instant. If the version you switch
  _to_ is on disk it's playable right away (Play stays Play); if it isn't, the button becomes **Download**.
- **Switching back to a version still on disk** — instant and playable immediately, with no re-download.
- **Leaving a downloaded version whose saves were never uploaded** — a dialog warns you first. It offers **Sync now &
  switch** (uploads the saves, then switches), **Switch anyway** (the saves stay on disk but stop syncing until you
  switch back to that version), and **Cancel**. When RomM is unreachable the saves can't be uploaded, so the dialog
  drops the sync option and offers only **Switch anyway** and **Cancel**, explaining why.

When you leave a downloaded version with unsynced saves behind, the **Saves** tab shows a reminder banner ("switch back
to sync them") so those saves aren't forgotten. It does not recommend switching back to a version positively confirmed
as no longer available on RomM; it skips that retained row and continues checking any later live versions.

While a download of the game is running, switching is blocked with a short message — cancel the running download first.

While a switch is being applied, the version control briefly shows a spinner and can't be reopened until the switch
settles and its version list refreshes — so a fast second tap can't act against the not-yet-updated list (for example,
switching straight back before the list has caught up).

### Cleaning up versions removed from RomM

Normal library sync never deletes retained local rows, installed files, saves, or playtime just because a game
disappears from RomM. To remove that state explicitly, open **Danger Zone → Clean Up Removed RomM Games**. The first
scan is local and finds groups containing rows absent from a completed platform fetch.

The dialog opens on the count that matters — how many locally kept versions are no longer on your RomM server — and
lists those versions first, under **Versions no longer on RomM**. Other versions of the same games follow under **Other
versions of these games — kept**: they were still on RomM at your last sync, and they go only if the run's fresh check
finds every version of that game gone, in which case the whole game is removed with its Steam shortcut. They are listed
so nothing can be removed without having been shown, and they appear only while **Remove fully vanished games** is on —
with that option off they cannot be removed at all, so the list drops them. The headline count always counts the
versions that are gone, never these. The modal byte-budgets every page for the Decky bridge; load every page before
confirmation. The confirmation run checks every exact RomM id again. Only a confirmed 404 can be removed. Offline,
timeout, authentication, server, malformed-response, active-download, and ambiguous multi-shortcut cases are skipped and
reported without deleting data.

The confirmation options apply to this run only:

- **Repoint vanished shortcuts to the live default version** is on. It preserves the Steam shortcut and its appId,
  collections, artwork, and playtime while switching to the group's natural live Default. This option works
  independently of row removal.
- **Remove confirmed rows and installed content from groups with a live version** is on.
- **Remove fully vanished games, including any Steam shortcut** is on. It applies only to games where the server
  confirms every single version is gone; those are removed whole, Steam shortcut included. It ships on because removing
  a game RomM no longer has is what this dialog is for, and because the default-on recovery bundle keeps the shortcut's
  Steam details so it can be rebuilt by hand. Switch it off to limit the run to individual versions of games that still
  exist — the list then hides the retained siblings, since nothing else can reach them.
- **Create recovery bundle** is on. Bundles are sealed under `~/decky-romm-sync-recovery/bundles/` before mutation.
  Leaving it on is what makes whole-game removal reversible by hand; turning it off asks you to confirm separately.
- **Include installed ROM content** is off for every disclosed installed row. Its exact recursive size is shown;
  selecting more than the currently free recovery space blocks confirmation. Turning recovery off clears and disables
  these selections, and so does switching off whole-game removal for a row that is only listed because of it. Large
  selections are staged in bounded pages before the run without a total selection cap. Unselected installed content is
  still deleted if the row is removed. When no listed version has ROM files on this device the option has nothing to
  attach to, so the list says so instead of leaving the option apparently missing.

The displayed selected-content total is a lower-bound preflight, not the complete bundle size. Use **Refresh free
space** after freeing disk space. The backend remeasures selected ROMs plus mandatory saves, backup history, caches, and
Steam files before mutation and safely fails the group if the complete bundle does not fit.

Recovery always records the affected database state, local playtime and pending sessions, exact attributable current
saves (including path-safe filenames retained in prior save-sync state) and backup history, and relevant plugin caches.
Fully vanished shortcut recovery also records bounded Steam details, collections/playtime fields, grid art, Steam Input
files, and the controller setting. It contains no settings, credentials, tokens, whole database, BIOS, or save states.
Recovery is manual only: there is no restore UI, and a new Steam shortcut cannot inherit the recorded Steam-assigned
appId or playtime. Because the folder is the recovery interface, it is written to be read months later. Each bundle is
named `<game>_<date>_<id>`, and its `README.txt` lists every game the bundle covers and what the run did to it, every
copied file with the exact path to put it back, the playtime in hours and minutes, and the steps to restore by hand
starting with `sha256sum -c checksums.sha256`. The recovery folder itself carries a `README.txt` explaining what it is.

A synced vanished row carries a red trash icon at its right edge in the version picker, and activating that row opens
the same confirmation scoped to that one version. A synced singleton vanished binding shows the same trash action as a
single button, without opening an otherwise empty picker. The vanished version itself remains non-switchable either way
— selecting it can only ever start the cleanup, never rebind the shortcut to it. A vanished version with no local data
to remove stays listed and disabled, with no trash icon.

While a cleanup runs, the Danger Zone shows a progress bar under the entry point — the game being worked on, the phase
in words (checking with RomM, backing up, backup complete, updating or removing the Steam shortcut, removing local data,
done) and which game of how many it is on — and the scan button stays unavailable, so you can close the dialog and still
see what is happening. The bar itself fills with the games already finished, not the one in progress, so it reaches the
end only when the run does: backing up a large game takes a while and reports nothing while it copies, and a full bar
sitting over it would say the run was done when it was not. Both the dialog and the Danger Zone offer **Stop Cleanup**.
It stops at the next safe moment: a backup still being written is abandoned and that game is left completely untouched,
while a game whose removal has already begun finishes and reports what it changed. Backing up a large game can take
minutes, so being able to walk away from one is the difference between stopping now and waiting it out. While the run
winds down the button says **Stopping** so a second press is never needed. Nothing already done is undone — stopping is
not an undo, and the report afterwards lists exactly what was committed. If a game's backup finished before the run
stopped, the report says so and names the folder, so a recovery bundle that removed nothing is never a mystery.

If Steam changes before local cleanup can finish, the report can show a **partial** group with the concrete committed
action and failure message. A confirmed shortcut removal is reconciled to an unbound retained row; a committed repoint
remains bound to the new Default. If Steam removal succeeded but every completion report was lost, the result is marked
ambiguous and source data stays retained; the same applies when Steam removal was attempted but its absence could not be
confirmed. Retrying confirms an already-absent shortcut instead of removing it twice. Save ownership warnings are shown
in a focusable terminal-detail region even when the group was removed successfully, and a run-level cancellation or
failure message remains visible after earlier groups committed. If bounded warning or message text was omitted or
shortened, the detail distinguishes omitted warnings from shortened displayed text and never reports zero additional
warnings. Progress is tied to the preview that you confirmed, so a matching run is still shown if only the successful
start response is delayed or lost; a matching terminal event makes the modal closable immediately. Once that terminal
result is assembled, delayed frames for the same run cannot replace it. Frames from an older preview are ignored. These
outcomes are intentional and retryable rather than being reported as unchanged.

### Region and Languages

In the panel's **Game Info** tab, the plugin shows the **Region** (e.g. `USA/Europe`) and **Languages** (e.g. `En, Fr`)
of the **active version**, when RomM has them. These are attributes of that one version — they change when you switch
versions, and a version with no region/language detail simply omits the rows. The values refresh on every sync.

## Downloading ROMs

Games appear as shortcuts in your library even before the ROM file is downloaded. Before downloading, the panel shows a
**Space Required** cell next to the Play button so you know how much disk space the ROM needs. To download:

1. Open the game's detail page in the Steam Library
2. In the Tender panel, tap **Download**
3. A progress bar shows download status with bytes transferred
4. When complete, the status changes to "Installed" and the game is ready to play

<!-- Screenshot: Game detail page during a download with progress bar -->

You can also abort a download in progress — tap the **X** that appears on the right of the download button on the game's
detail page, or use **Cancel** in the QAM download queue. Only the partial transfer files are cleaned up — an
already-installed copy of the game is never removed, so cancelling a re-download (or a download that fails partway)
leaves your existing install intact. If the cancel happens to land just as the download finishes, the game is kept as
**Installed** rather than torn down.

Downloaded ROMs are stored in your RetroDECK roms directory (e.g. `~/retrodeck/roms/gba/`).

**Only one version of a game is downloaded at a time.** If you tap **Download** on a version while another version of
the same game (its [sibling group](#versions)) is already on disk, the plugin removes the old install first and then
downloads the new one — no prompt. This keeps a multi-version game to a single copy on disk. Your **save files are never
touched** by this cleanup, so switching back and re-downloading the earlier version rejoins its saves. Games that still
carry a separate shortcut per version from an older release are left untouched.

### Pausing and Resuming a Download

A running download can be **paused** and later **resumed** without losing the progress already transferred. On the game
detail page, a resumable download shows a chevron next to the progress button — open it and choose **Pause**; the button
freezes at its current progress and shows **Paused**, and the same menu then offers **Resume**. You can do the same from
the QAM download queue, where a downloading item gets a **Pause** button and a paused item gets a **Resume** button (a
paused download stays in the active list, not the finished one).

Resume is only available for **single-file ROMs on a direct connection** — the server has to support resuming a transfer
from where it left off. Two cases can't resume, so they show only **Cancel** (no Pause/Resume):

- **Multi-file ROMs** (multi-disc or bin/cue titles downloaded as a single ZIP), and
- **Servers behind Cloudflare** (the Cloudflare Tunnel doesn't honour partial-content requests).

In those cases, cancelling and starting over is the only option — but a fresh download is safe, as cancelling never
removes an already-installed copy.

### Multi-Disc and Multi-File Games

Some games ship as more than one file — multi-disc PS1 titles, a base game plus updates and DLC, a BIN+CUE pair. RomM
downloads these as a single ZIP, which the plugin extracts automatically. You just download and play; the layout is
handled for you.

After the byte transfer finishes, the download button and the QAM queue show a brief **Extracting…** phase with its own
progress (the percentage climbs back from 0 as the archive unpacks — a large Switch or disc image takes a moment). The
extraction can't be cancelled, so the cancel/pause controls are replaced by a spinner until it's done; the game then
flips to **Installed** as usual. Single-file ROMs download as a bare file and skip this phase entirely.

The plugin gives the extracted game its own folder and names that folder after the real **launch file** (including the
extension, e.g. `Final Fantasy VII (USA).m3u/` or `Halo 3 (USA).iso/`) so that ES-DE collapses it into a single game
entry instead of showing a folder plus loose files.

**Disc switching only applies to systems whose emulator supports it.** For the disc-swapping consoles — PS1, Saturn,
Sega CD, PC Engine CD, Dreamcast, GameCube, Wii, and the like — a game-named `.m3u` playlist is generated so you can
flip between discs in-game, and the folder is named after that playlist. The plugin decides this by reading ES-DE's own
per-system supported-extension list, so a game only ever gets an `.m3u` on a system where ES-DE (and the emulator behind
it) actually understands one.

**Cartridge and folder systems get no playlist.** Switch (`.nsp`), Xbox 360 (`.iso`), and other systems with no disc
concept collapse to their real game file instead — the folder is named after the cartridge/disc image (`<Game>.nsp/`,
`<Game>.iso/`) and the game launches straight from it. RomM bundles a generic `.m3u` into every multi-file ZIP, but the
plugin ignores it on these systems rather than launching from a file the emulator can't read.

Single-file titles (most `.chd`/`.iso` games on disc-image systems) download as a bare file with no folder, so they need
no playlist either way.

!!! note "Known limitation"

    Games installed **before** this version keep their old folder layout, so ES-DE may still show them as a folder plus
    loose files — or, on cartridge systems, as a stray `.m3u`. The fix only affects new downloads; re-download the game
    to get the single clean ES-DE entry.

### When a download has nothing launchable

A few titles are distributed as an **installer** rather than as playable content — most commonly a PS3 game shipped as a
`.pkg` (plus a `.rap` licence file), where the game stays sealed inside the package until an emulator installs it. A
disc rip that arrived as raw `.bin` tracks with no `.cue` or `.gdi` alongside them has the same problem.

The plugin checks what the download actually produced against the list of formats the system can open. When nothing in
it qualifies, the shortcut is left **without a launch command** and the game's **ROM File** section says so, instead of
writing a command that would fail the first time you press Play. Pressing Play shows the same explanation.

**Your download is kept.** The files stay on disk where the ROM would normally live, and **Uninstall** works as usual —
because installing the package by hand in the emulator is exactly what you would do next. See
[the troubleshooting entry](troubleshooting.md#the-download-has-no-launchable-file) for how.

## Picking a Disc for Multi-Disc Games

When an installed game has more than one disc, a small **disc dropdown** appears on the game detail page, right next to
the **Play** button. Its face shows the disc that will launch (a 💿 with the disc's label), so it doubles as a badge —
single-disc games show no dropdown at all.

To pick a disc:

1. Open the game's detail page in the Steam Library.
2. Tap the **💿 disc dropdown** next to Play.
3. Choose the disc (or "All discs" — see below). The Play button now launches that disc.

That's the whole flow — there's no separate save step. The next time Steam launches the game, it launches the disc you
picked.

### What "All discs" means

On the **disc-swapping consoles** (PS1, Saturn, Sega CD, PC Engine CD, Dreamcast, GameCube, Wii, and the like) a
multi-disc game gets a generated `.m3u` playlist, so the dropdown's default entry is **All discs (m3u)** — it launches
through the playlist and lets you flip between discs **inside the emulator**, no relaunch needed. You can still pick a
single disc from the same dropdown to jump straight to it.

On systems with **no playlist concept**, there's no in-emulator disc switching, so the dropdown defaults to **Disc 1**
and switching discs means picking a different disc here and relaunching. Either way, the picker works the same.

The dropdown only ever lists discs your emulator can actually launch — it reads ES-DE's own per-system file-type list,
so you'll never be offered a disc image the emulator can't open.

### Your pick sticks

The selected disc is **saved**, and it survives the things that would otherwise reset it:

- **A library re-sync** never changes your pick.
- **Uninstalling and re-downloading** the game restores it — the choice is remembered against the game, not the
  downloaded files, so a reinstall re-applies it automatically.
- A **RetroDECK path migration** (moving your RetroDECK home) keeps it too.

If the specific disc you pinned ever goes missing (for example a partial re-download), the game quietly falls back to
the default disc rather than failing to launch.

### Where it applies

The disc you pick applies to **the Steam shortcut this plugin created for the game**, whenever Steam launches it — in
**game mode** (the couch UI) or **desktop mode**. It does **not** touch any custom non-plugin shortcut you set up
yourself (for example a hand-made Xenia or standalone-emulator shortcut) — those carry their own launch command, which
the plugin doesn't manage.

!!! note "RetroDECK is the supported launcher"

    The disc picker launches through RetroDECK, the supported launcher today. PS1 and the other `.m3u` disc-swapping
    systems are fully covered; systems that need an external standalone emulator to run at all are a separate roadmap
    item.

## Uninstalling ROMs

To remove a downloaded ROM file:

1. Open the game's detail page
2. Tap **Uninstall** in the Tender panel
3. The ROM file is deleted from disk
4. The shortcut remains in your library so you can re-download later

This only removes the ROM file — the Steam shortcut, artwork, and metadata are preserved.

The button switches to **Uninstalling…** as soon as you tap it, and a game made of many files counts them down as they
go. Pressing it again while that is on screen does nothing — the removal already running is the one that finishes, and
the plugin refuses a second one for the same game rather than letting two run against the same folder. That holds across
both entry points: **Uninstall All ROMs** in the Danger Zone claims every game it is about to remove, so it is refused
while a single uninstall is running, and a single uninstall is refused while the bulk run holds that game. If the plugin
is reloaded or the Deck shuts down mid-removal, the next uninstall of that game picks up where the interrupted one
stopped.

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

- Active downloads with progress bars, plus pause/resume and cancel buttons (pause/resume only where the download is
  resumable — see [Pausing and Resuming a Download](#pausing-and-resuming-a-download))
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
4. For a multi-disc game, the [disc you picked](#picking-a-disc-for-multi-disc-games) is the one that launches

If the ROM is not downloaded, pressing Play won't launch a game — download it first from the game's detail panel; the
shortcut's command is filled in automatically when the download completes.

### Stopping a running game

While a game is running, its detail panel shows **Resume** instead of Play, with a small chevron beside it. The
chevron's menu holds one action: **Stop Game**. It asks you to confirm first, because any progress since your last
in-game save may be lost.

Steam's own "Stop Game" cannot end these games. Your shortcut starts RetroDECK through Flatpak, and Flatpak launches the
emulator outside the process tree Steam watches — so Steam has nothing to stop, and pressing its button does nothing at
all. The plugin's Stop Game finds the emulator itself and ends it.

It ends **only the game you pressed it for**. RetroDECK can be running more than one thing at a time — a second game
launched from another shortcut, or ES-DE opened on its own — so the plugin identifies the session by the ROM the button
belongs to and leaves the rest alone. If it cannot pin down that game's session — nothing matches, or it cannot tell two
sessions apart with certainty — it stops nothing and tells you so: ending someone else's game mid-save would be worse
than not stopping at all. Should that happen while the game really is running, Resume stays available, and quitting from
inside the emulator always works.

It asks the emulator to quit politely **once**, waits a few seconds for it to shut down (which is when emulators write
their save file), and only forces it if it is still running after that. The polite request is deliberately never
repeated: most emulators treat a second one as "quit right now" and skip writing the save entirely, which would destroy
the file the first request was in the middle of saving. If you want to be certain your progress is kept, save in-game
first, then stop.

Because of that, the menu item greys out and reads **Stopping…** while it works. Those few seconds of nothing visibly
happening are normal — the emulator is writing your save. Pressing Stop again would be the very thing that discards it,
so the plugin ignores a second press until the first one finishes.

---

**Previous:** [Syncing Your Library](syncing-your-library.md) | **Next:** [BIOS Management](bios-management.md)
