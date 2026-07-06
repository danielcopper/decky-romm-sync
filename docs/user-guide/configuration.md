# Configuration

All settings are accessible from the plugin's QAM panel. Open the Quick Access Menu (**...** button) and navigate to the
decky-romm-sync plugin.

## Connection Settings

The Connection Settings page manages your RomM server connection.

<!-- Screenshot: Connection Settings page -->

- **RomM URL** — the full URL of your RomM server, including port if needed (e.g. `http://192.168.1.100:8080`). Tap
  **Edit** to change it; the URL saves automatically.
- **RomM Account** — shows **Signed in** once a token is stored, or **Not signed in** otherwise. Tap **Sign in** to open
  a one-time prompt. The prompt offers three sign-in methods, chosen from the **Sign-in method** dropdown:
  - **Username & password** (default) — enter your RomM username and password once. The plugin exchanges them for a RomM
    Client API Token and stores only the token; your password is discarded after the token is minted and never saved. If
    your account cannot create API tokens, the status reports that.
  - **API token** — paste a Client API Token you created in RomM's web UI. This is one of the two paths for accounts
    that have no password to mint from, such as **OIDC / SSO logins**. See
    [Sign in with an API token (OIDC)](#sign-in-with-an-api-token-oidc) below.
  - **Pairing code** — the other, recommended OIDC path: instead of copying the token, enter the short-lived 8-character
    code RomM shows when you **Pair** a token. The plugin fetches the token itself, so nothing is copied or pasted. See
    [Sign in with an API token (OIDC)](#sign-in-with-an-api-token-oidc) below.

  Both the credentials and the pasted token are write-only — they are never pre-filled or shown back to you.

  Once you are signed in, this button reads **Sign in again** and a **Sign out** button appears below it (see
  [Sign out](#sign-out)).
- **Allow Insecure SSL** — shown only for `https://` URLs; skips certificate verification for self-signed certs (LAN
  only).
- **Test Connection** — available once you are signed in; verifies the plugin can reach and authenticate with your RomM
  server using the stored token.

### Sign out

The **Sign out** button (shown only while signed in) forgets the stored token **on this device**: it clears the token,
its server-side id, its origin, and its provenance from the plugin's settings, but keeps the **server URL** and the SSL
setting so you do not have to re-enter them. It asks for confirmation first.

Signing out **never deletes or revokes the token in RomM** — the token stays valid on the server. A token the plugin
minted from your username and password can only be deleted during a same-server re-sign-in (the stored token
deliberately lacks the permission to delete itself), and a token you supplied (pasted or paired) is yours to manage. To
revoke a token for good, delete it in RomM's web UI under **Settings → API Tokens**.

If you just want to switch accounts or re-authenticate, prefer **Sign in again** over signing out and back in. For
username/password accounts, re-signing in on the **same** server revokes the token the plugin minted before — a path
that a sign-out then sign-in cannot take, since sign-out has already forgotten the old token's id. RomM caps the number
of Client API Tokens per user, so avoiding stranded minted tokens matters.

### Sign in with an API token (OIDC)

If you log in to RomM through an identity provider (OIDC / SSO), your RomM account has no password, so the plugin cannot
mint a token for you. Instead you create a Client API Token yourself in RomM's web UI and hand it to the plugin. There
are two ways to do that — **pairing code** (recommended) and **pasting the token** — and both grant the same scopes and
carry the same warnings (see [Required scopes](#required-scopes) below).

Start the same way for either method:

1. In RomM's web UI, open **Settings → API Tokens** (also called Client API Tokens) and create a new token.
2. Grant the scopes listed below — make sure the **write** scopes are included. Without them, downloads work but save
   upload, device sync, and playtime tracking fail with a permissions error.

#### Pairing code (recommended)

Pairing hands the device the token over a short-lived one-time code, so you never copy or type the token itself.

1. Create the token with the scopes above (steps 1–2).
2. On that token in RomM's web UI, click **Pair**. RomM shows an 8-character pairing code, valid for **60 seconds**.
3. In the plugin's Connection Settings, tap **Sign in**, switch the **Sign-in method** dropdown to **Pairing code**,
   enter the code, and confirm — within the 60-second window. The plugin exchanges the code for the token itself.

> **Pairing rotates the token's secret.** Exchanging a pairing code hands the device a **freshly rotated** secret for
> that token — any raw token value you copied earlier stops working. Use **one token per device** so pairing a new
> device never invalidates another.

#### Paste the token

1. Create the token with the scopes above (steps 1–2).
2. Copy the token value (RomM shows it only once).
3. In the plugin's Connection Settings, tap **Sign in**, switch the **Sign-in method** dropdown to **API token**, paste
   the token, and confirm.

Both methods validate the token at sign-in with an authenticated probe against your RomM profile: a wrong, revoked, or
expired credential is rejected there with an actionable message. Sign-in only confirms that the token **authenticates**,
though — the plugin cannot verify the token's granted scopes, so double-check that you granted the write scopes
(`assets.write`, `devices.write`, `roms.user.write`) when creating it. A missing write scope is not caught at sign-in;
it surfaces later as a permissions error on the affected action (save upload, device sync, or playtime).

#### Required scopes

| Scope              | Access    | What it is used for                                                                          |
| ------------------ | --------- | -------------------------------------------------------------------------------------------- |
| `me.read`          | read      | Your RomM profile — validates the token at sign-in and reads your RetroAchievements username |
| `platforms.read`   | read      | Listing your platforms                                                                       |
| `roms.read`        | read      | Listing and reading ROM metadata (the library)                                               |
| `roms.user.read`   | read      | Your per-user ROM data — native play-session history                                         |
| `collections.read` | read      | Reading your collections (user, smart, franchise)                                            |
| `firmware.read`    | read      | Listing and downloading BIOS / firmware                                                      |
| `assets.read`      | read      | Downloading save files                                                                       |
| `devices.read`     | read      | Reading your registered devices (device sync)                                                |
| `assets.write`     | **write** | Uploading save files                                                                         |
| `devices.write`    | **write** | Registering this device and opening device-sync sessions                                     |
| `roms.user.write`  | **write** | Writing your per-user ROM data — playtime ingest                                             |

All eleven scopes are within RomM's **Viewer** role, so a token created by any account (including OIDC accounts) can
carry them. `me.write` is deliberately **not** requested — a pasted token cannot mint or delete tokens.

> **The plugin never deletes a pasted token.** Signing out of or back into the plugin, or switching servers, leaves your
> token untouched on the RomM server — you manage its lifecycle in RomM's web UI. (This differs from the
> username/password method, where the plugin revokes the token it minted when you re-sign-in on the same server.)
>
> Note the reverse case too: if you previously signed in with your username and password and then switch to a pasted
> token on the **same** server, the token the plugin minted earlier is left behind on RomM — it can only revoke that
> during a same-server password re-sign-in (it has no password once you switch to a token). Revoke the old token
> manually in RomM's web UI if you no longer want it.

## SteamGridDB API Key

The plugin uses [SteamGridDB](https://www.steamgriddb.com/) to fetch additional artwork for your games — hero banners,
logos, and wide grid images. RomM provides cover art, but SteamGridDB fills in the rest so your games look like
first-class Steam titles.

To set this up:

1. Create a free account at [steamgriddb.com](https://www.steamgriddb.com/)
2. Go to your [API preferences](https://www.steamgriddb.com/profile/preferences/api) and copy your API key
3. In Connection Settings, paste it into the **API Key** field under "SteamGridDB"
4. Tap **Verify Key** to confirm it works

<!-- Screenshot: SteamGridDB API Key section with Edit and Verify buttons -->

Without an API key, games will still have cover art from RomM but the hero banner, logo overlay, and wide grid image
will be missing.

## Steam Input Mode

Controls how Steam handles controller input for ROM shortcuts. Found under the **Controller** section in Connection
Settings.

| Mode                      | Description                                                                                                                |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Default** (Recommended) | Uses your global Steam Input settings. Works well with RetroDECK's default configuration.                                  |
| **Force On**              | Explicitly enables Steam Input wrapping. Normalizes the controller as standard XInput, which RetroArch autoconfig expects. |
| **Force Off**             | Raw HID passthrough. Only for advanced users — may break RetroArch menu navigation.                                        |

After changing the mode, tap **Apply to All Shortcuts** to update all existing ROM shortcuts.

<!-- Screenshot: Steam Input Mode dropdown with the three options -->

## Preferred region

A dropdown in the **Library** section of the settings page. When a game exists in your RomM library as several regional
dumps (versions), this decides which region the plugin prefers when it picks the version to bind and the name it gives
the Steam shortcut.

| Option                                   | Effect                                                                                              |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------- |
| **Default (World > USA > Europe)**       | Prefer World, then USA, then Europe, then Japan, then any other region alphabetically (fixed order) |
| a specific region (World, USA, Japan, …) | Put that region at the top of the order; everything else keeps the default order behind it          |

**"Default" is a fixed order, not auto-detection.** It always prefers `World > USA > Europe > Japan` (then other regions
alphabetically, then dumps with no region); the plugin never looks at your language or system region.

**The dropdown's options** are the fixed anchors — Default, World, USA, Europe, Japan — followed by every other region
actually present in the games you have already synced (read from the local database, sorted alphabetically; no server
request). If your synced library has no other regions, only the anchors are shown.

**When you change it, a short modal explains the effect** and asks you to confirm. The choice is saved immediately, but
it takes effect on the **next sync** and only affects games synced from then on. It **never** switches the version or
renames an existing shortcut — shortcut names are fixed when the shortcut is first created, to protect its artwork,
collections and playtime. Already-synced games keep their bound version and name; run a sync to apply the new preference
to new games. See [Multiple versions of a game](syncing-your-library.md#multiple-versions-of-a-game) for the full
picture.

## Log Level

A dropdown in the **Advanced** section on the main page. Controls how much detail the plugin logs.

| Level              | Description                        |
| ------------------ | ---------------------------------- |
| **Error**          | Only errors — minimal output       |
| **Warn** (default) | Errors and warnings                |
| **Info**           | General operational messages       |
| **Debug**          | Verbose output for troubleshooting |

Leave this at **Warn** unless you're investigating an issue. Switch to **Debug** when reporting bugs or diagnosing
problems.

## RetroArch Input Driver Fix

If the plugin detects that RetroArch is using the `x` input driver (which causes controller issues in menus on Wayland
systems), a warning appears on the main page with a **Change to sdl2** button. This modifies your RetroArch config to
use `sdl2` instead, which fixes controller navigation in RetroArch menus.

<!-- Screenshot: RetroArch input_driver warning with fix button -->

---

**Previous:** [Getting Started](getting-started.md) | **Next:** [Syncing Your Library](syncing-your-library.md)
