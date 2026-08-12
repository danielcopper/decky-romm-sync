# Getting Started

## What is Tender?

Tender is a [Decky Loader](https://decky.xyz/) plugin that connects your self-hosted
[RomM](https://github.com/rommapp/romm) ROM library to Steam. Every game in your RomM library appears as a Non-Steam
shortcut in the Steam Library, complete with cover art, metadata, and collections. Games launch through
[RetroDECK](https://retrodeck.net/).

## Prerequisites

Before installing the plugin, you need:

1. **A RomM server** — a running RomM instance with your ROM library. You'll need the server URL plus a username and
   password to connect the first time. The plugin exchanges those credentials for a RomM Client API Token and stores
   only the token — your password is never saved. Each user should have their own RomM account (see
   [Save Sync](save-sync.md) for why this matters).

2. **RetroDECK** — installed on your Steam Deck or Linux PC. RetroDECK handles the actual emulation. The plugin creates
   shortcuts that launch games through RetroDECK.

3. **Decky Loader** — the plugin framework. Install it from [decky.xyz](https://decky.xyz/) if you haven't already.
   Decky renders inside Steam's gamepad UI, and the plugin works wherever that UI runs: Gaming Mode on the Steam Deck,
   or Big Picture Mode on a Linux PC/HTPC — a dedicated Game Mode session is not required.

4. **A personal RomM account** — save sync ties saves to the authenticated user. Use your own account, not a shared one.

## Installation

### From Decky's "Install Plugin From URL"

1. Open the Quick Access Menu (QAM) in Gaming Mode by pressing the **...** button
2. Go to the Decky Loader tab (the plug icon) and open settings (gear icon)
3. Under **General → Other**, enable **Developer Mode** — a new **Developer** tab appears in the sidebar
4. Open the **Developer** tab and select **Install Plugin From URL**
5. Enter the direct URL to the release zip

   This one always points at the newest release, so it needs no version number:

   ```text
   https://github.com/danielcopper/romm-tender/releases/latest/download/tender.zip
   ```

   To pin a specific version instead, name its tag:

   ```text
   https://github.com/danielcopper/romm-tender/releases/download/tender-v{VERSION}/tender.zip
   ```

   Releases published before the rename use the older `decky-romm-sync-v{VERSION}/decky-romm-sync.zip` form; their links
   keep working unchanged.

6. Decky downloads and installs the plugin automatically — no restart needed

**Tip:** You can also open the [releases page](https://github.com/danielcopper/romm-tender/releases) in Steam's built-in
browser (Gaming Mode → long-press the Steam button → Web Browser), long-press the zip download link, and copy the URL
from there.

Any direct URL to the zip file works (GitHub releases, a self-hosted mirror, etc.) as long as it points to a valid
`.zip` containing the plugin.

### Manual installation (alternative)

1. Download `tender.zip` from the [releases page](https://github.com/danielcopper/romm-tender/releases)
2. Extract the zip to `~/homebrew/plugins/` on your device (via SSH, file manager, or USB)
3. Restart Decky Loader — either reboot, or run `sudo systemctl restart plugin_loader` via SSH
4. The plugin appears in your QAM under the Decky tab

## First-Time Setup

After installation, you need to connect the plugin to your RomM server:

1. Open the QAM and find **Tender**
2. Tap **Connection Settings**
3. Enter your RomM server URL (e.g. `http://192.168.1.100:8080`) — this saves automatically
4. Tap **Sign in**, enter your RomM username and password once, and confirm
5. The **RomM Account** row shows **Signed in** on success. The plugin's main QAM panel has a **Connection** row that
   shows the live connection status whenever you open it — there is no separate test button

On a controller you can confirm a text field with the on-screen keyboard's **Enter** key (or the **R2** shortcut)
instead of navigating to the button — for example save-slot names and the artwork search. In the sign-in dialog, Enter
moves from the username to the password field and confirms only once every required field is filled; an incomplete form
can't be submitted, and **Cancel** leaves your existing sign-in untouched.

The plugin mints a RomM Client API Token from the credentials you enter and discards the password — it is never stored.
The same applies if the plugin auto-migrates an older install that still had a saved password: the password is discarded
as soon as a token is minted. If your RomM account is not allowed to create API tokens, the sign-in step reports that
and you'll need an account with token permissions.

<!-- Screenshot: Connection Settings page with URL field, Sign in button, and token status -->

Once connected, you're ready to sync your library. See [Configuration](configuration.md) for additional settings, or
jump straight to [Syncing Your Library](syncing-your-library.md).

---

**Next:** [Configuration](configuration.md)
