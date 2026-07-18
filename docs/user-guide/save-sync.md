# Save Sync

Save sync keeps your game saves in sync between multiple devices through your RomM server. Play a game on your Steam
Deck, then continue where you left off on your HTPC — your saves travel with you.

## How It Works

The plugin uploads and downloads your RetroArch game saves to and from your RomM server. When you start a game, the
plugin checks if the server has a newer save and downloads it. When you stop playing, it uploads your updated save.

> **Important:** Save sync runs in **Game Mode only**, but there it covers every way a game can start: the Play button
> on the game detail page syncs directly, and a launch that skips the plugin's UI (for example a `steam://rungameid`
> deep link) is caught by the plugin's launch gate and synced before the game starts. In **Desktop Mode** the plugin is
> not loaded at all, so nothing syncs there — no pre-launch download, no post-exit upload. Changes made in Desktop Mode
> are picked up the next time you sync in Game Mode, which may surface a conflict.

Sync uses a **newest-wins** model with a hash-divergence guard:

- The plugin asks the RomM server for the saves in your active slot for the game and picks the newest (highest
  `updated_at`).
- If the server tracks your device as up-to-date and your local file matches the recorded baseline, there's nothing to
  do.
- If another device pushed a newer save and your local file is unchanged, the new server save is downloaded silently.
- If you played offline and your local file changed, your save is pushed back to the server.
- A conflict modal only appears if **both sides changed** since the last sync — the plugin won't silently overwrite
  either version.

This is the same model used by the official RomM clients (Argosy and Grout). It keeps cross-device save sync simple: one
timeline per slot, newest wins.

### Saves are per version

