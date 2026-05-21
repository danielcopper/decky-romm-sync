# RetroDECK Path Migration

If you move your RetroDECK installation to a different location — for example, from internal storage to an SD card, or vice versa — the plugin detects the change and helps you migrate your downloaded files so everything keeps working.

## How It Works

Every time the plugin starts, it compares the current RetroDECK home path with the path it had stored from your last session. If the paths differ, the plugin flags a migration. This typically happens after you use RetroDECK's built-in move tool or manually relocate your `retrodeck/` directory.

The plugin does not move your RetroDECK files — RetroDECK handles that. What the plugin migrates are the files *it* manages: downloaded ROMs, BIOS files, and save files that it tracks for sync purposes.

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

If files already exist at the destination (for example, you copied some files manually before migrating), a popup appears with three choices:

- **Overwrite** — Replace the existing files at the destination with the ones being migrated
- **Skip** — Keep the existing files at the destination and just update the plugin's internal path references to point to them
- **Cancel** — Abort the migration entirely; no files are moved and no paths are updated

## What Gets Migrated

The migration covers three categories of files:

- **ROMs** — All ROMs tracked in the plugin's state (previously downloaded through the plugin)
- **BIOS files** — Both files the plugin downloaded and untracked files that match known entries in the BIOS registry (e.g. files you placed manually that the plugin recognizes)
- **Save files** — Recursively scanned from the save directories, excluding hidden directories (like `.git` or `.tmp`)

Files that the plugin doesn't know about — ROMs you added outside the plugin, for instance — are not affected. RetroDECK's own move tool handles those.

---

**Previous:** [BIOS Management](bios-management.md) | **Next:** [Save Sync](save-sync.md)
