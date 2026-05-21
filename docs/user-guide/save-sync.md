# Save Sync

Save sync keeps your game saves in sync between multiple devices through your RomM server. Play a game on your Steam Deck, then continue where you left off on your HTPC — your saves travel with you.

## How It Works

The plugin uploads and downloads RetroArch save files (`.srm`) to and from your RomM server. When you start a game, the plugin checks if the server has a newer save and downloads it. When you stop playing, it uploads your updated save.

> **Important:** Save sync (pre-launch download, post-exit upload, conflict detection) only runs when you launch games from the **game detail page** using the plugin's Play button. Launching from context menus, search results, or the recent games shelf bypasses the sync flow. Always open the game page and use the Play button to ensure your saves stay in sync.

Sync uses a **newest-wins** model with a hash-divergence guard:

- The plugin asks the RomM server for the saves in your active slot for the game and picks the newest (highest `updated_at`).
- If the server tracks your device as up-to-date and your local file matches the recorded baseline, there's nothing to do.
- If another device pushed a newer save and your local file is unchanged, the new server save is downloaded silently.
- If you played offline and your local file changed, your save is pushed back to the server.
- A conflict modal only appears if **both sides changed** since the last sync — the plugin won't silently overwrite either version.

This is the same model used by the official RomM clients (Argosy and Grout). It keeps cross-device save sync simple: one timeline per slot, newest wins.

## Important: Use Your Own RomM Account

Save files in RomM are tied to the authenticated user account. If multiple people share the same RomM account, their saves will overwrite each other. Each person should have their own RomM account with their own credentials configured in the plugin.

## Supported Systems

Save sync currently supports **RetroArch per-game `.srm` saves only**. This covers all systems that use RetroArch cores through RetroDECK:

- NES, SNES, Game Boy, Game Boy Color, Game Boy Advance
- Genesis / Mega Drive, Master System, Nintendo 64
- PlayStation (via RetroArch cores like SwanStation/Beetle PSX)
- Saturn, Dreamcast, PC Engine / TurboGrafx-16
- Neo Geo Pocket, WonderSwan, Atari Lynx

Standalone emulator saves (PCSX2, DuckStation, Dolphin, PPSSPP, melonDS, etc.) are **not yet supported** and are planned for a future update.

## Settings

Open **Save Sync** from the main QAM page to configure sync behavior.

[Screenshot: Save Sync settings page showing auto-sync toggles]

### Auto Sync

- **Sync before launch** (default: on) — runs sync from the game detail page when you tap Play. If the server is unreachable, the game launches with whatever local save exists.
- **Sync after exit** (default: on) — runs sync after closing a game. Shows a toast notification on success.

### When saves conflict

The plugin uses a single, automatic resolution policy:

- **Newest server save in your slot wins** by default. If your local save hasn't changed since the last successful sync, the server version is downloaded silently.
- **Your local edits push automatically** when the server still considers your device up-to-date — for example, you played offline and no other device synced in the meantime.
- **A conflict modal appears only when both sides changed** since the last sync. You pick which version to keep.

Tap **Sync All Saves Now** to sync saves for all installed ROMs at once. This is useful for:

- Bulk backup before uninstalling ROMs
- Catching up after a period of offline play
- Verifying that all saves are in sync

## Resolving Conflicts

When both your local save and the server save have changed since the last sync, a modal appears with both versions.

[Screenshot: Sync conflict modal with local and server save details and three buttons]

Each side shows:

- File size
- Modified / uploaded timestamp

Three actions:

- **Keep Local** — uploads your local save to the server, overwriting the server version.
- **Use Server** — downloads the server save and overwrites your local file.
- **Cancel** — dismisses the modal without changing anything. The conflict will reappear on the next sync as long as both sides still differ. If another device pushes an update in the meantime, the situation may resolve automatically (your unchanged local file gets the new version) and the modal won't reappear.

The modal blocks the Play action until you choose. If a post-exit sync detects a conflict you'll see a toast — the modal opens the next time you tap Play, where it blocks launch until resolved. There is no longer a separate "pending conflicts" list on the settings page.

## Core Switch Warning

When you switch the emulator core for a game (e.g., from mGBA to gpSP for GBA), the plugin detects the change and shows a warning before launching. This is because some cores use incompatible save formats — launching with a different core may overwrite your existing save with data the previous core can't read.

The warning shows which core you're switching from and to. You can:

- **Continue** — launch with the new core (your save may be overwritten)
- **Cancel** — go back and switch the core back before launching

### Per-game override warning (temporary)

