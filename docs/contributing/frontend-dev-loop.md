# Frontend dev loop

Iterate on the frontend from Desktop Mode on the Steam Deck: edit code next to a windowed Big Picture window and watch
the real plugin UI hot-reload on every save — no Desktop/Game Mode switching, no `plugin_loader` restarts.

## Why this works

Three pieces line up:

- **Desktop Big Picture is the real UI.** Desktop Steam's Big Picture window runs the same gamepadui React app as Game
  Mode — same routes, same game-detail pages — and Decky Loader injects into desktop Steam too. What you see in the
  windowed BPM is the plugin's actual UI, not an approximation.
- **decky-loader ships a hot-reload watcher.** The loader watches `~/homebrew/plugins` and reacts to create/modify
  events on exactly two files per plugin: `dist/index.js` and `main.py`. On a match it reloads the plugin in place —
  backend subprocess restart plus a cache-busted frontend re-import (the plugin unmounts cleanly first, so router
  patches are removed and re-applied).
- **The watcher is gated on a `debug` flag.** Hot reload only applies to plugins carrying `"debug"` in the `flags` array
  of `plugin.json`. `mise run deploy` injects the flag into the **deployed** `plugin.json` only — it is never committed
  to the repo copy, because the Decky plugin store rejects plugins that ship it.

## One-time setup

```bash
mise run dev:setup
```

This changes the system so the daily loop can run without `sudo`:

- Writes the systemd drop-in `/etc/systemd/system/plugin_loader.service.d/10-dev-loop.conf` with
  `Environment=CHOWN_PLUGIN_PATH=0`. This disables Decky's tamper guard, which otherwise re-owns the plugin dir and
  `plugin.json` back to root on every plugin load — with the guard off, the deck user can write straight into
  `~/homebrew/plugins/decky-romm-sync`. Drop-ins survive Decky self-updates (those rewrite only the unit file).
