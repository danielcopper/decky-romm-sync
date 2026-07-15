# Syncing Your Library

Syncing fetches your RomM game library and creates Non-Steam shortcuts in Steam for every game. After syncing, your
games appear in the Steam Library with cover art, metadata, and organized into collections.

## How Sync Works

1. The plugin fetches all ROMs from your RomM server (filtered by your enabled platforms)
2. For each ROM, a Non-Steam shortcut is created in Steam via the SteamClient API — no restart required
3. Cover art from RomM is applied as the portrait grid image
4. If you have a SteamGridDB API key configured, hero banners, logos, and wide grid images are also fetched
5. Metadata (description, developer, genres, release date) is cached and displayed in the plugin's custom game detail
   panel
6. Steam collections are created per platform (e.g. "RomM: Game Boy Advance (steamdeck)")

## Starting a Sync

1. Open the QAM and navigate to the plugin
2. Tap **Sync Library** on the main page
3. A progress bar shows the sync status
4. When complete, a toast reports what actually changed — the true delta, not the total in your library. It shows the
   number of shortcuts added and/or removed this run (e.g. "Sync complete — 42 added, 3 removed."), omitting a part that
   is zero. If nothing changed, it reads "Library up to date."

![RomM Sync QAM panel with connection status and the Sync Library button](../assets/screenshot-qam.jpg)

<!-- Screenshot: Sync in progress with progress bar -->

You can tap **Cancel Sync** to stop mid-sync. Games already added will remain. A cancelled sync never removes any Steam
collections — stale-collection cleanup only runs after a sync finishes in full, so cancelling can never wipe the
collections for platforms the run did not reach.

## Time estimate and progress

Before you start, the sync preview shows a **Changes** block: what the run will add, update, or remove, and below it one
line with the run's coverage and estimated time (for example "Syncing 3 platforms · 2 collections — 3 min"). The sync
only touches the games that are actually new or changed — games that are already correct in your library are skipped
entirely, not re-processed — so a re-sync of a mostly-settled library is quick, and the estimate reflects only that
changed work. If you sync without previewing first, the estimate appears once the run starts as an **Estimated time**
line, shown as "up to X min".

That starting estimate is **skip-aware**: when the run is planned, the plugin already knows which platforms haven't
changed since their last sync and expects to skip them wholesale, so they don't inflate the number — an incremental
re-sync of an unchanged library reads seconds, not the minutes a full first import would take. The prediction is only an
estimate (the actual skip is decided per platform as the run reaches it), so a wrong guess can make the readout run long
or short for a moment, but it never changes what the sync actually does.

Once the sync has been creating shortcuts for a few seconds, that "up to" ceiling is replaced by a **live countdown** —
"2 min left" — measured from the actual speed on your device and updated as the run proceeds. Both the countdown and the
progress counter show **net** progress: they count the games this run actually needs to add or update (say "100/801"),
not every game in your library. It holds steady across the short pauses where the sync fetches the next platform's game
list, rather than jumping around. The main progress bar apportions its width the same way — a platform expected to skip
takes no space, and a huge platform fills the bar in proportion to its real work instead of an equal slice per platform.

A few things worth knowing for a large library:

- **The progress line narrates each phase.** For a large platform the sync first pulls the game list from your server
  page by page and then downloads cover art before it starts creating shortcuts; the line names what it is doing (e.g.
  "Fetching Game Boy Advance (page 12/62)" then "Preparing covers for Game Boy Advance"), so a fetch that takes minutes
  never looks stuck.
- **Cover art fills in as the run goes.** Covers appear on your library tiles progressively while the sync creates
  shortcuts, not only at the very end, so you can watch the library fill in. A few tiles may stay gray until the first
  time you open that game's page (or the next time Steam restarts) — that is expected and does not mean the cover is
  missing.
- **Progress is saved as it goes** — roughly every 200 games. If Steam crashes or you cancel partway through, the games
  already created are kept, and the next sync picks up where it left off instead of starting over.
- **Cancelling keeps finished games.** Everything added before you cancel stays in your library; cancelling never
  removes finished games (and never removes Steam collections).
- **Sleep is safe; keep it powered.** If the Deck sleeps mid-sync the run pauses and resumes on wake — it does not stall
  or lose progress. For a large first sync, plug it in so the battery lasts the whole run.