When a game has several [versions](managing-games.md#versions) — a `(USA)` dump and a `(Europe)` one, a `(Rev 1)`, and
so on — each version has its **own** saves, on RomM and on disk. Automatic sync only ever touches the **active version**
(the one currently bound to the shortcut). Switching versions never copies, moves, or deletes a save.

That means an inactive version's local saves simply wait: if you switch away from a downloaded version whose saves were
never uploaded, those saves stay on disk untouched and resume syncing the moment you switch back to that version. The
plugin warns you before you leave such a version and shows a reminder banner on the **Saves** tab so the saves aren't
forgotten (see [Managing Games → Versions](managing-games.md#versions)). Nothing is lost — the saves just don't sync
while their version is inactive.

## Important: Use Your Own RomM Account

Save files in RomM are tied to the authenticated user account. If multiple people share the same RomM account, their
saves will overwrite each other. Each person should have their own RomM account with their own credentials configured in
the plugin.

## Supported Systems

Save sync works on the **per-game save files RetroArch writes** for the systems RetroDECK supports. Standard cartridge
saves (SRAM) sync automatically; several systems also sync their own formats — RTC, EEPROM, and console-specific backup
RAM — so it is not limited to a single file type. Exactly what syncs varies by system: for the current per-system
breakdown of what carries across devices today and what's planned, see the
[save sync support matrix](save-sync-support-matrix.md).

Standalone emulator saves (PCSX2, DuckStation, Dolphin, PPSSPP, melonDS, etc.) are **not yet supported** and are planned
for a future update.

## Settings

Open **Save Sync** from the main QAM page to configure sync behavior.

<!-- Screenshot: Save Sync settings page showing auto-sync toggles -->

### Auto Sync

- **Sync before launch** (default: on) — runs sync when a game starts, whether via the Play button or the launch gate.
  If the server is unreachable, the game launches with whatever local save exists.
- **Sync after exit** (default: on) — runs sync after closing a game. A toast confirms what moved and which way — "Saves
  uploaded to RomM", "Saves downloaded from RomM", or "Saves synced with RomM (1 up, 2 down)" when a run went both ways;
  a sync that transferred nothing shows no toast. If the sync fails, the toast names the actual cause (see
  [Offline Behavior](#offline-behavior)) instead of a generic message.

### When saves conflict

The plugin uses a single, automatic resolution policy:

- **Newest server save in your slot wins** by default. If your local save hasn't changed since the last successful sync,
  the server version is downloaded silently.
- **Your local edits push automatically** when the server still considers your device up-to-date — for example, you
  played offline and no other device synced in the meantime.
- **A conflict modal appears only when both sides changed** since the last sync. You pick which version to keep.

Tap **Sync All Saves Now** to sync saves for all installed ROMs at once. This is useful for:

- Bulk backup before uninstalling ROMs
- Catching up after a period of offline play
- Verifying that all saves are in sync

This only covers games you've already set up save sync for (games whose save slot you've confirmed). Games you haven't
configured yet are left untouched, so a stale local save can't accidentally overwrite newer progress from another
device.

## Resolving Conflicts

When both your local save and the server save have changed since the last sync, a modal appears with both versions.

<!-- Screenshot: Sync conflict modal with local and server save details and three buttons -->

Each side shows:

- File size
- Modified / uploaded timestamp

Three actions:

- **Keep Local** — uploads your local save to the server, overwriting the server version.
- **Use Server** — downloads the server save and overwrites your local file.
- **Cancel** — dismisses the modal without changing anything. The conflict will reappear on the next sync as long as
  both sides still differ. If another device pushes an update in the meantime, the situation may resolve automatically
  (your unchanged local file gets the new version) and the modal won't reappear.

The modal blocks the Play action until you choose. If a post-exit sync detects a conflict you'll see a toast — the modal
opens the next time you tap Play, where it blocks launch until resolved. There is no longer a separate "pending
conflicts" list on the settings page.

## Core Switch Warning

When you switch the emulator core for a game (e.g., from mGBA to gpSP for GBA), the plugin detects the change and shows
a warning before launching. This is because some cores use incompatible save formats — launching with a different core
may overwrite your existing save with data the previous core can't read.

The warning shows which core you're switching from and to. You can:

- **Continue** — launch with the new core (your save may be overwritten)
- **Cancel** — go back and switch the core back before launching

A per-game core applies for any ROM filename — the plugin bakes the chosen core into the game's launch command rather
than relying on RetroDECK's gamelist lookup. See [Changing the Active Core](bios-management.md#changing-the-active-core)
for how per-game and per-platform cores work.

### Which cores are compatible?

Most cores for the same system produce compatible `.srm` saves because the save format is defined by the original
hardware, not the emulator. However, there are exceptions:

| System  | Cores                    | Compatible?   | Notes                                                                                                                                  |
| ------- | ------------------------ | ------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| SNES    | Snes9x, Supafaust, bsnes | Yes           | All dump raw cartridge SRAM identically                                                                                                |
| PSX     | Beetle PSX, PCSX ReARMed | Generally yes | Both use RetroArch's `.srm` memory card convention, but verify after switching — historical edge cases exist with memory card handling |
| N64     | Mupen64Plus, ParaLLEl    | Generally yes | ParaLLEl-N64 is based on Mupen64Plus-next and shares save logic; save-type auto-detection can occasionally mismatch between cores      |
| GBA     | mGBA, VBA-M              | Generally yes | Minor edge cases with save type detection                                                                                              |
| **GBA** | **mGBA ↔ gpSP**          | **No**        | **gpSP uses different save type detection; may corrupt saves**                                                                         |
| **NDS** | **MelonDS ↔ DeSmuME**    | **No**        | **Completely different save formats**                                                                                                  |

> **If in doubt, don't switch cores mid-playthrough.** Before switching, back up your `.srm` file — copy it somewhere
> safe (e.g. a `saves-backup/` folder). If the new core loads your save correctly, you can delete the backup. If it
> doesn't, you can restore the backup and switch the core back.

<!-- TODO: Expand this section with more tested core combinations as user reports come in -->

## Offline Behavior

If the RomM server is unreachable when a sync is attempted:

- **Before launch**: the game starts normally with your local save (a toast notification informs you).
- **After exit**: the upload is skipped. Your local save is untouched, and the next sync attempt produces the same
  outcome — typically pushing your changes once the server is reachable again.
- No save data is ever lost due to a failed sync.

"Server offline" is reported **only** for a genuine reachability failure (the server can't be reached or times out). A
sync that fails for another reason — for example an expired or revoked login token, or an SSL certificate problem —
shows that specific reason instead (such as "Authentication failed — check your username and password"), so a working
server is never mislabelled as offline. This applies to both surfaces: the warning shown before launch and the "after
exit" toast both name the actual cause rather than a generic "failed to sync" message. When more than one save file
fails in the same sync, the toast shows the first file's reason followed by a "(+N more)" count so the message stays
short. If you see an authentication message, re-enter your server URL and sign in again in the plugin settings.

After a failed sync the game-detail save panel reflects the honest state right away: a file whose upload failed shows a
yellow **Local changes** badge (not a green "synced"), and its "Last synced" line keeps the time of the last
_successful_ sync — a green checkmark appears only once a sync actually succeeds. A separate "Checked" line shows when
the plugin last attempted a sync, so a recent failed attempt and the last good sync are both visible.

## Playtime Tracking

The plugin tracks playtime per game. Session start and end times are recorded, and time the device spent suspended
(asleep) during a session is excluded, so a game left running overnight in sleep does not inflate its playtime. Playtime
is displayed on the game detail page next to the save sync status.

Each finished session is sent to RomM's built-in play-session store, so your `last_played` time shows correctly in the
RomM web UI. This is separate from save sync — playtime is recorded for **every** game on every exit, even when save
sync is off. If RomM is unreachable, the session is queued locally and uploaded automatically the next time you launch a
game or reconnect (nothing is lost).

The displayed playtime refreshes live: when you finish a session the PLAYTIME value updates on the same detail page
without needing to navigate away and back. Opening a game's detail page also reconciles playtime with RomM — if you
played the same game on another device, that device's play is folded in (the total only ever goes up, never backwards)
and shown as soon as the page loads. Cross-device reconcile needs a RomM server version that grants the play-session
read scope and a fresh sign-in; until then the displayed value is your local total. **After upgrading, sign in again to
enable cross-device playtime** — the plugin shows a banner in the QAM panel prompting this whenever your saved login
predates the play-session read scope. When RomM is unreachable the displayed value stays on your local total.

The **LAST PLAYED** date on the detail page comes from this same cross-device history, so it stays correct after you
move to a new device — where Steam would otherwise reset it and show every game as just played. Until the server has a
recorded session for a game (or while RomM is unreachable), the date falls back to Steam's own last-played value.

Steam also tracks playtime natively for non-Steam shortcuts, so you'll see playtime in the standard Steam UI as well.

> **Upgrading from an older version:** earlier releases stored playtime in a hidden RomM note named
> `romm-sync:playtime`. The plugin no longer uses these notes and starts fresh with the native store — your local total
> is preserved and keeps showing until the server re-accumulates. The old notes are left on the server, harmless; you
> can delete them yourself from RomM if you like.

## Save File Location

Save files are stored in your RetroDECK saves directory. The exact path is read from RetroDECK's configuration at
runtime — typically:

- **Internal SSD**: `~/retrodeck/saves/{system}/{rom_name}.srm`
- **SD card**: `/run/media/deck/Emulation/retrodeck/saves/{system}/{rom_name}.srm`

## RetroArch Save Sorting Requirement

Save sync expects save files to be organized as `{saves_dir}/{system}/{rom_name}.srm`. This matches the **RetroDECK
default** RetroArch configuration:

| RetroArch Setting                            | Required Value | RetroDECK Default |
| -------------------------------------------- | -------------- | ----------------- |
| Sort Saves into Folders by Content Directory | **ON**         | ON                |
| Sort Saves into Folders by Core Name         | **OFF**        | OFF               |

> **If you changed these settings in RetroArch, save sync will silently fail to find your save files.** No error is
> shown — saves simply won't sync.

### What happens with other configurations

RetroArch has four possible save sorting combinations. Only the first one is supported:

| Content Directory | Core Name | Save path                 | Supported? |
| ----------------- | --------- | ------------------------- | ---------- |
| ON                | OFF       | `saves/gba/game.srm`      | Yes        |
| OFF               | ON        | `saves/mGBA/game.srm`     | No         |
| ON                | ON        | `saves/gba/mGBA/game.srm` | No         |
| OFF               | OFF       | `saves/game.srm` (flat)   | No         |

If you use "Sort by Core Name" (alone or combined with Content Directory), your saves end up in a subfolder named after
the core (e.g., `mGBA`, `duckstation`, `Mesen`). The plugin does not search these subfolders.

### How to check your settings

In RetroArch: **Settings > Saving**. Look for the two "Sort Saves into Folders" toggles. On a fresh RetroDECK install,
they are already set correctly.

### If your saves are already in core-name folders

If you previously played with "Sort by Core Name" enabled, your existing `.srm` files are inside core-named subfolders.
You have two options:

1. **Move the files** back to the parent system directory (e.g., move `saves/gba/mGBA/*.srm` to `saves/gba/`)
2. **Change the RetroArch setting** back to Content Directory only (RetroArch will create new save files in the correct
   location on next launch — but your old saves stay in the core folder)

## RomM Version Compatibility

The plugin requires **RomM >= 4.9.0**. Pre-release builds whose numeric core is **above** that floor pass — for example
`4.9.1-beta` or `5.0.0-alpha.1`. Tags at the exact floor (`4.9.0-beta`, `4.9.0-alpha`) are rejected because they rank
below the `4.9.0` release. Servers below 4.9.0 are rejected at connection time with a full error page in both the QAM
panel and the game detail view. The plugin uses server-side device tracking, content hashing, save slots, and
`device_syncs` for conflict detection. The 4.9.0 minimum is set because that is the release that ships RomM's Device
Sync (`negotiate`) save-sync transport — the direction adopted in
[ADR-0016](../adr/0016-save-sync-hands-detection-to-romm-negotiate.md).

For technical details on how save sync works internally (three-way conflict detection, state schema, session detection),
see the [Save File Sync Architecture](../architecture/save-file-sync-architecture.md) technical reference.

---

**Previous:** [RetroDECK Path Migration](retrodeck-path-migration.md) | **Next:** [Troubleshooting](troubleshooting.md)
