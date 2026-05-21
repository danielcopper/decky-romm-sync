# Steam Remote Play and Cross-Device Shortcuts

## What Users See

When a user runs the plugin on one machine (e.g. Steam Deck) and has another machine logged into the same Steam account on the same LAN (e.g. Bazzite HTPC), they may notice:

1. **Shortcuts appearing on other devices** — all non-Steam shortcuts from the source machine show up on the remote client
2. **Missing artwork** — remote shortcuts appear without grid, hero, or logo images
3. **Disappearing shortcuts** — when the source machine goes offline (sleep, shutdown, network loss), the shortcuts vanish from the remote client
4. **No "Stream" vs "Play" confusion** — sometimes remote shortcuts show only a "Stream" button instead of the expected "Play"

## Root Cause: Steam In-Home Streaming Discovery Protocol

This is **NOT** Steam Cloud syncing `shortcuts.vdf`. The `shortcuts.vdf` file is strictly local and never leaves the machine via Steam Cloud. Collections, on the other hand, do sync via Steam Cloud.

What users observe is Steam's **In-Home Streaming / Remote Play discovery protocol** — a LAN-based mechanism for advertising available games to other Steam clients.

## Protocol Details

### Discovery Phase (UDP 27036)

Steam clients periodically broadcast UDP packets on port 27036 to discover other Steam instances on the local network. When two clients running under the same Steam account find each other, they negotiate a TCP control connection.

The discovery broadcast uses `CMsgRemoteClientBroadcastStatus` protobuf messages containing:

- Machine name, OS type, Steam universe
- Available apps and their streaming capabilities
- User session information

### Control Connection (TCP)

After discovery, the two clients establish a persistent TCP connection. Over this connection, the source client sends `CMsgRemoteClientAppStatus` messages that contain a `ShortcutInfo` submessage for each non-Steam shortcut.

### ShortcutInfo — What Gets Transmitted

The `ShortcutInfo` protobuf message (from `steammessages_remoteclient.proto`) includes:

| Field            | Type            | Description                                    |
| ---------------- | --------------- | ---------------------------------------------- |
| `name`           | string          | Display name of the shortcut                   |
| `icon`           | string          | Icon data hash (not the full image file)       |
| `categories`     | repeated string | Steam collection names the shortcut belongs to |
| `exepath`        | string          | Path to the executable on the source machine   |
| `launch_options` | string          | Launch options string                          |

### What Is NOT Transmitted

- **Artwork files** — Grid (portrait), hero, logo, and wide grid images are stored as local files in `userdata/<id>/config/grid/`. These paths are not shared over the protocol. This is why remote shortcuts appear with placeholder/missing artwork.
- **Detailed metadata** — No description, genres, developer info, or other store-patched metadata transfers.
- **Install state** — The remote client has no way to know if the game's ROM is actually downloaded on the source machine.

### Ephemeral Nature

The remote shortcuts exist only as long as the TCP control connection is alive. They are **not persisted** to the remote machine's `shortcuts.vdf`. When the source machine:

- Goes to sleep
- Shuts down
- Disconnects from the network
- Closes Steam

...the shortcuts immediately disappear from the remote client's library.

### No Per-Shortcut Opt-Out

Steam advertises **all** non-Steam shortcuts to remote clients. There is no:

- Per-shortcut toggle to exclude from streaming
- API call to hide specific shortcuts from advertisement
- Steam setting to selectively disable shortcut sharing

The only options are:

- Disable Remote Play entirely (Settings > Remote Play > uncheck "Enable Remote Play")
- Disable it per-device in Remote Play settings

## Detection APIs

The plugin can distinguish local shortcuts from remote phantom shortcuts using these Steam frontend APIs:

### `collectionStore.localGamesCollection`

This collection contains only shortcuts created on the local machine. Remote streaming phantoms are excluded. Use this to filter DangerZone removal operations to local-only shortcuts.

```typescript
const localApps = collectionStore.localGamesCollection.apps;
// Only contains locally-created shortcuts
```

### `SteamAppOverview.per_client_data`

Each `SteamAppOverview` object has a `per_client_data` array containing entries for each client that has advertised the app:

```typescript
interface PerClientData {
  clientid: string; // Unique client identifier
  client_name: string; // Machine name (e.g. "steamdeck", "htpc")
  display_status: number; // App status on that client
  // ... other fields
}
```

A shortcut from a remote machine will have a `per_client_data` entry with a different `clientid` than the local machine.

### `SteamAppOverview.local_per_client_data`

Returns only the local machine's `per_client_data` entry. If this is `undefined` or empty for a shortcut, it likely originates from a remote client.

## What the Plugin Does

### Machine-Scoped Collections

Collections are named with the machine hostname, e.g. `"RomM: Nintendo 64 (steamdeck)"`. Since Steam Cloud syncs collections across devices, this prevents collection name collisions between machines. Each machine's collections are clearly attributed.

### What We Tried and Removed

We investigated two approaches for programmatic detection of remote phantoms:

1. **`collectionStore.localGamesCollection`** — Was supposed to contain only locally-created shortcuts, allowing us to filter out remote phantoms in DangerZone and label them in the game detail panel. **In practice, this was unreliable** — it incorrectly marked local shortcuts as remote.

2. **`SteamAppOverview.per_client_data`** — Was supposed to provide per-device metadata for each shortcut, allowing us to show which device a game came from. **This field is never populated for Non-Steam Shortcuts** — it only works for real Steam store games.

Both approaches were removed. **Steam's native UI already handles this well**: remote phantom shortcuts show a "Stream" button instead of "Play", which clearly communicates that the game is available from another device.

### DangerZone and Remote Phantoms

DangerZone removal operations ("Remove All RomM Shortcuts", "Remove by Platform") use the **backend shortcut registry**, which is local-only. These operations can only affect shortcuts created by the plugin on the current machine. Remote streaming phantoms cannot be removed this way — and even if they could, they would reappear immediately since they're ephemeral entries from the live TCP connection.

The "Remove Non-Steam Games" section shows all non-Steam apps visible to the current client, including remote phantoms. Removing a phantom has no lasting effect — it reappears as long as the source device is online.

## What Can't Be Controlled

- **No per-shortcut streaming opt-out** — all non-Steam shortcuts are advertised; there is no API to exclude specific ones
- **No artwork transfer** — artwork must be set locally on each machine; the streaming protocol doesn't carry it
- **No persistence** — remote shortcuts are ephemeral and cannot be "saved" on the remote client

## Known Steam Bugs

### Issue #8791: Name Collision

When both the source and remote machine have a non-Steam shortcut with the same name, Steam may display incorrect metadata (mixing fields from both entries). This can occur when both machines run the plugin and sync the same RomM library.

### Issue #12315: "Stream" Button Regression

A regression in certain Steam client versions causes remote non-Steam shortcuts to only show a "Stream" button instead of allowing direct local launch. This affects users who have the same game installed locally but also see it advertised from a remote client.

## References

- [SteamDatabase/Protobufs](https://github.com/SteamDatabase/Protobufs) — Decompiled Steam protobuf definitions including `steammessages_remoteclient.proto`
- [Coding Range: Steam In-Home Streaming Discovery Protocol](https://codingrange.com/coding/2015/09/05/steam-in-home-streaming-discovery-protocol.html) — Detailed protocol analysis
- [IHSlib](https://github.com/nicfit/ihslib) — Python library implementing the Steam In-Home Streaming protocol
- [steam-discover](https://github.com/nicfit/steam-discover) — CLI tool for discovering Steam clients on the network
