# Every gaming-mode launch is gated through one shared path via cancel-then-relaunch

## Status

Proposed. Part of [#1051](https://github.com/danielcopper/decky-romm-sync/issues/1051) — the launch-gate / save-conflict
/ offline hardening, expanded from "4b modal hardening" after a RomM-communication audit. Addresses the **gaming-mode**
portion of [#1144](https://github.com/danielcopper/decky-romm-sync/issues/1144) (the one genuine non-Play-button launch
path in gaming mode — `steam://rungameid` deep links — now funnels through the gate); **desktop-mode** coverage remains
open there and under [#831](https://github.com/danielcopper/decky-romm-sync/issues/831).

## Context

A RomM ROM can be launched two ways in Steam gaming mode, and today they behave inconsistently:

1. **The plugin Play button** (`CustomPlayButton.handlePlay`) runs a rich gate **before** the launch — connectivity,
   save-slot setup, core-change, then a pre-launch save sync with a conflict modal — and only then calls `RunGame`.
   Because it runs before the launch, it can block cleanly.
2. **A launch that bypasses our button.** In gaming mode the library grid, the home "recent" carousel, and Big Picture
   tiles all **navigate to the detail page** (our button) — they do not launch directly (verified against the
   deobfuscated SteamUI: a tile's `onActivate` does `navigate(/library/app/:appid)`, never `RunGame`). The genuine
   non-Play-button launch paths are `steam://rungameid` deep links (which issue a launch with no detail-page visit) and
   the resilience case where a Steam UI update breaks our React-tree patch and the native Play button renders. The only
   thing watching **beneath every surface** is a global hook (`launchInterceptor.ts` via
   `SteamClient.Apps.RegisterForGameActionStart`).

The global watcher has a structural defect: it fires **after** Steam has already begun the launch, so it can only
`CancelGameAction`, never pause. Today it `await`s a network `evaluate_launch` and then cancels on a block verdict — but
the await **races the un-pausable launch**: on a slow server the verdict arrives too late and the launch slips through
(the block/conflict becomes a no-op).

A **conflict** is a two-sided divergence — the local save diverged from its `last_sync_hash` baseline **and** the server
save also diverged. Conflicts are **never persisted**; they are recomputed fresh from a server `list_saves` + local
state on every check, and `get_cached_game_detail` hard-codes `conflicts: []`. So a fresh conflict **cannot** be known
locally or synchronously — it needs a server round-trip.

The audit (#1051) found the consequences: a launch with local drift while the server is unreachable is **silently
allowed** with no warning (the soft-warn path is dead because `get_save_status` swallows the offline error); the Play
button's `RunGame` re-triggers the watcher into a **second, independent** `list_saves` (the double-gate); the offline
decision rides a **page-open-stale** module flag; multi-file slots under-report conflicts; the live save-status refresh
loop has no production trigger.

Research (the SteamClient API surface, the Steam-UI launch state machine, and the MoonDeck + unifideck plugins)
established the hard constraint: **no Steam hook lets a plugin pause a launch, run async work, and then proceed.**
Steam's internal pause/resume (`bWaitingForUI` + `ContinueGameAction`) is Steam-driven (EULA, cloud-conflict, shader
cache) and not plugin-injectable. Patching `RunGame` at the source is worse — the `@decky/ui` patchers call the original
synchronously and don't await, `RunGame` returns `void`, and it would miss launch paths that don't route through that
symbol. **Cancel-then-relaunch** (`CancelGameAction` → async work → `RunGame`) is the only mechanism, and it is the
established community pattern (MoonDeck and unifideck both use it; unifideck shows modals from the watcher in
production). The watcher **can** show a real modal — `showModal` is imperative, already used from non-component code.
Decky loads plugins **only in gaming mode** — in the desktop Steam client the whole plugin (these hooks, the session
manager, sync, and playtime) is absent, so desktop-mode launches are entirely ungated. That gap is tracked separately
([#1144](https://github.com/danielcopper/decky-romm-sync/issues/1144) /
[#831](https://github.com/danielcopper/decky-romm-sync/issues/831)): the only universal chokepoint there is the
`bin/rom-launcher` wrapper, which can sync headlessly but cannot show a modal.

## Decision

**One shared gate, run for every gaming-mode launch via cancel-then-relaunch (the "full funnel").**

- The launch-gate logic is extracted into a **single standalone async path** used by both surfaces. The Play button runs
  it before `RunGame`; the watcher runs it after cancelling.
- The global watcher intercepts **every** `LaunchApp` of a RomM-owned shortcut that did **not** originate from our Play
  button, **cancels it immediately** (synchronous — this wins the race; the launch is stopped before any network call),
  runs the full shared gate, and on approval **relaunches** via `RunGame(gameId, "", -1, 100)`.
- A **one-shot skip-set** (module-level `Set<appId>`, checked-and-deleted at watcher entry) exempts exactly one launch:
  the watcher's own relaunch, and the Play button's launch (which already ran the gate). This kills the double-gate.
- Because the watcher cancels **first** and only then does async work, **there is no race** — the defect of awaiting
  against a live launch is gone by construction.

**What the gate does** (identical on both surfaces):

- **Not installed** → hard block (no "Start Anyway"); the ROM isn't on disk.
- **Migration pending** → block.
- **Reachability** is checked with a **fast fresh probe** at gate time — a single-attempt, short-timeout
  `heartbeat_once` (no retry/backoff, unlike the sync paths' retrying heartbeat), so an offline verdict returns in ~3s
  instead of up to ~90s on a remote timeout. The same fast probe drives the page's **offline badge** (it shows
  immediately on page open), replacing the page-open-stale connection flag.
- **Server reachable** → pre-launch save sync; a true conflict opens the conflict modal.
- **Server unreachable + local drift** → a **3-button modal** ("RomM unreachable — your local save has unsynced changes;
  playing may create a conflict you'll resolve later."): **Start Anyway** / **Retry connection** / **Cancel**. _Retry
  connection_ re-runs the gate with a fresh probe — if the server is back it proceeds down the online path (pre-launch
  sync → conflict modal / launch), otherwise the modal reappears. **Local drift is detected by content hash, never by
  file size or mtime.**
- **Server unreachable + no local drift** → allow silently (nothing to lose; reconciles on the next sync).

The watcher surfaces these as **real modals**, not toasts. The gate must **always fall back to launching on its own
error** — it never traps the user's game.

**Offline policy (made explicit and consistent across surfaces):** the offline model is recoverable, not lossy — a
launch into an undetected conflict is detected at the next sync, and conflict resolution quarantines the overwritten
local file to `.romm-backup`, so there is no silent data loss. "Unknown because the server is unreachable" is treated as
"can't verify" → the drift modal when there is local drift, silent allow otherwise.

**Desktop-mode launches** do not fire these hooks and are **not gated** — an accepted, documented limitation.

## Consequences

- **Consistent:** one behavior for all gaming-mode launch paths. The cold-launch-misses-a-fresh-conflict gap is closed;
  the gaming-mode portion of #1144 is addressed — deep-link launches now funnel through the gate. Desktop-mode launches
  remain ungated (Decky doesn't run there — #1144 / #831).
- **Cost — smaller than first feared (corrected after the SteamUI-code research):** in gaming mode, **normal launches go
  through our Play button**, which gates _before_ `RunGame` and skip-marks the launch, so the watcher skips it — **no
  flicker, no cancel-relaunch** on the common path. The cancel → gate → relaunch flicker only fires for the rare genuine
  bypass (`steam://rungameid` deep links, or a launch via the native button if our React-tree patch breaks). So the
  feared "instant-launch feel is gone on every launch" does **not** happen, and the **hybrid fast-path** is largely moot
  — there is no common direct-launch path to fast-path. Still worth an on-device confirm that a normal Play-button
  launch is flicker-free and that a deep link is caught.
- **One gate** to maintain instead of two divergent ones; the double-gate's redundant `list_saves` is gone.
- The skip-set adds a little state; its lifetime self-heals (check-and-delete at entry).
- **The Play-button funnel re-confirms `launch_options` just before `RunGame`** (in `dispatchLaunch`, after the verdict
  approves and on every launching branch): it pulls `get_rom_relaunch_options(rom_id)` and confirm-sets the shortcut's
  command, healing mid-session drift on the common launch path between plugin loads
  ([ADR-0009](0009-launcher-pure-exec-wrapper-baked-launch-options.md),
  [#1150](https://github.com/danielcopper/decky-romm-sync/issues/1150)). Best-effort — a failed or `None` re-confirm
  still launches. The **watcher path is not covered**: it decides synchronously before `CancelGameAction` and can't
  afford the round-trip, so it relies on the startup reconcile.
- **On-device verification required** (cannot be unit-tested): the cancel-relaunch flicker, modal focus after cancel,
  slow-server feel, and the suspend hooks.

## Alternatives considered

- **Patch `RunGame` for a true async pre-gate.** Rejected: the patchers don't await, `RunGame` returns `void`, and you'd
  still have to suppress-and-re-issue (= cancel-relaunch) on a fragile internal symbol that misses non-`RunGame` launch
  paths. The `BIsModOrShortcut` prototype-patch was already dropped for exactly this fragility.
- **Two-tier gate** (rich Play button + thin watcher safety-net). Rejected for consistency: the thin watcher misses
  fresh conflicts on cold direct launches and maintains divergent logic.
- **Hybrid funnel** (cancel + route only when a synchronous signal flags). **Deferred, not rejected** — start with the
  full funnel, add the fast-path if on-device feel demands it.
- **Dirty-flag approximation for drift** (a tracked "played-since-last-sync" boolean). Rejected in favor of exact
  content hashing: fixed-size saves change content without changing size, the maintainer wants the exact signal, hashing
  one save is single-digit ms, and the watcher cancels-first so it can hash async without racing.

## See also

[#1051](https://github.com/danielcopper/decky-romm-sync/issues/1051) (umbrella),
[#1144](https://github.com/danielcopper/decky-romm-sync/issues/1144) (non-plugin bypass, resolved here),
[#1146](https://github.com/danielcopper/decky-romm-sync/issues/1146). The suspend-time playtime fix and the
multi-file-slot conflict fix are bundled in the same work but are separate bugfixes, not part of this ADR's decision.
