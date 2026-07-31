---
name: desktop-mode-test-constraints
description: Steam Deck Game Mode vs Desktop Mode constraints for smoke-testing the Decky plugin. The plugin toggle UI is only accessible in Game Mode — from Desktop Mode the user can only stop/start plugin_loader via systemctl or switch to Game Mode (which restarts the loader as part of the session swap). Game Mode and Desktop Mode are temporally mutually exclusive — no live mid-UI injection (Claude runs in Desktop terminal, user exercises UI in Game Mode). TOCTOU-style tests (inject server state mid-modal) are not doable; static setup → switch → observe is the only viable shape. The stale_conflict / TOCTOU guard #384 is covered by service-layer unit tests; on-device it's untestable here.
type: project
---

# Smoke testing on the Steam Deck — desktop mode constraints

The Decky plugin toggle UI is only accessible in **Game Mode**. When the user is in Desktop Mode:

- They cannot toggle the plugin off/on through the Decky UI.
- The available controls are stopping/starting the plugin loader from the terminal (e.g.
  `systemctl --user stop plugin_loader`) or switching to Game Mode (which restarts the plugin loader as part of the
  session swap).
- "Toggle plugin off in Decky" instructions only work when the user is testing in Game Mode.

When prepping a test that needs the plugin restarted: ask whether they're on desktop or game mode and tailor the prep
step accordingly. A switch to Game Mode is itself a plugin reload, so a typical desktop-mode test flow is: I prep state
file edits with the loader stopped, user switches to Game Mode (which loads the plugin), they exercise the UI, I verify
state on disk afterwards.

**Game Mode and Desktop Mode are temporally mutually exclusive — no live mid-UI injection.** I (Claude) run in the
Desktop-Mode terminal session; the user exercises the plugin UI in Game Mode. They cannot be in both at once, so they
can't relay live UI state (e.g. "the modal shows server_save_id=48") back to me while I simultaneously write to the
server/files. Any test that needs a server/file change injected _while a UI element is open_ (TOCTOU-style: open
conflict modal → inject newer server save mid-modal → resolve → expect `stale_conflict`) is **not doable** in this setup
— skip those. Static setup → switch to Game Mode → observe is the only viable shape. (The `stale_conflict` / TOCTOU
guard #384 is covered by unit tests at the service layer; on-device it's untestable here.)
