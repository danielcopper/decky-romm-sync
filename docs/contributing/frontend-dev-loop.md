# Frontend dev loop

Iterate on the frontend from Desktop Mode on the Steam Deck: edit code next to a windowed Big Picture window and watch
the real plugin UI hot-reload on every save — no Desktop/Game Mode switching, no `plugin_loader` restarts.

## Why this works

Three pieces line up:

- **Desktop Big Picture is the real UI.** Desktop Steam's Big Picture window runs the same gamepadui React app as Game
  Mode — same routes, same game-detail pages — and Decky Loader injects into desktop Steam too. What you see in the
  windowed BPM is the plugin's actual UI, not an approximation. It is _not_ WYSIWYG about **vertical space**, though —
  by default the windowed BPM gives the QAM panel far more height than the Deck does. See
  [Display scale: the dev loop lies about height](#display-scale-the-dev-loop-lies-about-height).
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

Just want the window, not the loop? `mise run dev:bpm [display]` opens and places the same windowed Big Picture without
deploying or watching anything — useful for eyeballing a one-off `mise run dev` test build.

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

## Display scale: the dev loop lies about height

The windowed Big Picture renders the plugin's real UI, but **not at the Deck's real size**. Measured on-device:

| Configuration                                     | Big Picture window               | QuickAccess (QAM) panel |
| ------------------------------------------------- | -------------------------------- | ----------------------- |
| Game Mode (Deck internal panel, scale 1.5)        | 1280x800 physical, CSS 853x533   | CSS **854x454**         |
| Desktop BPM, 1280x800 window, unscaled (dpr 1)    | 1280x800 physical, CSS 1280x800  | CSS **854x720**         |
| Desktop BPM fullscreen on 1440p, Steam's auto 1.9 | 2560x1440 physical, CSS 1348x758 | CSS **855x679**         |
| Desktop BPM fullscreen on 1440p, **forced 1.5**   | 2560x1440 physical, CSS 1707x960 | CSS **854x880**         |

The dev loop hands the QAM panel far more vertical room than the device. CSS **width is ~854 everywhere**, so layout,
wrapping and truncation are faithful — only **height** lies. A panel that fits comfortably in the windowed BPM can
overflow in Game Mode.

The last row is the trap, and it is why this tool does two things rather than one. Every number above obeys:

```text
QAM CSS height = (Big Picture window PHYSICAL height / scale) - 80
```

- Game Mode: 800 / 1.5 − 80 = 453 → **measured 454**
- Fullscreen 1440p at Steam's automatic 1.9: 1440 / 1.9 − 80 = 678 → **measured 679**
- Fullscreen 1440p forced to 1.5: 1440 / 1.5 − 80 = **880** — nearly double the Deck's 454

(Steam lays a view out in `ceil(physical / scale)` CSS px, hence the ±1.) So **forcing the scale alone does not emulate
the Deck**: on a maximized or fullscreen Big Picture it hands you the Deck's `devicePixelRatio` with the wrong layout —
a lie that looks like a measurement. The window has to be the Deck panel's **1280x800 physical pixels** as well.

The 1.5 is **Steam's own per-display "GamepadUI display scale"** — not gamescope, and not a Chromium flag (Chromium is
pinned to `--force-device-scale-factor=1` by `steamclient.so`). Steam derives it from the display's resolution and
physical size and pushes it into each CEF browser view; it is the same value the user sees under **Settings → Display →
UI Scale**. Steam's default is **automatic**, which on a 1280x800 window comes out as 1.

### `mise run dev:ui-scale`

```bash
mise run dev:ui-scale          # deck (default): force 1.5 — exact Game Mode metrics
mise run dev:ui-scale 2.4      # a user on "Larger text" — worst case, least vertical room
mise run dev:ui-scale steam    # adopt whatever UI Scale you have set in Steam
mise run dev:ui-scale auto     # rescue: force automatic scaling back on, and exit
```

The task emulates the Deck with **both halves**:

