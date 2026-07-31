---
name: appid-launchoptions-reliability
description: Hardware-validated (#827, 2026-06-03) — SetAppLaunchOptions on an EXISTING Steam shortcut IS reliable (in-session + across restart + 30-churn). appId=CRC32(exe+appname), so launch_options/startDir mutations are appId-safe, exe/name are appId-destructive. Narrows the old "shortcut property re-sync unreliable" framing in CLAUDE.md + steam-non-steam-shortcuts.md. Unblocks #785.
type: project
---

# Updating existing Steam shortcuts — what's actually reliable

Hardware-validated on a Steam Deck on **2026-06-03** (#827, now closed), run from the **desktop-mode** browser CEF
inspector (`localhost:8080` → SharedJSContext — same `SteamClient.Apps` API / `shortcuts.vdf` as Game Mode):

- **`SetAppLaunchOptions` on an existing shortcut is reliable.** In-session change reads back the new value via
  `RegisterForAppDetails` in ~150 ms; the change **persists across a full Steam restart** (rules out in-memory-only);
  30× add → set → read-back → remove churn cycles ran with zero failures.
- **appId = CRC32(exe + appname).** Therefore `launch_options` and `startDir` mutations are **appId-safe** (artwork /
  playtime / collections preserved); changing **exe** or **name** is **appId-destructive** (orphans all of those).
  `SetShortcutExe` on an existing shortcut _does_ read back the new exe in-session, but that doesn't make it safe — the
  appId desyncs. Never `SetShortcutExe` on a live RomM shortcut; the launcher exe path must stay constant.

**Why:** the old framing in CLAUDE.md ("Shortcut property re-sync … Full delete + recreate required") and
`docs/architecture/steam-non-steam-shortcuts.md` ("Set* … may not take effect reliably") was **documented but never
empirically validated** — and it's overstated for `launch_options` specifically. The real, narrower hazard is a
Steam-client **corruption state triggered by removal churn** at higher/random volume, after which _all_ property sets
silently fail until `SteamClient.User.StartRestart(true)`. That's a removal-volume lifecycle hazard, not a property of
`SetAppLaunchOptions`.

**How to apply:**

- This is the ADR-0005 "reliable" branch → **#785 proceeds as the full refactor**: bake the resolved ROM path into
  `launch_options` at download-complete (an update-to-existing, now proven safe), make `bin/rom-launcher` a pure exec
  wrapper, drop the interim DB-read launcher.
- **Robust write pattern:** fire `SetAppLaunchOptions`, then poll `AppDetails` (`RegisterForAppDetails`) until the value
  lands — not fire-and-forget. The ~150 ms read-back makes the confirm cheap. The current `syncManager.ts` update path
  (existing-shortcut branch) is fire-and-forget and should adopt this.
- **Shortcut enumeration:** `collectionStore.deckDesktopApps.apps` gives the appId set, but the overview object lacks
  `display_name` for freshly-created shortcuts (name lives in `AppDetails.strDisplayName`). Matters for #785's ownership
  detection, which moves off the `romm:<id>` launch_options marker onto the launcher **exe path**.
- **Doc debt to settle in the #785 PR:** update CLAUDE.md's "Shortcut property re-sync" note +
  `steam-non-steam-shortcuts.md` to the narrowed reality, and flip ADR-0005's status to the reliable branch.

Related: [[desktop-mode-test-constraints]], [[dev-deploy-loop]].