- Runs `systemctl daemon-reload` and restarts `plugin_loader`.
- Chowns an existing plugin dir back to the deck user.
- Warns (without failing) if `~/.steam/steam/.cef-enable-remote-debugging` is missing — see [DevTools](#devtools).

The task is idempotent — safe to re-run at any time. The `debug` flag itself is not part of the setup: it is injected at
deploy time by `mise run deploy`, on every deploy.

## Daily loop

```bash
mise run dev:watch          # BPM window on the internal display (default)
mise run dev:watch dp2      # BPM window on an external monitor
```

What happens:

1. A full deploy (`mise run deploy`): frontend build plus rsync of everything into the plugin dir. The `main.py` copy
   inside it is itself a modify event, so the plugin hot-reloads with the fresh backend and frontend right away.
2. A windowed Big Picture opens on the desktop (a no-op if one is already open; if Steam isn't running, it starts
   straight into BPM) and its window is placed on the chosen display.
3. Rollup stays in watch mode. On every save it rebuilds `dist/index.js` and copies it into the deployed `dist/`;
   decky-loader hot-reloads the plugin about 1–2 seconds later.

The reload is bundle-level, not component-level hot module replacement: the plugin is unmounted and re-imported, so
component state resets and whatever is currently on screen keeps showing the **old** render until it is mounted again.
Concretely: after a save, leave the plugin's QAM panel and re-enter it (back out of _RomM Sync_, open it again) — or
re-navigate to a patched game-detail page — to see the change. Keyboard shortcuts in the BPM window: **Ctrl+2** opens
the Quick Access Menu, **Ctrl+1** the main menu.

### Choosing the display

The optional argument is matched against the **real outputs** of the machine — output naming varies between Decks and
docks (external outputs may be `DP-2`/`DP-3` rather than `DP-1`/`DP-2`), so nothing is hardcoded. List the selectable
targets with `scripts/dev_open_bpm.sh --list`: it prints a lowercase short form per connected **and enabled** output
(e.g. `edp1`, `dp2`, `dp3`), plus the `internal` alias while the built-in panel is enabled. Disabled outputs are neither
listed nor resolvable — KWin can't place a window on them. The raw `kscreen-doctor -o` names (e.g. `DP-2`) are accepted
as well — matching is case- and dash-insensitive, so `dp2`, `DP2` and `DP-2` all mean `DP-2`. The default `internal`
resolves to the built-in panel (`eDP-*`); a target that matches no enabled output is a hard error before anything opens.
On a docked Deck whose internal panel is disabled or disconnected, the default prints a warning and the BPM window
simply opens wherever the window manager puts it.

Placement itself is done by a short-lived KWin script loaded over DBus (`scripts/dev_open_bpm.sh`), which moves the Big
Picture window to the target output and unloads itself again — if KWin scripting is unavailable, the loop still works
and only the placement is skipped. The BPM window stays a normal desktop window: it can always be dragged elsewhere.

With [mise shell completions](https://mise.jdx.dev/installing-mise.html#shells) enabled (requires the `usage` CLI, e.g.
`eval "$(mise completion bash)"` in your shell rc), the display argument tab-completes with those targets.

## Backend changes

```bash
mise run dev:push-backend
```

`dev:watch` only watches `src/`, so **frontend** edits reload automatically but **backend** (Python) edits do not — push
them on demand with this task. It rsyncs `py_modules/` and `main.py` into the deployed plugin during a `dev:watch`
session. The watcher matches only `dist/index.js` and `main.py`, so a `py_modules/`-only push never triggers a reload —
the task therefore copies `main.py` **last**, and that copy is the modify event that reloads the plugin. Every reload is
a full one regardless of which file changed: decky-loader restarts the backend subprocess **and** re-imports the
frontend bundle. For `bin/` or `defaults/` changes, run the full `mise run deploy` instead.

## DevTools

With `~/.steam/steam/.cef-enable-remote-debugging` present, Steam exposes the CEF DevTools protocol on
<http://localhost:8080>:

- The **SharedJSContext** target is where all plugin JS runs — console output and JS debugging live here.
- The **Steam Big Picture Mode** target is the rendered UI — element inspection and live CSS editing.

Decky's developer setting **Allow Remote CEF Debugging** forwards the same protocol to port 8081 on the LAN, for
DevTools from a second PC. Reload activity from the loader side is visible with:

```bash
journalctl -u plugin_loader -f
```

## Troubleshooting and caveats

- **Decky UI gone after leaving and re-entering BPM** — Decky's UI survives only the _first_ Big Picture entry per Steam
  process; closing the BPM window and reopening it loses the Decky QAM until Steam restarts. Recover with
  `mise run dev:bpm-reset` (shuts Steam down, waits for it to exit, reopens windowed BPM). It takes the same optional
  display argument as `dev:watch`, e.g. `mise run dev:bpm-reset dp2`.
- **Watcher arms ~10 seconds after `plugin_loader` starts.** Edits saved inside that window don't reload — save again
  once it's up.
- **Renames and moves are ignored by the watcher** — it reacts only to create/modify events. That's why the loop copies
  with `cp` (an in-place modify); `mv`, or an rsync that writes a temp file and renames it into place, would silently
  never trigger a reload for `dist/index.js` / `main.py`.
- **Big Picture opened on the wrong monitor** — placement matches the window by its title once it appears. Check what
  the window manager actually saw with `journalctl --user -b | grep decky-bpm`: the log lists every window's caption and
  output, and whether the move fired. The window stays a normal desktop window, so you can always drag it over yourself.
- **A "screen sharing" portal dialog pops up when Big Picture opens** — that's Steam's own desktop capture (Game
  Recording / Remote Play) asking through the xdg-desktop-portal, because there is no gamescope to capture in desktop
  mode. It is unrelated to this tooling. **Turn Steam's Game Recording off** (Steam → Settings → Game Recording) — you
  don't want Steam capturing your desktop mid-development anyway, and this removes the dialog for good. Ticking the
  portal's _"enable restore"_ box instead only hides the dialog: it pins whichever source you picked, so if you pick a
  single monitor the capture stays on it even after `dev:watch <other-display>` moves Big Picture elsewhere.
- **The QAM Performance tab is non-functional in desktop BPM** (it needs gamescope). Irrelevant for this plugin's UI.
- **Desktop-BPM injection is best-effort** on Decky's side — re-verify the loop still works after Decky or Steam
  updates.
- **Do a final Game Mode pass before a release.** The windowed BPM covers UI iteration, but controller focus behavior
  and gamescope rendering are only real in Game Mode.
