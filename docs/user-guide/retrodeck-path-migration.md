# RetroDECK Path Migration

If you move your RetroDECK installation to a different location — for example, from internal storage to an SD card, or
vice versa — the plugin detects the change and helps you migrate your downloaded files so everything keeps working.

## How It Works

Every time the plugin starts, it compares the current RetroDECK home path with the path it had stored from your last
session. If the paths differ, the plugin flags a migration. This typically happens after you use RetroDECK's built-in
move tool or manually relocate your `retrodeck/` directory.

The plugin does not move your RetroDECK files — RetroDECK handles that. What the plugin migrates are the files _it_
manages: downloaded ROMs, BIOS files, and save files that it tracks for sync purposes.

## Moving Again Before Migrating

You do not have to migrate right away, and you will not lose anything if you move RetroDECK a second time before you get
around to it. If you go from A → B and then B → C without running the migration in between, the plugin remembers
**every** home you left behind (A _and_ B), not just the most recent one. When you finally migrate, it collects your
files from whichever home each one actually lives under and brings them all to the current home (C) in a single pass.

- **The banner always reads "From: A → To: C"** — it names the _oldest_ pending home and your _current_ one, so you can
  see the full span of the move at a glance.
- **Reverting is handled.** If you move straight back to the home you just came from (A → B → A), the plugin recognizes
  it, drops the pending migration, and the banners disappear — there is nothing to migrate. Note that reverting only
  clears the pending migration; any files you created under the home you visited (for example, saves written while
  playing under B) are not automatically pulled back and stay where they were written.
- **Missing files are reported, not hidden.** If a file the plugin tracked can no longer be found at any of the homes it
  knows about (for example, you deleted it manually), the migration completes and its result tells you how many files
  were missing rather than silently declaring success.

## Warning Banners

Until the migration is completed, a yellow warning banner appears in two places:

- **QAM main page** — at the top, alerting you that a path change was detected
- **Game detail pages** — on every RomM game, reminding you that file paths may be out of date

These banners persist until you run the migration or the paths are resolved.

## Migrating Files

To perform the migration:

1. Open the QAM panel and go to **Connection Settings**
2. A **Path Migration** section appears when a migration is pending
3. The section shows a summary of what needs to move: e.g. "12 ROMs, 3 BIOS, 8 saves"
4. Tap **Migrate Files** to start

The plugin updates its internal tracking to point to the new paths and moves any files that need relocating.

## Conflict Handling

If files already exist at the destination (for example, you copied some files manually before migrating), a popup
appears with three choices:

- **Overwrite** — Replace the existing files at the destination with the ones being migrated
- **Skip** — Keep the existing files at the destination and just update the plugin's internal path references to point
  to them
- **Cancel** — Abort the migration entirely; no files are moved and no paths are updated

## What Gets Migrated

The migration covers three categories of files:

- **ROMs** — All ROMs tracked in the plugin's state (previously downloaded through the plugin)
- **BIOS files** — Both files the plugin downloaded and untracked files that match known entries in the BIOS registry
  (e.g. files you placed manually that the plugin recognizes)
- **Save files** — Recursively scanned from the save directories, excluding hidden directories (like `.git` or `.tmp`)

Files that the plugin doesn't know about — ROMs you added outside the plugin, for instance — are not affected.
RetroDECK's own move tool handles those.

---

**Previous:** [BIOS Management](bios-management.md) | **Next:** [Save Sync](save-sync.md)
