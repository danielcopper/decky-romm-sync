# Configuration

All settings are accessible from the plugin's QAM panel. Open the Quick Access Menu (**...** button) and navigate to the decky-romm-sync plugin.

## Connection Settings

The Connection Settings page manages your RomM server connection.

<!-- Screenshot: Connection Settings page -->

- **RomM URL** — the full URL of your RomM server, including port if needed (e.g. `http://192.168.1.100:8080`). Tap **Edit** to change it; the URL saves automatically.
- **RomM Account** — shows **Connected** once a token is stored, or **Not connected** otherwise. Tap **Connect** to open a one-time prompt for your RomM username and password. The plugin exchanges them for a RomM Client API Token and stores only the token; your password is discarded after the token is minted and never saved. The username and password are write-only — they are never pre-filled or shown back to you. If your account cannot create API tokens, the status reports that.
- **Allow Insecure SSL** — shown only for `https://` URLs; skips certificate verification for self-signed certs (LAN only).
- **Test Connection** — verifies the plugin can reach and authenticate with your RomM server using the stored token.

## SteamGridDB API Key

The plugin uses [SteamGridDB](https://www.steamgriddb.com/) to fetch additional artwork for your games — hero banners, logos, and wide grid images. RomM provides cover art, but SteamGridDB fills in the rest so your games look like first-class Steam titles.

To set this up:

1. Create a free account at [steamgriddb.com](https://www.steamgriddb.com/)
2. Go to your [API preferences](https://www.steamgriddb.com/profile/preferences/api) and copy your API key
3. In Connection Settings, paste it into the **API Key** field under "SteamGridDB"
4. Tap **Verify Key** to confirm it works

<!-- Screenshot: SteamGridDB API Key section with Edit and Verify buttons -->

Without an API key, games will still have cover art from RomM but the hero banner, logo overlay, and wide grid image will be missing.

## Steam Input Mode

Controls how Steam handles controller input for ROM shortcuts. Found under the **Controller** section in Connection Settings.

| Mode | Description |
| --- | --- |
| **Default** (Recommended) | Uses your global Steam Input settings. Works well with RetroDECK's default configuration. |
| **Force On** | Explicitly enables Steam Input wrapping. Normalizes the controller as standard XInput, which RetroArch autoconfig expects. |
| **Force Off** | Raw HID passthrough. Only for advanced users — may break RetroArch menu navigation. |

After changing the mode, tap **Apply to All Shortcuts** to update all existing ROM shortcuts.

<!-- Screenshot: Steam Input Mode dropdown with the three options -->

## Log Level

A dropdown in the **Advanced** section on the main page. Controls how much detail the plugin logs.

| Level | Description |
| --- | --- |
| **Error** | Only errors — minimal output |
| **Warn** (default) | Errors and warnings |
| **Info** | General operational messages |
| **Debug** | Verbose output for troubleshooting |

Leave this at **Warn** unless you're investigating an issue. Switch to **Debug** when reporting bugs or diagnosing problems.

## RetroArch Input Driver Fix

If the plugin detects that RetroArch is using the `x` input driver (which causes controller issues in menus on Wayland systems), a warning appears on the main page with a **Change to sdl2** button. This modifies your RetroArch config to use `sdl2` instead, which fixes controller navigation in RetroArch menus.

<!-- Screenshot: RetroArch input_driver warning with fix button -->

---

**Previous:** [Getting Started](getting-started.md) | **Next:** [Syncing Your Library](syncing-your-library.md)