1. **The window.** KWin scripting over DBus (the same `loadScript`/`run`/`unloadScript` route
   [`dev_open_bpm.sh`](#choosing-the-display) uses for placement) un-fullscreens the Big Picture window and sizes it so
   its **client area is exactly 1280x800**, on whatever monitor it already sits on. `frameGeometry` includes the window
   decoration, so the frame is corrected by the measured frame-vs-client delta until the client area lands exactly —
   nothing about the decoration is hardcoded.
2. **The scale.** The two undocumented calls Steam's own settings UI uses
   (`SteamClient.Window.SetGamepadUIAutoDisplayScale` / `SetGamepadUIManualDisplayScaleFactor`), driven through the CEF
   debugger on `localhost:8080`, so the views are **really re-laid out and repainted** — unlike CDP's
   `Emulation.setDeviceMetricsOverride`, which only fakes `devicePixelRatio` and leaves the window unpainted.

It then **verifies that the emulation actually happened** rather than claiming it, comparing the measured QAM against
what a Deck renders at that scale:

```text
$ mise run dev:ui-scale deck
Captured prior state: AUTOMATIC scaling at dpr 1.899999976158142 (source: Steam's live settings store)
Captured prior window: fullscreen 2560x1440 on DP-2
Sizing the Big Picture window to a 1280x800 client area (was fullscreen 2560x1440 on DP-2)...
  window now: windowed 1280x800 on DP-2 (frame 1282x829)
Forcing GamepadUI display scale 1.5 (auto scaling OFF)...
  scaling display: External: DP-2 27"|||Windowed

Measured (real, repainted — not a CDP emulation override):
  Big Picture  dpr 1.5  CSS 854x534
  QuickAccess  dpr 1.5  CSS 854x454
  => QAM is 854x454 CSS at dpr 1.5, the 854x454 a Deck renders at scale 1.5 — this is what the Deck renders.
```

If the QAM does **not** come out at the expected size (KWin unreachable, or Steam re-asserted the window size), the run
prints a loud `EMULATION FAILED` block with the measured-vs-expected numbers and the window's real pixel size, and says
that any layout judgement made on those numbers is invalid. It never reports Deck metrics it did not achieve.

#### Open the QAM once

Steam creates the QuickAccess view together with the Big Picture window but **lays it out only on the first QAM open of
that session**. Until then it measures **1x1 CSS**, and there is simply nothing to verify — so a run started against a
fresh Big Picture says so instead of crying wolf:

```text
Measured (real, repainted — not a CDP emulation override):
  Big Picture  dpr 1.5  CSS 854x534
  QuickAccess  dpr 1.5  CSS 1x1 — created, never rendered

QAM NOT VERIFIED YET — the emulation is applied, the QAM has simply never been opened.
  Big Picture: 1280x800 physical, scale forced to 1.5 (rendering at dpr 1.5).
  ...
  => OPEN THE QAM ONCE. This tool re-applies the scale to it the moment it renders, and
     then verifies it against the 854x454 a Deck shows at scale 1.5.
```

**Open the QAM once** and the run verifies it and prints the same verdict it would have printed up front — the success
line, or the loud `EMULATION FAILED` block if the window is not the Deck's size after all. A never-opened QAM is a
not-yet, not a failure; a QAM that renders at the wrong size still fails loudly.

That laziness has a sharper edge, and it is why the tool **holds in a polling loop rather than idling**: Steam's scale
push reaches only the views that are **rendered when it lands**. A QuickAccess view that renders _after_ the force can
therefore come up **unscaled — measured at dpr 1, CSS 854x720**, which is exactly the dev-loop lie this whole tool
exists to kill. So while it holds, the tool watches that view and re-issues the same two calls the moment it finds it
rendering at the wrong `devicePixelRatio`:

```text
QuickAccess view is at dpr 1.899999976158142  CSS 855x343 — not the forced 1.5.
Re-applying the scale (Steam's push reaches only the views that are rendered when it lands)...

Measured (real, repainted — not a CDP emulation override):
  Big Picture  dpr 1.5  CSS 854x534
  QuickAccess  dpr 1.5  CSS 854x454
  => QAM is 854x454 CSS at dpr 1.5, the 854x454 a Deck renders at scale 1.5 — this is what the Deck renders.
```

This also covers Steam re-materializing the popup later in the session. It is quiet by construction: it re-applies only
when a **rendered** view sits at the wrong dpr, and only once per distinct measurement, so a re-apply that does not take
is reported once rather than every couple of seconds.

The forcing modes **hold until Ctrl-C**. Useful factors:

| Factor  | What it reproduces              | Measured QAM   |
| ------- | ------------------------------- | -------------- |
| 1.5     | Deck internal panel (Game Mode) | 854x454        |
| 2.0–2.4 | A user who picked "Larger text" | 855x255 @ 2.4  |
| ~1.28   | Docked 1080p                    | 855x546 @ 1.28 |
| ~1.71   | Docked 1440p                    | —              |

#### What it restores on exit

The tool **captures both halves before it touches anything** — the scale you were on, and the Big Picture window's
geometry and fullscreen state — and puts back exactly that, on Ctrl-C, on SIGTERM, and on any error.

- **Scale.** If you were on automatic scaling, you get automatic scaling back. If you had deliberately set a **manual**
  UI Scale (an accessibility "Larger text" value, say), you get **that factor** back — it is never silently flipped to
  auto, which would destroy the setting. The restore is verified against Steam's live state and the rendered
  `devicePixelRatio`, and a restore that didn't land is reported as a warning rather than assumed.
- **Window.** It goes back to the geometry and fullscreen state it had — a fullscreen BPM is put back to fullscreen on
  the same monitor.

The scale is restored **before** the window, and the exit path **ignores further Ctrl-C/SIGTERM while it runs** so an
impatient second interrupt cannot truncate it half-way (the window half is the slow half — subprocesses and a journal
read). A stranded forced scale is the damaging outcome; a window left at 1280x800 is merely annoying.

`mise run dev:ui-scale auto` is different on purpose: it **unconditionally** forces automatic scaling back on. It is the
rescue path for when a previous run was hard-killed and the state it captured died with it — it has nothing to restore,
so it cannot honour a manual scale, and it does not touch the window (resize it by hand). If you are a manual-UI-Scale
user and ever have to use it, re-set your scale in Steam → Settings → Display afterwards.

!!! warning "A hard kill leaves the forced scale behind"

    Steam persists the factor to `~/.local/share/Steam/config/config.vdf` (`UI → display → Current → ScaleFactor`,
    flushed within seconds). Ctrl-C, SIGTERM and errors all restore — but a **hard kill (SIGKILL) skips the restore**,
    and the forced scale is what Steam keeps. If that happens, or if the UI ever comes up at the wrong size, run
    `mise run dev:ui-scale auto`.

    This is also why the prior state is read from Steam's **live settings store**
    (`window.settingsStore.settings`) rather than from `config.vdf`: the file's `AutoScaleFactor` is **not** re-flushed
    in-session (measured: it stayed `1` while a manual factor was applied and rendering), so trusting it would report a
    manual-scale user as "automatic" — and restore them to the wrong thing. `config.vdf` is only the fallback source.

!!! info "The UI Scale is per display — and the window mode is part of the display's identity"

    Steam files the scale under a display identity (`strDisplayName`), and `config.vdf` carries **one entry per
    identity**. The identity includes the window mode, which the tool prints on every run:

    ```text
    "External: eDP-1 7"|||Fullscreen-1280x800"    <- Game Mode / the internal panel
    "External: DP-2 27"|||Fullscreen-2560x1440"   <- a fullscreen BPM on an external 1440p monitor
    "External: DP-2 27"|||Windowed"               <- any windowed BPM on that monitor
    ```

    So the blast radius of a forced factor is narrower than "it bleeds into Game Mode": it lands in **the entry the
    window is currently under**. Because this tool always makes the window *windowed*, it writes a `…|||Windowed`
    entry — **not** Game Mode's `…|||Fullscreen-1280x800` one, even when the window sits on the Deck's internal panel.
    A leftover forced factor therefore mis-sizes a future *windowed desktop BPM*, and `dev:ui-scale auto` clears it.

The QAM is a popup view that Steam creates lazily and can re-materialize at will, and Steam's scale reaches only the
views that are **rendered when it is pushed** — a view that renders later comes up unscaled. That is why the tool holds
in a polling loop and re-applies the scale to a QuickAccess view it finds at the wrong `devicePixelRatio` (see
[Open the QAM once](#open-the-qam-once)).

### What this means for our panels

**The QAM's usable height is a runtime variable, not a constant.** The user can pick any UI Scale, and docking changes
it automatically — measured on this Deck the same QAM panel ranged from **255 CSS px** (scale 2.4) to **720 CSS px**
(unscaled desktop window), with Game Mode's 454 in between. So:

- **Never assume a pixel budget.** No fixed heights, no `maxHeight` tuned against one screenshot, nothing that only fits
  at 454 px.
- **Every row must be focusable**, so gamepad navigation can reach content that is scrolled out of view.
- **Let Steam's scroll container do its job** — the panel scrolls; content below the fold is reachable, not lost.

Validate a panel at 1.5 (the device) and at 2.4 (the worst case) before calling a layout done.

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
- **Big Picture comes up at the wrong size** — a `dev:ui-scale` run was hard-killed and left Steam on a forced scale (it
  persists in `config.vdf`, filed under the display identity that run printed — a `…|||Windowed` one). Run
  `mise run dev:ui-scale auto`.
- **Big Picture is left windowed at 1280x800** — same cause: a hard-killed `dev:ui-scale` run never restored the window.
  Re-fullscreen it by hand; nothing else is broken.
- **Do a final Game Mode pass before a release.** `dev:ui-scale deck` reproduces Game Mode's CSS metrics exactly (and
  says so out loud when it fails to), so layout and overflow can be judged from the desktop — but controller focus
  behavior and gamescope rendering are still only real in Game Mode.