- **A very large sync may pause itself to protect Steam.** Steam holds every shortcut it creates in memory for the rest
  of the session, and that memory only frees on a Steam restart. A very large first import can approach that limit, so
  the plugin watches Steam's memory and, when it gets close, pauses cleanly at a safe point rather than risking a Steam
  crash. If the preview expects this, it shows a blue note up front ("will likely pause partway to protect Steam's
  memory — normal for large syncs"). When a pause happens, the sync page shows a **blue banner** — "Steam memory is full
  (2.3 GB). 1200 of 2001 games done. Restart Steam, then Resume Sync." — that stays until you resume, and a toast says
  the same. The banner reports how far the run got, so you can see how much is left before you resume. Restarting Steam
  frees the memory and the resume finishes the job. Once you've restarted, the banner notices on its own — it changes to
  "Steam memory is free again (0.4 GB). 1200 of 2001 games done. Press Resume Sync to continue." and drops the restart
  button, so you know a resume will actually work now rather than pausing again. Nothing is lost, and you are never
  forced out of what you were doing. After a big run finishes with memory still high, a **yellow banner** recommends a
  Steam restart before your next large sync; it clears itself once you restart.
- **Tap "Restart Steam now" to free the memory.** Both banners include a **Restart Steam now** button. It restarts the
  Steam client (Steam closes and reopens) — the reliable way to reset its memory — and you can Resume Sync once it comes
  back. The button is disabled while a game is running (a restart would close your game), so close your game first; it
  is also unavailable while a sync is actively running.
- **The top of the panel shows Steam's current memory.** A **Steam memory** row sits alongside Connection and Last sync,
  so you can see how close Steam is to its limit at a glance (it's hidden only if the reading can't be taken); while a
  sync is running it refreshes every few seconds so you can watch the number climb. The number is colour-coded — green
  when there's plenty of headroom, yellow as it gets high, red once it's near the limit where syncs pause. On the same
  line it shows how much the last run (whether it finished, paused, or was cancelled) grew Steam's memory — for example
  "0.6 GB · last run +1.5" — so a big import tells you why the number climbed.

## Resuming an interrupted sync

Because progress is saved as the sync goes, a run that does not finish is never wasted — you just run sync again to
complete the job.

- **The main page tells you when the last run didn't finish.** The **Last sync** line normally shows when the last full
  sync completed. If your most recent run ended early, a second line reports that attempt and how it ended — "last
  attempt: 17:48 (interrupted)" if a crash or a Steam reload stopped it, "(paused)" if the memory guard paused it, or
  "(cancelled)" if you tapped Cancel Sync — so a partial run that still added hundreds of games never reads a misleading
  "Never". The end-of-run toast and the sync status line make the same distinction: a run stopped by a crash or Steam
  reload reads "Sync interrupted — … so far." rather than blaming a Cancel you never pressed, and the status line
  compares progress against the run's planned total (e.g. "3 of 10 games processed").
- **The Sync button becomes "Resume Sync".** When a run was cancelled, interrupted, or paused and left games in your
  library, the **Sync Library** button changes to **Resume Sync**. Pressing it completes the library: the platforms that
  already synced in full are skipped, and even in the platform that stopped, only the games it hadn't finished are
  processed — the ones already correct are skipped, so a resume finishes quickly and the counter shows just the
  remaining work. This is true whether or not you restart Steam in between. Once a run finishes in full, the button goes
  back to **Sync Library**.
- **Force Full Sync starts over from scratch.** Under the sync buttons, **Force Full Sync** clears the plugin's record
  of what it has already synced and re-fetches every platform from RomM on the next run — and that run also rewrites
  every shortcut instead of skipping the ones that look correct, so it repairs anything that drifted on the Steam side
  (a manually edited or broken shortcut). Reach for it if you suspect a platform is out of sync or want a clean rebuild;
  a normal Sync (or Resume Sync) is enough for everyday updates. It appears once you have run at least one sync.

## Multiple versions of a game

When your RomM library holds several dumps of the same game — region variants like `(USA)` / `(Europe)` / `(Japan)`,
multi-language dumps, or revisions — the plugin treats them as **one game** and creates **one Steam shortcut** for it,
not one per dump. RomM already groups these versions together; the plugin mirrors that grouping. Because of this, the
sync counts (in the preview and the completion toast) count **games**, not individual files: a five-region game is one
"added", not five.

The version the shortcut points at (the **active version**) is chosen automatically: a version you have already
installed wins, otherwise the shortcut follows the "SET DEFAULT" version you picked in RomM, otherwise the plugin picks
the best dump for you (see below). Switching versions from inside the plugin is a later feature; for now the active
version follows what is installed and RomM's default.

**How the plugin picks the best dump, and how the shortcut is named.** When nothing is installed and you haven't set a
default in RomM, the plugin ranks the dumps like a 1G1R (one-game-one-ROM) tool. A **finished release always beats a
prerelease** — a beta, prototype, alpha, sample or demo dump loses even to a finished release from a less-preferred
region (so a finished Japanese dump wins over a US beta). Among finished dumps, it prefers a region in the fixed order
**World → USA → Europe → Japan**, then any other region alphabetically, and a dump with no region last. (This is a fixed
order, not language or system detection.) Within one region it then prefers the **newest revision** (a `(Rev 1)` dump
over the plain release, a `(Rev 3)` over a `(Rev 1)`), and finally prefers the plain base game over a filename-only
re-dump like `(Virtual Console)` or `(Extended Edition)`. You can change the preferred region — see
[Preferred region](configuration.md#preferred-region) in Configuration. The **name** of the Steam shortcut follows the
same ranking (ignoring what's installed or set as default), so a multi-region game gets a readable name rather than
whichever dump happens to sort first: a game with two Japanese dumps and one US dump is named after the US dump, while a
game that only exists as a Japanese dump honestly gets its Japanese name. The name is chosen **once, when the shortcut
is created, and never changes automatically** afterwards — even if you later switch to a different version, change the
RomM default, or change the preferred region. This keeps the shortcut's artwork, collections and playtime intact. It
does mean the shortcut's name can differ from the version it currently launches, and that changing the preferred region
only affects games synced **after** the change.

If you synced **before** this update and already have several Steam shortcuts for one game, those existing shortcuts are
**kept** — the plugin never deletes a shortcut you can see. They converge to a single entry naturally as you uninstall
the extra versions.

## Per-Platform Toggles

Not every platform in your RomM library needs to be synced to Steam. Use the **Platforms** page to enable or disable
individual platforms.

1. From the main page, tap **Platforms**
2. Each platform shows its name and ROM count
3. Toggle platforms on or off
4. Use **Enable All** / **Disable All** for bulk changes
5. Only enabled platforms are included in the next sync

For a platform you have already synced, the count shows the number of **games** it syncs into Steam — multiple versions
of the same game (regional dumps, revisions) collapse into one shortcut, so this can be lower than the raw file count on
your server. A platform you have never synced shows the server's raw ROM count until its first sync.

All platforms are enabled by default until you change a toggle. Turning one platform off affects only that platform —
every other platform stays enabled and keeps syncing.

<!-- Screenshot: Platforms page with toggle switches and ROM counts -->

## Collections

The plugin automatically creates Steam collections for each synced platform. Collection names include your machine's
hostname to avoid conflicts if you run the plugin on multiple devices:

- `RomM: Nintendo 64 (steamdeck)`
- `RomM: Game Boy Advance (steamdeck)`
- `RomM: PlayStation (htpc)`

Collections appear in Steam's library sidebar and can be used to browse games by platform.

### Syncing RomM collections

The **Collections** page splits collections into three sub-tabs plus a dedicated top-level toggle for RomM favorites:

- **Sync RomM favorites** (top-level toggle) — the user collection RomM auto-manages as your favorites. Always exactly
  one per account, so it sits above the sub-tabs as a single switch.
- **My** sub-tab — your other user-created collections
- **Smart** sub-tab — filter-based collections that resolve membership at query time, so syncing always picks up the
  current matches
- **Franchise** sub-tab — auto-generated franchise groupings (IGDB)

Each sub-tab shows its visible count in the section header (e.g. `MY COLLECTIONS (4)`) and lets you toggle individual
collections, or use the paired **Enable All** / **Disable All** buttons to bulk-toggle just that sub-tab. The global
**Show collection games in platform groups** toggle controls whether games pulled in via a collection also get added to
their platform's Steam group.

## Artwork

Each synced game gets up to five types of artwork:

| Type                  | Source      | Where It Appears                |
| --------------------- | ----------- | ------------------------------- |
| Portrait Grid (cover) | RomM        | Library grid tiles, collections |
| Hero Banner           | SteamGridDB | Game detail page background     |
| Logo                  | SteamGridDB | Title overlay on hero banner    |
| Wide Grid             | SteamGridDB | Recent games shelf, list view   |
| Icon                  | SteamGridDB | Taskbar, small UI elements      |

Cover art is always applied from RomM. The other four types require a
[SteamGridDB API key](configuration.md#steamgriddb-api-key). Games without a SteamGridDB match will show Steam's default
placeholders for those slots.

You can refresh artwork for any individual game from its
[game detail page](managing-games.md#refreshing-artwork-and-metadata).

## Re-Syncing

Running sync again updates your library with any changes from RomM (new ROMs, removed platforms, etc.). Existing
shortcuts are updated rather than duplicated. If the specific version a shortcut pointed at is removed from RomM but the
game still has other versions on the server, the shortcut is **kept** and quietly re-pointed at a surviving version — it
is not torn down and re-created, so its artwork, collections, and playtime are preserved.

## Removing Shortcuts

To remove synced games, use the **Danger Zone** page. See
[Troubleshooting — Danger Zone](troubleshooting.md#danger-zone) for details on the available removal options.

If you delete a synced game directly from **Steam's own library** (rather than the Danger Zone), the next sync brings it
back. The plugin notices the shortcut is gone at sync start and re-creates it, so deleting through Steam is not a
permanent way to remove a RomM game — use the Danger Zone for that.

---

**Previous:** [Configuration](configuration.md) | **Next:** [Managing Games](managing-games.md)
