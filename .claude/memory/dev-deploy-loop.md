---
name: dev-deploy-loop
description: Standard tight dev loop on the Steam Deck. Claude edits in Desktop Mode (repo at /home/deck/Repos/decky-romm-sync) → user runs `mise run dev` (builds via Rollup → dist/index.js and restarts plugin_loader, deploying locally) → user switches to Game Mode (session swap reloads the plugin) → user exercises UI on real hardware → reports observed behavior. Real-hardware testing is fast and cheap — prefer asking the user to deploy and try a candidate fix over over-engineering theoretical solutions. `mise run dev` is the canonical command; don't invent alternatives.
type: project
---

# Dev deploy loop on the Steam Deck

The user often works **on the Steam Deck itself** in Desktop Mode while iterating on the plugin. The standard tight loop
is:

1. I make code changes in Desktop Mode (repo at `/home/deck/Repos/decky-romm-sync`).
2. User runs `mise run dev` — builds (Rollup → `dist/index.js`) and restarts `plugin_loader`, deploying locally.
3. User switches to Game Mode (the session swap reloads the plugin) and exercises the UI on the actual hardware.
4. User reports observed behaviour; we iterate.

So when investigating QAM/UI issues, assume real-hardware testing is fast and cheap — I can ask the user to deploy and
try a candidate fix rather than over-engineering theoretical solutions. `mise run dev` is the canonical deploy command;
do not invent alternatives.

See also [[desktop-mode-test-constraints]] for the Game-Mode-vs-Desktop-Mode temporal exclusion that shapes what kinds
of tests are possible.