You may see a second warning box in the same dialog titled **Per-Game Core Switch Not Working**. This is a temporary notice about a RetroDECK bug (tracked in [#210](https://github.com/danielcopper/decky-romm-sync/issues/210)): per-game core overrides are currently ignored for ROMs whose filenames contain special characters like parentheses (`()`), brackets (`[]`), or other punctuation. For those ROMs, switching the core per-game has no effect — RetroDECK silently falls back to the system-wide core.

**Workaround**: switch the core **system-wide** via the QAM panel (Core Switching) rather than per-game. This second warning will disappear from the dialog once RetroDECK fixes the underlying issue.

### Which cores are compatible?

Most cores for the same system produce compatible `.srm` saves because the save format is defined by the original hardware, not the emulator. However, there are exceptions:

| System | Cores | Compatible? | Notes |
| --- | --- | --- | --- |
| SNES | Snes9x, Supafaust, bsnes | Yes | All dump raw cartridge SRAM identically |
| PSX | Beetle PSX, PCSX ReARMed | Generally yes | Both use RetroArch's `.srm` memory card convention, but verify after switching — historical edge cases exist with memory card handling |
| N64 | Mupen64Plus, ParaLLEl | Generally yes | ParaLLEl-N64 is based on Mupen64Plus-next and shares save logic; save-type auto-detection can occasionally mismatch between cores |
| GBA | mGBA, VBA-M | Generally yes | Minor edge cases with save type detection |
| **GBA** | **mGBA ↔ gpSP** | **No** | **gpSP uses different save type detection; may corrupt saves** |
| **NDS** | **MelonDS ↔ DeSmuME** | **No** | **Completely different save formats** |

> **If in doubt, don't switch cores mid-playthrough.** Before switching, back up your `.srm` file — copy it somewhere safe (e.g. a `saves-backup/` folder). If the new core loads your save correctly, you can delete the backup. If it doesn't, you can restore the backup and switch the core back.

<!-- TODO: Expand this section with more tested core combinations as user reports come in -->

## Offline Behavior

If the RomM server is unreachable when a sync is attempted:

- **Before launch**: the game starts normally with your local save (a toast notification informs you).
- **After exit**: the upload is skipped. Your local save is untouched, and the next sync attempt produces the same outcome — typically pushing your changes once the server is reachable again.
- No save data is ever lost due to a failed sync.

## Playtime Tracking

The plugin tracks playtime per game. Session start and end times are recorded, and device suspend/resume is accounted for (sleep time is excluded). Playtime is displayed on the game detail page next to the save sync status.

Steam also tracks playtime natively for non-Steam shortcuts, so you'll see playtime in the standard Steam UI as well.

## Save File Location

Save files are stored in your RetroDECK saves directory. The exact path is read from RetroDECK's configuration at runtime — typically:

- **Internal SSD**: `~/retrodeck/saves/{system}/{rom_name}.srm`
- **SD card**: `/run/media/deck/Emulation/retrodeck/saves/{system}/{rom_name}.srm`

## RetroArch Save Sorting Requirement

Save sync expects save files to be organized as `{saves_dir}/{system}/{rom_name}.srm`. This matches the **RetroDECK default** RetroArch configuration:

| RetroArch Setting | Required Value | RetroDECK Default |
| --- | --- | --- |
| Sort Saves into Folders by Content Directory | **ON** | ON |
| Sort Saves into Folders by Core Name | **OFF** | OFF |

> **If you changed these settings in RetroArch, save sync will silently fail to find your save files.** No error is shown — saves simply won't sync.

### What happens with other configurations

RetroArch has four possible save sorting combinations. Only the first one is supported:

| Content Directory | Core Name | Save path | Supported? |
| --- | --- | --- | --- |
| ON | OFF | `saves/gba/game.srm` | Yes |
| OFF | ON | `saves/mGBA/game.srm` | No |
| ON | ON | `saves/gba/mGBA/game.srm` | No |
| OFF | OFF | `saves/game.srm` (flat) | No |

If you use "Sort by Core Name" (alone or combined with Content Directory), your saves end up in a subfolder named after the core (e.g., `mGBA`, `duckstation`, `Mesen`). The plugin does not search these subfolders.

### How to check your settings

In RetroArch: **Settings > Saving**. Look for the two "Sort Saves into Folders" toggles. On a fresh RetroDECK install, they are already set correctly.

### If your saves are already in core-name folders

If you previously played with "Sort by Core Name" enabled, your existing `.srm` files are inside core-named subfolders. You have two options:

1. **Move the files** back to the parent system directory (e.g., move `saves/gba/mGBA/*.srm` to `saves/gba/`)
2. **Change the RetroArch setting** back to Content Directory only (RetroArch will create new save files in the correct location on next launch — but your old saves stay in the core folder)

## RomM Version Compatibility

The plugin requires **RomM >= 4.8.1**. Servers below 4.8.1 are rejected at connection time with a full error page in both the QAM panel and the game detail view. The plugin uses v4.7+ features including server-side device tracking, content hashing, save slots, and `device_syncs` for conflict detection. The 4.8.1 minimum is set because that is the version the plugin has been tested against.

For technical details on how save sync works internally (three-way conflict detection, state schema, session detection), see the [Save File Sync Architecture](../architecture/save-file-sync-architecture.md) technical reference.

---

**Previous:** [RetroDECK Path Migration](retrodeck-path-migration.md) | **Next:** [Troubleshooting](troubleshooting.md)
